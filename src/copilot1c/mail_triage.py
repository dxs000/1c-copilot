"""Письмо и обращения: новое обращение, обновление существующего или просто материал для базы.

Аналитик бросает .msg в чат одной фразой («просмотри; при необходимости зарегистрируй или обнови обращение»).
Разбор (triage) ничего не сохраняет и для каждого письма отвечает:

1. Что в нём нового — письма сверяются с ветками переписки (letters.py): известное — не новое.
2. К какому обращению относится — признаки, от сильного к слабому:
   - письмо этой ветки уже связано с обращением (ответ на переписку, из которой его зарегистрировали);
   - этот же файл уже приложен к обращению или Message-ID письма — источник обращения;
   - номер ОБР в вопросе аналитика или в письме;
   - номер заявки из темы (0000026095, INC0012345, [#1234]) уже записан у обращения (issues.external_refs);
   - похожий текст / объекты 1С / место ошибки (related.py) — только кандидат, не решение.
3. Предложение: «обновить ОБР-…» (сильный признак), «новое обращение» (похоже на проблему, обращения нет),
   «только в базу знаний» (не похоже на проблему). Для обновления — что изменилось, комментарий в историю и
   предлагаемый статус (модель, если есть ключи AI Studio; иначе — шаблон по новым письмам).

Связь письма с обращением (link_email) делается при прикреплении письма к обращению: письма записываются в
ветки, ветка привязывается к обращению, номера заявок из темы — в external_refs. Так следующий ответ по той же
переписке узнаётся сам, без номера в вопросе.
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger("copilot1c.mail_triage")

# Номера заявок внешних систем в теме письма: длинные числа (0000026095), INC/SR/REQ/RITM + цифры, [#1234], «заявка № 123»
_REF_RES = [
    re.compile(r"(?<![\w.])(\d{7,12})(?![\w.])"),
    re.compile(r"\b((?:INC|SR|REQ|RITM|CHG|TASK|SD)[-_]?\d{4,10})\b", re.IGNORECASE),
    re.compile(r"\[#\s*(\d{3,10})\]"),
    re.compile(r"(?:заявк|тикет|инцидент|ticket|request)\w*\s*(?:№|N|#|no\.?)?\s*(\d{3,12})\b", re.IGNORECASE),
]
UPDATE_SCORE = 2.0       # с какого признака предлагается «обновить», а не «новое»
SIMILAR_MIN = 0.6        # похожий текст — кандидат, начиная с этого сходства
NEW_LETTERS_FOR_MODEL = 12000


def external_refs(*texts: str) -> list[str]:
    out: list[str] = []
    for t in texts:
        for rx in _REF_RES:
            for m in rx.finditer(t or ""):
                ref = m.group(1).upper().replace("_", "-")
                if ref not in out:
                    out.append(ref)
    return out


def _subjects(parsed) -> list[str]:
    from copilot1c.ingest.msg import walk

    return [m.subject for part in walk(parsed) for m in part.messages if m.subject]


# ---------- связь письма с обращением ----------

def link_email(conn, settings, issue_id: int, name: str, data: bytes, index=None, actor: str | None = None) -> dict:
    """Письмо, прикреплённое к обращению, — в ветки переписки (новые письма — и в базу поиска, если передан index),
    ветки — к обращению (если ещё ни к чему не привязаны), номера заявок из темы — в external_refs."""
    from copilot1c.ingest.attachments import parse_bytes
    from copilot1c.ingest.msg import ParsedEmail
    from copilot1c.letters import LetterStore, ingest_emails

    quick = settings.model_copy(update={"ocr_backend": "none"})
    parsed = [p for p in parse_bytes(data, name, f"issue:{issue_id}/{name}", settings=quick) if isinstance(p, ParsedEmail)]
    if not parsed:
        return {"threads": [], "refs": []}
    if index is not None:
        results = ingest_emails(conn, settings, parsed, index, summaries=True)
    else:  # без эмбеддингов: письма и ветки для узнавания, без фрагментов в поиске
        st = LetterStore(conn, settings.project)
        results = [r for p in parsed for r in st.ingest(p)]
    tids = sorted({r.thread_id for r in results if r.thread_id})
    with conn.cursor() as cur:
        if tids:
            cur.execute("UPDATE threads SET issue_id = %s WHERE id = ANY(%s) AND issue_id IS NULL", (issue_id, tids))
        refs = external_refs(*[s for p in parsed for s in _subjects(p)])
        if refs:
            cur.execute("""UPDATE issues SET external_refs = (SELECT array(SELECT DISTINCT x FROM
                               unnest(external_refs || %s::text[]) x ORDER BY x)) WHERE id = %s""", (refs, issue_id))
    conn.commit()
    return {"threads": tids, "refs": refs, "letters_new": sum(len(r.new_letters) for r in results)}


# ---------- разбор ----------

def _candidates(conn, project: str, question: str, parsed, chains, sha: str, new_text: str) -> list[dict[str, Any]]:
    from copilot1c.agent.issues_tools import mentioned_numbers
    from copilot1c.ingest.msg import walk
    from copilot1c.related import find_related

    found: dict[int, dict[str, Any]] = {}

    def add(issue_id: int, score: float, why: str) -> None:
        c = found.setdefault(issue_id, {"id": issue_id, "score": 0.0, "why": []})
        c["score"] += score
        if why not in c["why"]:
            c["why"].append(why)

    with conn.cursor() as cur:
        for r in chains:
            if r.issue_id:
                add(r.issue_id, 3.0, "переписка этой ветки уже связана с обращением")
        cur.execute("SELECT DISTINCT issue_id FROM issue_attachments a JOIN issues i ON i.id = a.issue_id "
                    "WHERE i.project = %s AND a.sha256 = %s", (project, sha))
        for (iid,) in cur.fetchall():
            add(iid, 3.0, "этот файл уже приложен к обращению")
        mids = [m.message_id for part in walk(parsed) for m in part.messages if m.message_id]
        if mids:
            cur.execute("SELECT id FROM issues WHERE project = %s AND source_message_id = ANY(%s)", (project, mids))
            for (iid,) in cur.fetchall():
                add(iid, 3.0, "обращение зарегистрировано из этого письма")
        texts = [question] + _subjects(parsed)
        for n in mentioned_numbers(" ".join(texts) + " " + new_text[:4000]):
            cur.execute("SELECT id FROM issues WHERE project = %s AND id = %s", (project, n))
            if cur.fetchone():
                add(n, 3.0, f"номер ОБР-{n:04d} упомянут")
        refs = external_refs(*_subjects(parsed))
        if refs:
            cur.execute("SELECT id, external_refs FROM issues WHERE project = %s AND external_refs && %s",
                        (project, refs))
            for iid, theirs in cur.fetchall():
                add(iid, 2.5, "номер заявки в теме: " + ", ".join(sorted(set(theirs) & set(refs))))
    conn.commit()
    if new_text.strip():
        for x in find_related(conn, project, {"title": " ".join(_subjects(parsed)[:1]), "description": new_text,
                                              "error_text": new_text})["issues"]:
            if x["score"] >= SIMILAR_MIN:
                add(x["id"], min(x["score"], 1.5), "похоже: " + ", ".join(x["why"]))
    return sorted(found.values(), key=lambda c: -c["score"])


def _issue_brief(reg, issue_id: int) -> dict[str, Any] | None:
    from copilot1c.issues import STATUSES

    i = reg.get(issue_id)
    if i is None:
        return None
    return {"id": i["id"], "number": i["number"], "title": i["title"], "status": i["status"],
            "status_label": STATUSES.get(i["status"], i["status"]), "version": i["version"],
            "assignee": i.get("assignee"), "description": (i.get("description") or "")[:1500],
            "resolution": i.get("resolution"), "external_refs": i.get("external_refs") or []}


UPDATE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "what_changed": {"type": "string", "description": "Что нового по обращению в этих письмах, 1–3 фразы"},
        "status": {"type": "string", "enum": ["new", "in_progress", "wait_customer", "wait_developer", "resolved",
                                               "closed"],
                   "description": "Статус обращения после этих писем"},
        "comment": {"type": "string", "description": "Комментарий в историю обращения: суть обновления, кто что "
                                                      "сообщил, что дальше; без приветствий и подписей"},
        "resolution": {"type": "string", "description": "Решение, если из писем видно, что проблема решена"},
    },
    "required": ["what_changed", "status", "comment"],
}
UPDATE_SYSTEM = ("Ты ведёшь обращения пользователей по системам 1С. Тебе дана карточка обращения и новые письма по "
                 "нему. Определи, что изменилось, какой статус теперь у обращения (ждём заказчика — если вопрос задан "
                 "пользователю; у разработчика — если передано в доработку; решено — если пользователь подтвердил "
                 "исправление; иначе — в работе), и напиши комментарий в историю. Только факты из писем.")


def _update_proposal(issue: dict[str, Any], new_letters: list, settings, use_llm: bool) -> dict[str, Any]:
    letters_text = "\n\n".join(
        f"— {x.sender or '?'}, {x.sent_at:%d.%m.%Y %H:%M}:\n{x.body}" if x.sent_at else f"— {x.sender or '?'}:\n{x.body}"
        for x in sorted(new_letters, key=lambda x: (x.sent_at is None, x.sent_at)))[-NEW_LETTERS_FOR_MODEL:]
    if (use_llm and settings is not None and getattr(settings, "intake_llm", True) and settings.yc_api_key
            and settings.yc_folder_id and new_letters):
        from copilot1c.index.yandex import chat_json

        try:
            res = chat_json(f"Обращение {issue['number']} «{issue['title']}», статус: {issue['status_label']}.\n"
                            f"Описание: {issue['description']}\n\nНовые письма:\n\n{letters_text}",
                            UPDATE_SCHEMA, model=settings.model_batch, system=UPDATE_SYSTEM, settings=settings)
            return {"what_changed": res.get("what_changed", ""), "status": res.get("status") or issue["status"],
                    "comment": res.get("comment", ""), "resolution": res.get("resolution"), "method": "llm"}
        except Exception as exc:  # noqa: BLE001 — модель недоступна: шаблон
            log.warning("обновление обращения моделью не разобрано: %s: %s", type(exc).__name__, exc)
    if not new_letters:
        return {"what_changed": "новых писем нет — всё уже известно", "status": issue["status"], "comment": "",
                "resolution": None, "method": "template"}
    last = max(new_letters, key=lambda x: (x.sent_at is not None, x.sent_at))
    body = re.sub(r"\s+", " ", last.body).strip()
    comment = "\n".join(f"Письмо {x.sender or '?'}" + (f" от {x.sent_at:%d.%m.%Y %H:%M}" if x.sent_at else "")
                        + ": " + re.sub(r"\s+", " ", x.body).strip()[:400] for x in new_letters[:5])
    return {"what_changed": f"{last.sender or '?'}: {body[:300]}" + ("…" if len(body) > 300 else ""),
            "status": issue["status"], "comment": comment, "resolution": None, "method": "template"}


def triage(files: list[tuple[str, bytes]], question: str, conn, settings, use_llm: bool = True) -> dict[str, Any]:
    """Разбор писем по обращениям. Ничего не сохраняет."""
    import hashlib

    from copilot1c.email_intake import is_email_file
    from copilot1c.ingest.attachments import parse_bytes
    from copilot1c.ingest.msg import ParsedEmail
    from copilot1c.intent import heuristics
    from copilot1c.issues import STATUSES, IssueRegistry
    from copilot1c.letters import LetterStore, agent_view, results_report

    quick = settings.model_copy(update={"ocr_backend": "none"})
    st = LetterStore(conn, settings.project)
    reg = IssueRegistry(conn, settings.project)
    out = []
    for name, data in files:
        if not is_email_file(name):
            continue
        parsed = next((p for p in parse_bytes(data, name, name, settings=quick) if isinstance(p, ParsedEmail)), None)
        if parsed is None:
            out.append({"filename": name, "error": "письмо не разобрано"})
            continue
        chains = st.check(parsed)
        conn.rollback()
        new_letters = [x for r in chains for x in r.new_letters]
        new_text = "\n\n".join(x.body for x in new_letters)
        cands = _candidates(conn, settings.project, question, parsed, chains, hashlib.sha256(data).hexdigest(), new_text)
        briefs = []
        for c in cands[:5]:
            b = _issue_brief(reg, c["id"])
            if b:
                briefs.append({**b, "score": round(c["score"], 2), "why": c["why"]})
        best = briefs[0] if briefs and briefs[0]["score"] >= UPDATE_SCORE else None
        looks_like_problem = heuristics(new_text or parsed.message.body, has_files=False).is_issue
        if best:
            decision = "update"
        elif looks_like_problem:
            decision = "new"
        else:
            decision = "knowledge"
        item: dict[str, Any] = {
            "filename": name, "subject": parsed.message.subject, "decision": decision,
            "letters": results_report(chains), "new_letters": [x.out() | {"excerpt": re.sub(r"\s+", " ", x.body)[:400]}
                                                              for x in new_letters],
            "agent_view": agent_view(chains), "candidates": briefs, "refs": external_refs(*_subjects(parsed)),
        }
        if decision == "new":  # система и подсистема будущего обращения
            from copilot1c.contours import suggest

            item["contours"] = suggest(conn, settings.project, f"{parsed.message.subject}\n{new_text}",
                                       settings=settings, use_llm=use_llm)
            conn.rollback()
        if best:
            item["update"] = _update_proposal(best, new_letters, settings, use_llm)
            item["update"]["status_label"] = STATUSES.get(item["update"]["status"], item["update"]["status"])
        out.append(item)
    return {"emails": out, "statuses": [{"value": k, "label": v} for k, v in STATUSES.items()]}


def agent_block(res: dict[str, Any]) -> str:
    lines = []
    for e in res.get("emails", []):
        if e.get("error"):
            lines.append(f"- «{e['filename']}»: {e['error']}")
            continue
        rep = e["letters"]
        head = (f"- «{e['filename']}» (тема «{e['subject']}»): новых писем {rep['letters_new']}, "
                f"известных {rep['letters_known']}.")
        if e["decision"] == "update":
            c = e["candidates"][0]
            u = e["update"]
            head += (f" Относится к {c['number']} «{c['title']}» (статус «{c['status_label']}»; почему: "
                     f"{'; '.join(c['why'])}). Что нового: {u['what_changed']}. Предлагаемый статус: {u['status_label']}.")
        elif e["decision"] == "new":
            head += " Похоже на новую проблему — зарегистрированного обращения не найдено."
            if e["candidates"]:
                head += " Слабые кандидаты: " + "; ".join(f"{c['number']} ({', '.join(c['why'])})"
                                                         for c in e["candidates"][:3])
        else:
            head += " На сообщение о проблеме не похоже — материал для базы знаний."
        lines.append(head)
    return "\n".join(lines)


TRIAGE_TASK = ("Аналитик просит разобрать письмо по обращениям. Ниже — автоматический разбор: что в письме нового, "
               "к какому обращению оно относится и что предлагается. Ответь коротко: о чём новое в письме, к какому "
               "обращению относится (или почему это новая проблема), что изменилось и что делать дальше. Регистрацию "
               "и обновление обращения аналитик подтверждает кнопкой в карточке под ответом — сам ничего не "
               "регистрируй и не утверждай, что обновил.")
