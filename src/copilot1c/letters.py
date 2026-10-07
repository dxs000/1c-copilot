"""Письма и ветки переписки: что в приложенном письме уже известно, а что ново.

Каждое новое письмо по проблеме несёт небольшое обновление и весь хвост прежней переписки в цитатах. Поэтому
единица хранения — отдельное письмо (таблица letters), а файл режется на письма (ingest/msg.py, thread.py):

1. Каждое письмо сверяется с уже сохранёнными:
   - по Message-ID (только у писем-файлов);
   - иначе по отправителю, времени и тексту. Время в заголовке цитаты записано без часового пояса и
     читается в поясе верхнего письма — расходится с настоящим на целые часы, поэтому кандидаты берутся
     в окне ±14 часов, а «та же минута с точностью до пояса» снижает порог похожести текста. Текст
     сравнивается по шинглам (тройки слов): доля общих от меньшего — цитату Outlook часто обрезает.
2. Совпало — письмо известно: в базу поиска повторно не идёт и агенту целиком не показывается.
   Если письмо пришло файлом, а раньше было известно только по цитате, — уточняется (Message-ID, полный
   текст). Строки, которых не было в сохранённом тексте («см. ниже красным»), — ответ внутри цитаты: они
   приписываются письму-автору этого файла.
3. Ветка — по пересечению с известными письмами, а не по теме (тема в переписке меняется). Совпали письма
   из двух веток — ветки сливаются. Ничего не совпало — ветка с той же темой и общим участником за
   последние 90 дней, иначе новая.
4. Сводка ветки (summarize) — проблема, что сделано, чего ждём, статус, открытые вопросы; пересобирается
   при новых письмах. Модель — если есть ключи AI Studio, иначе шаблон по последним письмам.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from psycopg.rows import dict_row

from copilot1c.ingest.msg import EmailMessage, ParsedEmail, message_chunk, thread_id, walk
from copilot1c.models import Chunk, DocType

log = logging.getLogger("copilot1c.letters")

DATE_WINDOW = timedelta(hours=14)   # разброс часовых поясов в заголовках цитат
SAME_MINUTE = 120                   # секунд: «та же минута» с точностью до целых часов пояса
MATCH_STRONG = 0.4                  # порог похожести текста, если время совпало до минуты
MATCH_WEAK = 0.75                   # порог, если совпало только окно ±14 часов
THREAD_BY_SUBJECT_DAYS = 90
INLINE_MIN_CHARS = 15               # строка ответа внутри цитаты не короче


# ---------- сравнение текстов ----------

def _words(text: str) -> list[str]:
    return re.findall(r"[\wЁё]+", (text or "").casefold().replace("ё", "е"))


def shingles(text: str, n: int = 3) -> set[int]:
    w = _words(text)
    if len(w) < n:
        return {hash(" ".join(w))} if w else set()
    return {int(hashlib.md5(" ".join(w[i:i + n]).encode()).hexdigest()[:12], 16) for i in range(len(w) - n + 1)}


def containment(a: str, b: str) -> float:
    """Доля общих шинглов от меньшего текста: обрезанная цитата целиком входит в оригинал."""
    sa, sb = shingles(a), shingles(b)
    if not sa or not sb:
        return 1.0 if _words(a) == _words(b) else 0.0
    return len(sa & sb) / min(len(sa), len(sb))


def _norm_line(line: str) -> str:
    return " ".join(_words(line))


def added_lines(new: str, old: str) -> list[str]:
    """Строки нового варианта цитаты, которых нет в сохранённом тексте письма (ответы внутри цитаты)."""
    known = {_norm_line(x) for x in (old or "").splitlines()}
    known_text = " ".join(_words(old))
    out = []
    for line in (new or "").splitlines():
        n = _norm_line(line)
        if len(n) >= INLINE_MIN_CHARS and n not in known and n not in known_text:
            out.append(line.strip())
    return out


def sender_key(name: str, addr: str) -> str:
    """Как в ingest.msg._person_key: фамилия одинакова в свойствах письма и в цитате, где адреса может не быть."""
    first = re.split(r"[\s,]+", (name or "").strip())[0] if (name or "").strip() else ""
    return (first or (addr or "").split("@")[0]).casefold().replace("ё", "е")


def _same_minute(a: datetime | None, b: datetime | None) -> bool:
    if a is None or b is None:
        return False
    try:
        d = abs((a - b).total_seconds())
    except TypeError:  # одна дата без пояса — сравниваем как есть
        d = abs((a.replace(tzinfo=None) - b.replace(tzinfo=None)).total_seconds())
    if d > DATE_WINDOW.total_seconds():
        return False
    r = d % 3600
    return r <= SAME_MINUTE or 3600 - r <= SAME_MINUTE


def _in_window(a: datetime | None, b: datetime | None) -> bool:
    if a is None or b is None:
        return True
    try:
        return abs(a - b) <= DATE_WINDOW
    except TypeError:
        return abs(a.replace(tzinfo=None) - b.replace(tzinfo=None)) <= DATE_WINDOW


# ---------- результат ----------

@dataclass
class LetterInfo:
    id: int | None              # None — в пробном прогоне для нового письма
    new: bool
    sender: str
    sent_at: datetime | None
    subject: str
    origin: str                 # file | quoted
    body: str
    upgraded: bool = False      # было известно по цитате, теперь пришло файлом
    inline_notes: list[str] = field(default_factory=list)
    chunk: Chunk | None = None  # для новых писем — фрагмент в базу поиска

    def out(self) -> dict[str, Any]:
        return {"id": self.id, "new": self.new, "sender": self.sender, "subject": self.subject,
                "sent_at": self.sent_at.isoformat(timespec="minutes") if self.sent_at else None,
                "origin": self.origin, "upgraded": self.upgraded, "inline_notes": self.inline_notes,
                "chars": len(self.body)}


@dataclass
class ChainResult:
    thread_id: int | None       # None — новая ветка (в пробном прогоне)
    thread_title: str
    thread_new: bool
    letters: list[LetterInfo]   # от новых к старым, как в файле
    merged: list[int] = field(default_factory=list)   # ветки, слитые в эту
    summary_text: str | None = None
    issue_id: int | None = None

    @property
    def new_letters(self) -> list[LetterInfo]:
        return [x for x in self.letters if x.new]

    def out(self) -> dict[str, Any]:
        return {"thread_id": self.thread_id, "thread_title": self.thread_title, "thread_new": self.thread_new,
                "new": len(self.new_letters), "known": len(self.letters) - len(self.new_letters),
                "merged": self.merged, "issue_id": self.issue_id, "summary": self.summary_text,
                "letters": [x.out() for x in self.letters]}


# ---------- хранилище ----------

class LetterStore:
    """Письма и ветки поверх открытого подключения psycopg. Коммит — за вызывающим."""

    def __init__(self, conn, project: str):
        self.conn, self.project = conn, project

    def _rows(self, sql: str, params: tuple | list) -> list[dict[str, Any]]:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else []

    # --- поиск известного письма ---

    def _find(self, m: EmailMessage, pending: list[tuple[EmailMessage, dict]]) -> dict | None:
        if m.message_id:
            rows = self._rows("SELECT * FROM letters WHERE project = %s AND message_id = %s", (self.project, m.message_id))
            if rows:
                return rows[0]
        key = sender_key(m.sender, m.sender_email)
        if not key:
            return None
        if m.date is not None:
            rows = self._rows("""SELECT * FROM letters WHERE project = %s AND sender_key = %s
                                 AND (sent_at IS NULL OR sent_at BETWEEN %s AND %s) ORDER BY id LIMIT 200""",
                              (self.project, key, m.date - DATE_WINDOW, m.date + DATE_WINDOW))
        else:
            rows = self._rows("SELECT * FROM letters WHERE project = %s AND sender_key = %s ORDER BY id DESC LIMIT 200",
                              (self.project, key))
        cands = rows + [row for pm, row in pending if row.get("sender_key") == key and _in_window(pm.date, m.date)]
        best, best_score = None, 0.0
        for c in cands:
            if m.message_id and c.get("message_id") and c["message_id"] != m.message_id:
                continue  # у обоих есть Message-ID, и они разные — это разные письма
            score = containment(m.body, c["body"])
            need = MATCH_STRONG if _same_minute(m.date, c.get("sent_at")) else MATCH_WEAK
            if score >= need and score > best_score:
                best, best_score = c, score
        return best

    # --- ветка ---

    def _thread_by_subject(self, chain: list[EmailMessage]) -> dict | None:
        subject = thread_id(chain[0].subject)
        if not subject:
            return None
        keys = sorted({sender_key(m.sender, m.sender_email) for m in chain} - {""})
        dates = [m.date for m in chain if m.date]
        since = (max(dates) if dates else datetime.now().astimezone()) - timedelta(days=THREAD_BY_SUBJECT_DAYS)
        rows = self._rows("""SELECT t.* FROM threads t
                             WHERE t.project = %s AND lower(t.subject) = lower(%s)
                               AND (t.last_at IS NULL OR t.last_at >= %s)
                               AND EXISTS (SELECT 1 FROM letters l WHERE l.thread_id = t.id AND l.sender_key = ANY(%s))
                             ORDER BY t.last_at DESC NULLS LAST LIMIT 1""", (self.project, subject, since, keys))
        return rows[0] if rows else None

    def _merge(self, keep: int, others: list[int]) -> None:
        if not others:
            return
        rows = self._rows("SELECT id, issue_id, contours FROM threads WHERE id = ANY(%s) OR id = %s ORDER BY id",
                          (others, keep))
        issue = next((r["issue_id"] for r in rows if r["id"] == keep and r["issue_id"]), None) \
            or next((r["issue_id"] for r in rows if r["issue_id"]), None)
        contours = sorted({c for r in rows for c in (r["contours"] or [])})
        self._rows("UPDATE letters SET thread_id = %s WHERE thread_id = ANY(%s)", (keep, others))
        self._rows("UPDATE threads SET issue_id = %s, contours = %s WHERE id = %s", (issue, contours, keep))
        self._rows("UPDATE chunks SET status = 'superseded' WHERE chunk_id IN (SELECT summary_chunk FROM threads "
                   "WHERE id = ANY(%s) AND summary_chunk IS NOT NULL)", (others,))
        self._rows("DELETE FROM threads WHERE id = ANY(%s)", (others,))

    def _touch(self, tid: int) -> None:
        self._rows("""UPDATE threads SET first_at = (SELECT min(sent_at) FROM letters WHERE thread_id = %s),
                                         last_at = (SELECT max(sent_at) FROM letters WHERE thread_id = %s),
                                         updated_at = now() WHERE id = %s""", (tid, tid, tid))

    # --- разбор файла ---

    def check(self, parsed: ParsedEmail) -> list[ChainResult]:
        """Пробный прогон: что известно, что ново, к какой ветке относится. Ничего не пишет."""
        pending: list[tuple[EmailMessage, dict]] = []  # письмо из вложенной цепочки может повторять верхнюю
        return [self._chain(part.messages, write=False, pending=pending) for part in walk(parsed) if part.messages]

    def ingest(self, parsed: ParsedEmail, material_id: int | None = None) -> list[ChainResult]:
        """Записывает новые письма и ветки. Фрагменты новых писем — в LetterInfo.chunk (записывает их
        в базу поиска вызывающий, с material_id), id писем ставятся в фрагменты уже здесь."""
        return [self._chain(part.messages, write=True, material_id=material_id)
                for part in walk(parsed) if part.messages]

    def _chain(self, chain: list[EmailMessage], write: bool, material_id: int | None = None,
               pending: list[tuple[EmailMessage, dict]] | None = None) -> ChainResult:
        pending = [] if pending is None else pending
        kept: list[EmailMessage] = []
        found: list[dict | None] = []
        for m in chain:
            if not (m.body or "").strip():
                continue
            row = self._find(m, pending)
            if row is not None and row.get("id") is None:
                continue  # то же письмо уже встретилось в этом файле (ещё не записано) — одно
            kept.append(m)
            found.append(row)
            if row is None:
                pending.append((m, {"sender_key": sender_key(m.sender, m.sender_email), "body": m.body,
                                    "sent_at": m.date, "message_id": m.message_id, "id": None, "thread_id": None}))
        chain = kept
        if not chain:
            return ChainResult(None, "", False, [])
        tids = sorted({r["thread_id"] for r in found if r is not None and r.get("thread_id")})
        thread_new = False
        if tids:
            tid, merged = tids[0], tids[1:]
            thread = self._rows("SELECT * FROM threads WHERE id = %s", (tid,))[0]
        else:
            merged = []
            thread = self._thread_by_subject(chain)
            tid = thread["id"] if thread else None
            thread_new = thread is None
        if write and merged:
            self._merge(tid, merged)
        if write and tid is None:
            subj = thread_id(chain[-1].subject) or thread_id(chain[0].subject) or "(без темы)"
            thread = self._rows("INSERT INTO threads (project, subject) VALUES (%s, %s) RETURNING *",
                                (self.project, subj[:500]))[0]
            tid = thread["id"]
        title = (thread or {}).get("title") or (thread or {}).get("subject") or thread_id(chain[0].subject)

        infos: list[LetterInfo] = []
        for m, row in zip(chain, found, strict=True):
            info = LetterInfo(row["id"] if row else None, row is None, m.sender, m.date, m.subject, m.origin, m.body)
            if row is not None and m.origin == "quoted":
                info.inline_notes = added_lines(m.body, row["body"])
            if row is not None and m.origin == "file" and row["origin"] == "quoted":
                info.upgraded = True
            infos.append(info)
        # ответы внутри цитат принадлежат автору верхнего письма файла
        notes = [(chain[i], infos[i].inline_notes) for i in range(1, len(chain)) if infos[i].inline_notes]
        top_notes = "\n\n".join(f"Ответ внутри цитаты письма {m.sender or '?'}"
                                + (f" от {m.date:%d.%m.%Y %H:%M}" if m.date else "") + ":\n" + "\n".join(lines)
                                for m, lines in notes)

        if write:
            for i, (m, info) in enumerate(zip(chain, infos, strict=True)):
                if info.new:
                    body_notes = top_notes if i == 0 and top_notes else None
                    info.id = self._rows(
                        """INSERT INTO letters (project, thread_id, message_id, sender, sender_email, sender_key,
                               recipients, sent_at, subject, body, inline_notes, origin, source, material_id)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (self.project, tid, m.message_id, m.sender, m.sender_email,
                         sender_key(m.sender, m.sender_email), "; ".join(x for x in (m.to, m.cc) if x)[:2000],
                         m.date, m.subject, m.body, body_notes, m.origin, m.source, material_id))[0]["id"]
                    chunk = message_chunk(m, self.project)
                    if body_notes:
                        chunk.text += "\n\n" + body_notes
                        chunk.body += "\n" + body_notes
                    chunk.extra.update({"letter_id": str(info.id), "thread": str(tid)})
                    info.chunk = chunk
                    self._rows("UPDATE letters SET chunk_id = %s WHERE id = %s", (chunk.chunk_id, info.id))
                elif info.upgraded:  # письмо известно по цитате, теперь — файлом: точнее дата, есть Message-ID
                    self._rows("""UPDATE letters SET origin = 'file', message_id = COALESCE(message_id, %s),
                                      sent_at = COALESCE(%s, sent_at), body = CASE WHEN length(%s) > length(body)
                                      THEN %s ELSE body END, source = %s WHERE id = %s""",
                               (m.message_id, m.date, m.body, m.body, m.source, info.id))
            ids = [x.id for x in infos]
            for child, parent in zip(ids, ids[1:], strict=False):  # [0] — ответ на [1], [1] — на [2]…
                self._rows("UPDATE letters SET parent_id = COALESCE(parent_id, %s) WHERE id = %s AND id <> %s",
                           (parent, child, parent))
            self._touch(tid)
            thread = self._rows("SELECT * FROM threads WHERE id = %s", (tid,))[0]
        return ChainResult(tid, title, thread_new, infos, merged, (thread or {}).get("summary_text"),
                           (thread or {}).get("issue_id"))

    # --- чтение ---

    def thread(self, tid: int) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM threads WHERE project = %s AND id = %s", (self.project, tid))
        if not rows:
            return None
        t = rows[0]
        t["letters"] = self._rows("""SELECT id, parent_id, message_id, sender, sender_email, recipients, sent_at,
                                            subject, body, inline_notes, origin, material_id, first_seen
                                     FROM letters WHERE thread_id = %s ORDER BY sent_at NULLS FIRST, id""", (tid,))
        return t

    def find_thread(self, ref: str) -> dict[str, Any] | None:
        """Ветка по номеру или по словам темы (последняя по времени)."""
        ref = (ref or "").strip()
        if ref.isdigit():
            return self.thread(int(ref))
        rows = self._rows("""SELECT id FROM threads WHERE project = %s AND (coalesce(title, subject) ILIKE %s)
                             ORDER BY last_at DESC NULLS LAST LIMIT 1""", (self.project, f"%{ref}%"))
        return self.thread(rows[0]["id"]) if rows else None

    def list(self, q: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self._rows("""SELECT t.id, t.subject, t.title, t.contours, t.issue_id, t.first_at, t.last_at,
                                    t.summary_text, t.summary_at,
                                    (SELECT count(*) FROM letters l WHERE l.thread_id = t.id) AS letters_count
                             FROM threads t WHERE t.project = %s AND (%s::text IS NULL OR
                                  coalesce(t.title, t.subject) ILIKE '%%' || %s || '%%')
                             ORDER BY t.last_at DESC NULLS LAST, t.id DESC LIMIT %s""", (self.project, q, q, limit))

    def update(self, tid: int, data: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {k: v for k, v in data.items() if k in ("title", "issue_id", "contours")}
        if allowed:
            sets = ", ".join(f"{k} = %s" for k in allowed)
            self._rows(f"UPDATE threads SET {sets}, updated_at = now() WHERE project = %s AND id = %s",
                       (*allowed.values(), self.project, tid))
            if "contours" in allowed:  # фрагменты писем ветки получают те же контуры
                self._rows("""UPDATE chunks SET contours = %s WHERE chunk_id IN (SELECT chunk_id FROM letters
                              WHERE thread_id = %s AND chunk_id IS NOT NULL) OR chunk_id = (SELECT summary_chunk
                              FROM threads WHERE id = %s)""", (allowed["contours"], tid, tid))
        return self.thread(tid)


# ---------- сводка ветки ----------

SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "problem": {"type": "string", "description": "Суть вопроса или проблемы, 1–2 фразы"},
        "done": {"type": "array", "items": {"type": "string"}, "description": "Что уже сделано или решено"},
        "waiting": {"type": "string", "description": "Чего ждём и от кого (роль или организация); пусто — ничего"},
        "status": {"type": "string", "enum": ["открыт", "ждём ответа", "в работе", "решён", "закрыт"]},
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["problem", "done", "status", "open_questions"],
}
SUMMARY_SYSTEM = ("Ты ведёшь сводку ветки деловой переписки по проекту 1С. По письмам в хронологическом порядке "
                  "опиши текущее состояние: суть вопроса, что сделано и решено, чего ждём и от кого, статус, открытые "
                  "вопросы. Опирайся только на письма; более поздние письма важнее ранних. Без приветствий и подписей.")
MAX_SUMMARY_INPUT = 24000


def _letters_text(letters: list[dict], limit: int = MAX_SUMMARY_INPUT) -> str:
    parts = []
    for x in letters:
        when = f"{x['sent_at']:%d.%m.%Y %H:%M}" if x.get("sent_at") else "дата неизвестна"
        body = x["body"] + (f"\n{x['inline_notes']}" if x.get("inline_notes") else "")
        parts.append(f"— {x.get('sender') or '?'}, {when}:\n{body.strip()}")
    text = "\n\n".join(parts)
    return text if len(text) <= limit else "…\n" + text[-limit:]  # свежие письма важнее — режем начало


def summary_text(s: dict[str, Any]) -> str:
    lines = [f"Суть: {s.get('problem', '').strip()}"]
    if s.get("done"):
        lines.append("Сделано: " + "; ".join(s["done"]))
    if s.get("waiting"):
        lines.append(f"Ждём: {s['waiting']}")
    lines.append(f"Статус: {s.get('status', 'открыт')}")
    if s.get("open_questions"):
        lines.append("Открытые вопросы: " + "; ".join(s["open_questions"]))
    return "\n".join(lines)


def _template_summary(letters: list[dict]) -> dict[str, Any]:
    first, last = letters[0], letters[-1]

    def lead(x):
        text = re.sub(r"\s+", " ", x["body"]).strip()
        return text[:300] + ("…" if len(text) > 300 else "")

    return {"problem": lead(first), "done": [], "waiting": "",
            "status": "открыт", "open_questions": [],
            "last": f"{last.get('sender') or '?'}: {lead(last)}" if last is not first else ""}


def summarize(store: LetterStore, tid: int, settings=None, use_llm: bool = True, index=None) -> dict[str, Any] | None:
    """Пересобирает сводку ветки: модель (если есть ключи) или шаблон. Сводка — и фрагментом в базе поиска
    (прежний фрагмент сводки → superseded)."""
    t = store.thread(tid)
    if t is None or not t["letters"]:
        return None
    letters = t["letters"]
    method = "template"
    data = None
    if use_llm and settings is not None and settings.yc_api_key and settings.yc_folder_id:
        from copilot1c.index.yandex import chat_json

        try:
            data = chat_json(f"Тема ветки: {t['title'] or t['subject']}\n\nПисьма:\n\n{_letters_text(letters)}",
                             SUMMARY_SCHEMA, model=settings.model_batch, system=SUMMARY_SYSTEM, settings=settings)
            method = "llm"
        except Exception as exc:  # noqa: BLE001 — модель недоступна: шаблон
            log.warning("сводка ветки %s моделью не удалась: %s: %s", tid, type(exc).__name__, exc)
    if not data:
        data = _template_summary(letters)
    data["method"] = method
    text = summary_text(data) + (f"\nПоследнее письмо — {data['last']}" if data.get("last") else "")
    title = t["title"] or t["subject"]
    dates = [x["sent_at"] for x in letters if x.get("sent_at")]
    period = f"{min(dates):%d.%m.%Y}–{max(dates):%d.%m.%Y}" if dates else ""
    participants = ", ".join(dict.fromkeys(x["sender"] for x in letters if x.get("sender")))
    chunk = Chunk(text=f"Сводка ветки переписки «{title}» ({len(letters)} писем{', ' + period if period else ''}; "
                       f"участники: {participants})\n\n{text}",
                  doc_type=DocType.EMAIL, source=f"thread:{tid}", title=f"Сводка ветки «{title}»"[:200],
                  project=store.project, date=max(dates) if dates else None,
                  extra={"thread": str(tid), "kind": "thread_summary"})
    chunk_id = t.get("summary_chunk")
    if index is not None:
        try:
            index.add([chunk], contours=t["contours"] or ())
            if chunk_id and chunk_id != chunk.chunk_id:
                index.set_status([chunk_id], "superseded")
            chunk_id = chunk.chunk_id
        except Exception as exc:  # noqa: BLE001 — эмбеддинги недоступны: сводка сохраняется, в поиск — позже
            log.warning("сводка ветки %s не записана в базу поиска: %s: %s", tid, type(exc).__name__, exc)
    from psycopg.types.json import Jsonb

    store._rows("""UPDATE threads SET summary = %s, summary_text = %s, summary_letters = %s, summary_at = now(),
                       summary_chunk = %s WHERE id = %s""", (Jsonb(data), text, len(letters), chunk_id, tid))
    return {**data, "text": text}


# ---------- текст для агента ----------

def agent_view(results: list[ChainResult], full_limit: int = 12000) -> str:
    """Письмо-файл глазами агента: сводка известной ветки, новые письма целиком, известные — строкой."""
    parts = []
    for r in results:
        if not r.letters:
            continue
        head = (f"Ветка «{r.thread_title}»" + (" (новая)" if r.thread_new or r.thread_id is None else f" № {r.thread_id}")
                + f": писем в файле {len(r.letters)}, новых {len(r.new_letters)}")
        if r.issue_id:
            head += f"; ветка связана с обращением ОБР-{r.issue_id:04d}"
        block = [head]
        if r.summary_text and len(r.new_letters) < len(r.letters):
            block.append(f"Сводка ветки по известным письмам:\n{r.summary_text}")
        known = [x for x in r.letters if not x.new]
        if known:
            block.append("Уже известны (в базе): " + "; ".join(
                f"{x.sender or '?'} {x.sent_at:%d.%m.%Y}" if x.sent_at else (x.sender or "?") for x in known))
        used = 0
        for x in reversed(r.letters):  # хронологически
            notes = x.inline_notes
            if not x.new and not notes:
                continue
            when = f"{x.sent_at:%d.%m.%Y %H:%M}" if x.sent_at else "дата неизвестна"
            text = x.body if x.new else "Ответ внутри цитаты: " + "\n".join(notes)
            room = full_limit - used
            if room <= 0:
                block.append("(остальные новые письма не вошли в лимит)")
                break
            block.append(f"Новое — {x.sender or '?'}, {when}, «{x.subject}»:\n{text[:room]}")
            used += min(len(text), room)
        parts.append("\n\n".join(block))
    return "\n\n".join(parts)


# ---------- запись писем из файлов (worker, index-docs) ----------

def ingest_emails(conn, settings, emails: list[ParsedEmail], index, material_id: int | None = None,
                  summaries: bool = True, contours: list[int] | None = None) -> list[ChainResult]:
    """Письма-файлы → письма и ветки; фрагменты только новых писем → база поиска (с контурами ветки);
    сводки веток, где появились новые письма. contours — решение аналитика: добавляются к контурам веток.
    Возвращает результаты по цепочкам."""
    store = LetterStore(conn, settings.project)
    results: list[ChainResult] = []
    for e in emails:
        results += store.ingest(e, material_id)
    touched: dict[int, list[Chunk]] = {}
    for r in results:
        for x in r.new_letters:
            touched.setdefault(r.thread_id, []).append(x.chunk)
    if contours:
        for tid in {r.thread_id for r in results if r.thread_id}:
            row = store._rows("SELECT contours FROM threads WHERE id = %s", (tid,))
            merged = sorted(set((row[0]["contours"] if row else None) or []) | set(contours))
            store.update(tid, {"contours": merged})
    for tid, chunks in touched.items():
        contours = store._rows("SELECT contours FROM threads WHERE id = %s", (tid,))
        index.add(chunks, material_id=material_id, contours=(contours[0]["contours"] if contours else None) or ())
    conn.commit()
    if summaries:
        for tid in touched:
            try:
                summarize(store, tid, settings, use_llm=getattr(settings, "thread_summary_llm", True), index=index)
                conn.commit()
            except Exception:  # noqa: BLE001 — сводка не должна ронять запись писем
                conn.rollback()
                log.exception("сводка ветки %s", tid)
    for r in results:  # сводка могла обновиться — в результате свежая
        if r.thread_id in touched:
            row = store._rows("SELECT summary_text FROM threads WHERE id = %s", (r.thread_id,))
            r.summary_text = row[0]["summary_text"] if row else r.summary_text
    return results


def results_report(results: list[ChainResult]) -> dict[str, Any]:
    threads: dict[Any, dict[str, Any]] = {}
    for r in results:
        if not r.letters:
            continue
        key = r.thread_id if r.thread_id is not None else f"new:{r.thread_title}"  # новая ветка (пробный прогон)
        t = threads.setdefault(key, {"id": r.thread_id, "title": r.thread_title, "new": 0, "known": 0,
                                     "issue_id": r.issue_id})
        t["new"] += len(r.new_letters)
        t["known"] += len(r.letters) - len(r.new_letters)
    return {"letters_new": sum(t["new"] for t in threads.values()),
            "letters_known": sum(t["known"] for t in threads.values()), "threads": list(threads.values())}
