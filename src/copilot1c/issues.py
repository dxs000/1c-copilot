"""Обращения: проблемы, о которых сообщают аналитики интегратора, и их инициаторы (контакты).

Заводят и ведут обращения только аналитики. Сотрудник заказчика, от которого пришла проблема, —
это контакт (таблица contacts), а не пользователь системы.

Что делает модуль:
- справочники статусов, категорий и приоритетов с русскими подписями (их же отдаёт веб-интерфейсу
  метод /issues/meta, чтобы подписи жили в одном месте);
- IssueRegistry — создание, список с фильтрами, карточка (вложения, история, инициатор), правка
  с защитой от одновременного редактирования, комментарии, вложения;
- каждая правка пишется в issue_events: история появляется сама, без отдельной логики в клиентах.

Защита от одновременной правки: у обращения есть поле version. Клиент присылает версию, которую
видел; если запись успели изменить — правка отклоняется (VersionConflict), клиент показывает
свежую версию, и аналитик повторяет правку осознанно.
"""

from __future__ import annotations

import hashlib
import mimetypes
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from copilot1c.materials import safe_filename

STATUSES = {
    "new": "новое",
    "in_progress": "в работе",
    "wait_customer": "ждём заказчика",
    "wait_developer": "у разработчика",
    "resolved": "решено",
    "closed": "закрыто",
    "rejected": "отклонено",
    "duplicate": "дубль",
    "transferred": "передано",   # не по профилю — передано другой команде (кому — transferred_to)
}
OPEN_STATUSES = ("new", "in_progress", "wait_customer", "wait_developer")
CATEGORIES = {
    "bug": "ошибка",
    "consult": "консультация",
    "change": "доработка",
    "data": "данные и НСИ",
    "performance": "производительность",
    "access": "права доступа",
    "other": "прочее",
}
PRIORITIES = {"critical": "критично", "high": "высокий", "medium": "средний", "low": "низкий"}
SOURCES = {"manual": "вручную", "chat": "чат", "email": "письмо"}

# Поля, которые можно задать при создании и менять правкой. Остальное ядро ведёт само
# (id, project, version, created_at, updated_at, resolved_at).
EDITABLE = (
    "title", "description", "summary", "error_text", "steps", "expected", "actual",
    "category", "priority", "status", "tags", "assignee", "due_date",
    "infobase", "server", "config_version", "platform_version", "objects",
    "initiator_contact_id", "reported_at", "registered_by", "source", "source_ref", "source_message_id",
    "classifier_confidence", "duplicate_of", "requirement_ids", "test_case_ids",
    "root_cause", "resolution", "kb_material_id", "external_refs", "contours", "transferred_to",
)
_CHOICES = {"status": STATUSES, "category": CATEGORIES, "priority": PRIORITIES, "source": SOURCES}
_ARRAYS = ("tags", "objects", "requirement_ids", "test_case_ids", "external_refs")
_NUMBER = re.compile(r"^\s*(?:ОБР-?)?0*(\d+)\s*$", re.IGNORECASE)


class IssueError(ValueError):
    """Неверные данные обращения (неизвестный статус, пустая тема…) — клиенту 422 с текстом."""


class StorageError(Exception):
    """Папка вложений недоступна для записи (чаще всего — не добавлена в ReadWritePaths службы)."""


class VersionConflict(Exception):
    """Обращение изменили после того, как клиент его открыл."""

    def __init__(self, current: dict[str, Any]):
        super().__init__("Обращение уже изменено другим аналитиком")
        self.current = current


def number(issue_id: int) -> str:
    return f"ОБР-{issue_id:04d}"


def meta(analysts: tuple[str, ...] = ()) -> dict[str, Any]:
    """Справочники для интерфейса: значения и подписи, открытые статусы, список аналитиков."""
    def pairs(d: dict[str, str]) -> list[dict[str, str]]:
        return [{"value": k, "label": v} for k, v in d.items()]

    return {"statuses": pairs(STATUSES), "open_statuses": list(OPEN_STATUSES), "categories": pairs(CATEGORIES),
            "priorities": pairs(PRIORITIES), "sources": pairs(SOURCES), "analysts": list(analysts)}


def _jsonable(v: Any) -> Any:
    if isinstance(v, datetime):
        return v.isoformat(timespec="seconds")
    if isinstance(v, date):
        return v.isoformat()
    return v


def _same(a: Any, b: Any) -> bool:
    """Совпадают ли старое и новое значение поля (даты — как даты, дробные — с допуском real)."""
    if isinstance(a, datetime) and isinstance(b, datetime):
        return a == b
    if isinstance(a, float) and isinstance(b, (int, float)):
        return abs(a - b) < 1e-6
    return _jsonable(a) == _jsonable(b)


def _out(row: dict[str, Any]) -> dict[str, Any]:
    out = {k: _jsonable(v) for k, v in row.items()}
    if "id" in row and "title" in row:
        out["number"] = number(row["id"])
        for field, labels in _CHOICES.items():
            if field in row:
                out[f"{field}_label"] = labels.get(row[field], row[field])
    return out


def _clean(changes: dict[str, Any]) -> dict[str, Any]:
    """Проверка и нормализация полей: известные значения справочников, строки без пробелов по краям,
    пустая строка = пусто (NULL), массивы — без пустых элементов и повторов."""
    unknown = set(changes) - set(EDITABLE)
    if unknown:
        raise IssueError(f"Поля нельзя менять: {', '.join(sorted(unknown))}")
    out: dict[str, Any] = {}
    for k, v in changes.items():
        if isinstance(v, str):
            v = text_safe(v).strip() or None
        if k == "contours":  # система и подсистема — id контуров
            try:
                v = list(dict.fromkeys(int(x) for x in (v or [])))
            except (TypeError, ValueError) as exc:
                raise IssueError("contours — список id контуров") from exc
        elif k in _ARRAYS:
            v = list(dict.fromkeys(text_safe(str(x)).strip() for x in (v or []) if text_safe(str(x)).strip()))
        if k in _CHOICES and v not in _CHOICES[k]:
            raise IssueError(f"Неизвестное значение {k}: {v!r}; допустимо: {', '.join(_CHOICES[k])}")
        if k == "title" and not v:
            raise IssueError("Тема обращения не может быть пустой")
        out[k] = v
    return out


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def text_safe(v: Any) -> Any:
    """Строка без управляющих символов. PostgreSQL не принимает NUL (\\x00) в text и jsonb, а в письмах
    Outlook (.msg) он встречается — хвосты строк свойств MAPI. Перевод строки и табуляция остаются."""
    return _CONTROL.sub("", v) if isinstance(v, str) else v


def parse_number(text: str) -> int | None:
    """«ОБР-0012», «обр12», «12» → 12 (поиск по номеру)."""
    m = _NUMBER.match(text or "")
    return int(m.group(1)) if m else None


class IssueRegistry:
    """Реестр обращений поверх открытого подключения psycopg (GraphStore.conn).

    Каждая операция — своя транзакция: запись обращения и событие истории фиксируются вместе.
    """

    def __init__(self, conn, project: str, files_root: Path | None = None, base: Path | None = None):
        self.conn, self.project = conn, project
        self.files_root = files_root or Path("issues")  # вложения: <files_root>/<id>/<имя>
        self.base = base

    # ---------- чтение ----------

    def _one(self, sql: str, params: tuple) -> dict[str, Any] | None:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def _all(self, sql: str, params: tuple | list) -> list[dict[str, Any]]:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def _row(self, issue_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM issues WHERE project = %s AND id = %s", (self.project, issue_id))

    def list(self, status: str | None = None, priority: str | None = None, category: str | None = None,
             assignee: str | None = None, q: str | None = None, open_only: bool = False,
             limit: int = 200, contour: int | None = None) -> list[dict[str, Any]]:
        """Строки таблицы «Обращения»: без длинных текстов, с именем инициатора и числом вложений.
        contour — система или подсистема: обращения с ней или с любой её подсистемой."""
        where, params = ["i.project = %s"], [self.project]
        if contour:
            where.append("i.contours && (SELECT array_agg(id) FROM contours WHERE id = %s OR parent_id = %s)")
            params += [contour, contour]
        for field, value in (("status", status), ("priority", priority), ("category", category),
                             ("assignee", assignee)):
            if value:
                where.append(f"i.{field} = %s")
                params.append(value)
        if open_only:
            where.append("i.status = ANY(%s)")
            params.append(list(OPEN_STATUSES))
        if q and q.strip():
            n = parse_number(q)
            like = f"%{q.strip()}%"
            cond = ("(i.title ILIKE %s OR i.description ILIKE %s OR i.error_text ILIKE %s OR c.name ILIKE %s "
                    "OR array_to_string(i.objects, ' ') ILIKE %s")
            params += [like] * 5
            if n is not None:
                cond += " OR i.id = %s"
                params.append(n)
            where.append(cond + ")")
        params.append(limit)
        rows = self._all(f"""
            SELECT i.id, i.title, i.status, i.priority, i.category, i.objects, i.assignee, i.registered_by, i.contours,
                   i.transferred_to,
                   i.source, i.reported_at, i.created_at, i.updated_at, i.due_date,
                   c.name AS initiator_name, c.organization AS initiator_org,
                   (SELECT count(*) FROM issue_attachments a WHERE a.issue_id = i.id) AS attachments
            FROM issues i LEFT JOIN contacts c ON c.id = i.initiator_contact_id
            WHERE {' AND '.join(where)}
            ORDER BY i.id DESC LIMIT %s""", params)
        self.conn.commit()
        return [_out(r) for r in rows]

    def get(self, issue_id: int) -> dict[str, Any] | None:
        """Карточка: все поля, инициатор, вложения, история (от старых к новым)."""
        row = self._row(issue_id)
        if row is None:
            self.conn.commit()
            return None
        out = _out(row)
        contact = (self._one("SELECT * FROM contacts WHERE id = %s", (row["initiator_contact_id"],))
                   if row["initiator_contact_id"] else None)
        out["initiator"] = _out(contact) if contact else None
        out["attachments"] = [_out(a) for a in self._all(
            "SELECT id, filename, mime, size, uploaded_by, uploaded_at, (extracted_text IS NOT NULL) AS has_text "
            "FROM issue_attachments WHERE issue_id = %s ORDER BY id", (issue_id,))]
        out["events"] = [_out(e) for e in self._all(
            "SELECT id, at, actor, type, field, old_value, new_value, comment FROM issue_events "
            "WHERE issue_id = %s ORDER BY at, id", (issue_id,))]  # письма — по времени отправки, не загрузки
        self.conn.commit()
        return out

    # ---------- запись ----------

    def _event(self, cur, issue_id: int, actor: str | None, type_: str, field: str | None = None,
               old: Any = None, new: Any = None, comment: str | None = None) -> None:
        cur.execute("INSERT INTO issue_events (issue_id, actor, type, field, old_value, new_value, comment) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (issue_id, actor, type_, field, Jsonb(_jsonable(old)) if old is not None else None,
                     Jsonb(_jsonable(new)) if new is not None else None, comment))

    def find_by_message_id(self, message_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM issues WHERE project = %s AND source_message_id = %s",
                        (self.project, message_id))
        self.conn.commit()
        return row

    def create(self, fields: dict[str, Any], actor: str | None = None) -> tuple[dict[str, Any], bool]:
        """Новое обращение. Возвращает (карточка, уже_было): письмо с тем же Message-ID второй раз
        не регистрируется — возвращается существующее обращение."""
        data = _clean(fields)
        if "title" not in data:
            raise IssueError("Тема обращения не может быть пустой")
        if data.get("source_message_id"):
            existing = self.find_by_message_id(data["source_message_id"])
            if existing is not None:
                return self.get(existing["id"]), True
        data.setdefault("registered_by", actor)
        if data.get("status") in ("resolved", "closed"):
            data["resolved_at"] = datetime.now().astimezone()
        cols = list(data)
        try:
            with self.conn.cursor(row_factory=dict_row) as cur:
                cur.execute(f"INSERT INTO issues (project, {', '.join(cols)}) "
                            f"VALUES (%s, {', '.join(['%s'] * len(cols))}) RETURNING id",
                            [self.project, *data.values()])
                issue_id = cur.fetchone()["id"]
                self._event(cur, issue_id, actor, "created", new={"title": data["title"],
                                                                  "status": data.get("status", "new")})
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return self.get(issue_id), False

    def update(self, issue_id: int, changes: dict[str, Any], version: int, actor: str | None = None,
               comment: str | None = None) -> dict[str, Any] | None:
        """Правка полей. version — версия, которую видел клиент; при расхождении — VersionConflict.
        В историю пишется каждое реально изменившееся поле (смена статуса — отдельным типом)."""
        data = _clean(changes)
        try:
            with self.conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM issues WHERE project = %s AND id = %s FOR UPDATE",
                            (self.project, issue_id))
                row = cur.fetchone()
                if row is None:
                    self.conn.rollback()
                    return None
                if row["version"] != version:
                    self.conn.rollback()
                    raise VersionConflict(self.get(issue_id))
                diff = {k: v for k, v in data.items() if not _same(row[k], v)}
                if "status" in diff:
                    resolved = diff["status"] in ("resolved", "closed")
                    if resolved and row["resolved_at"] is None:
                        diff["resolved_at"] = datetime.now().astimezone()
                    elif not resolved and row["resolved_at"] is not None:
                        diff["resolved_at"] = None  # обращение переоткрыто
                if diff:
                    sets = ", ".join(f"{k} = %s" for k in diff)
                    cur.execute(f"UPDATE issues SET {sets}, version = version + 1, updated_at = now() WHERE id = %s",
                                [*diff.values(), issue_id])
                    for k, v in diff.items():
                        if k != "resolved_at":
                            self._event(cur, issue_id, actor, "status" if k == "status" else "field", k,
                                        old=row[k], new=v)
                if comment and comment.strip():
                    self._event(cur, issue_id, actor, "comment", comment=comment.strip())
                    if not diff:
                        cur.execute("UPDATE issues SET updated_at = now() WHERE id = %s", (issue_id,))
            self.conn.commit()
        except VersionConflict:
            raise
        except Exception:
            self.conn.rollback()
            raise
        return self.get(issue_id)

    def add_comment(self, issue_id: int, text: str, actor: str | None = None) -> dict[str, Any] | None:
        text = text_safe(text or "")
        if not text.strip():
            raise IssueError("Пустой комментарий")
        with self.conn.cursor() as cur:
            cur.execute("UPDATE issues SET updated_at = now() WHERE project = %s AND id = %s RETURNING id",
                        (self.project, issue_id))
            if cur.fetchone() is None:
                self.conn.rollback()
                return None
            self._event(cur, issue_id, actor, "comment", comment=text.strip())
        self.conn.commit()
        return self.get(issue_id)

    def add_letter_events(self, issue_id: int, letters: list[dict[str, Any]]) -> int:
        """Каждое письмо переписки — отдельным пунктом истории (тип letter) со временем отправки письма.
        letters — строки таблицы letters (+ role). Повторно то же письмо не добавляется (по letter_id)."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT new_value->>'letter_id' FROM issue_events WHERE issue_id = %s AND type = 'letter'",
                        (issue_id,))
            have = {r[0] for r in cur.fetchall()}
            added = 0
            for x in letters:
                if str(x["id"]) in have:
                    continue
                body = text_safe((x.get("body") or "").strip())
                if x.get("inline_notes"):
                    body += "\n\n" + text_safe(x["inline_notes"])
                cur.execute("INSERT INTO issue_events (issue_id, at, actor, type, new_value, comment) "
                            "VALUES (%s, coalesce(%s, now()), %s, 'letter', %s, %s)",
                            (issue_id, x.get("sent_at"), text_safe(x.get("sender") or "?"),
                             Jsonb({"letter_id": str(x["id"]), "email": x.get("sender_email") or None,
                                    "role": x.get("role"), "subject": text_safe(x.get("subject") or ""),
                                    "thread_id": x.get("thread_id"), "origin": x.get("origin")}),
                             body[:20000]))
                added += 1
            if added:
                cur.execute("UPDATE issues SET updated_at = now() WHERE id = %s", (issue_id,))
        self.conn.commit()
        return added

    def add_attachment(self, issue_id: int, filename: str, data: bytes,
                       actor: str | None = None) -> tuple[dict[str, Any], bool] | None:
        """Сохраняет файл в <files_root>/<id>/<имя> и регистрирует. Тот же файл (по SHA-256) у того же
        обращения второй раз не сохраняется. Возвращает (вложение, уже_было) или None, если обращения нет."""
        if self._row(issue_id) is None:
            self.conn.commit()
            return None
        sha = hashlib.sha256(data).hexdigest()
        existing = self._one("SELECT id, filename, mime, size, uploaded_by, uploaded_at FROM issue_attachments "
                             "WHERE issue_id = %s AND sha256 = %s", (issue_id, sha))
        if existing is not None:
            self.conn.commit()
            return _out(existing), True
        try:
            path = _save(self.files_root / str(issue_id), filename, data)
        except OSError as exc:
            self.conn.rollback()
            raise StorageError(f"Не удалось сохранить файл в папку вложений {self.files_root.resolve()}: "
                               f"{exc.strerror or exc}. Для службы copilot1c-core папка должна быть в "
                               "ReadWritePaths (deploy/copilot1c-core.service)") from exc
        rel = path.relative_to(self.base) if self.base and path.is_relative_to(self.base) else path
        name = safe_filename(filename)
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        try:
            with self.conn.cursor(row_factory=dict_row) as cur:
                cur.execute("INSERT INTO issue_attachments (issue_id, filename, path, mime, size, sha256, uploaded_by) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                            "RETURNING id, filename, mime, size, uploaded_by, uploaded_at",
                            (issue_id, name, rel.as_posix(), mime, len(data), sha, actor))
                att = cur.fetchone()
                self._event(cur, issue_id, actor, "attachment", new={"id": att["id"], "filename": name})
                cur.execute("UPDATE issues SET updated_at = now() WHERE id = %s", (issue_id,))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            path.unlink(missing_ok=True)
            raise
        return _out(att), False

    def attachment(self, issue_id: int, attachment_id: int) -> dict[str, Any] | None:
        """Запись вложения с путём к файлу (для выдачи файла клиенту)."""
        row = self._one("SELECT a.* FROM issue_attachments a JOIN issues i ON i.id = a.issue_id "
                        "WHERE i.project = %s AND a.issue_id = %s AND a.id = %s",
                        (self.project, issue_id, attachment_id))
        self.conn.commit()
        return row

    # ---------- контакты ----------

    def contact_by_email(self, email: str | None) -> dict[str, Any] | None:
        if not email:
            return None
        row = self._one("SELECT * FROM contacts WHERE lower(email) = %s", (email.strip().lower(),))
        self.conn.commit()
        return _out(row) if row else None

    def contacts(self, q: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        like = f"%{(q or '').strip()}%"
        rows = self._all("SELECT * FROM contacts WHERE name ILIKE %s OR coalesce(email, '') ILIKE %s "
                         "OR coalesce(organization, '') ILIKE %s ORDER BY last_seen DESC, id DESC LIMIT %s",
                         (like, like, like, limit))
        self.conn.commit()
        return [_out(r) for r in rows]

    def upsert_contact(self, name: str, email: str | None = None, organization: str | None = None,
                       position: str | None = None, phone: str | None = None) -> dict[str, Any]:
        """Контакт по e-mail: если уже есть — обновляются пустые поля и last_seen, иначе создаётся.
        Без e-mail контакт создаётся всегда (сопоставлять по имени ненадёжно)."""
        name = text_safe(name or "").strip()
        email = text_safe(email or "").strip().lower() or None
        if not name and not email:
            raise IssueError("У контакта должно быть имя или e-mail")
        vals = {"organization": organization, "position": position, "phone": phone}
        vals = {k: (text_safe(v).strip() or None) if isinstance(v, str) else v for k, v in vals.items()}
        with self.conn.cursor(row_factory=dict_row) as cur:
            if email:
                cur.execute("SELECT * FROM contacts WHERE lower(email) = %s", (email,))
                row = cur.fetchone()
                if row is not None:
                    cur.execute("""UPDATE contacts SET name = CASE WHEN name = '' OR name = email THEN %s ELSE name END,
                                       organization = coalesce(organization, %s), position = coalesce(position, %s),
                                       phone = coalesce(phone, %s), last_seen = now()
                                   WHERE id = %s RETURNING *""",
                                (name or row["name"], vals["organization"], vals["position"], vals["phone"],
                                 row["id"]))
                    out = cur.fetchone()
                    self.conn.commit()
                    return _out(out)
            cur.execute("INSERT INTO contacts (name, email, organization, position, phone) "
                        "VALUES (%s, %s, %s, %s, %s) RETURNING *",
                        (name or email, email, vals["organization"], vals["position"], vals["phone"]))
            out = cur.fetchone()
        self.conn.commit()
        return _out(out)


def _save(folder: Path, filename: str, data: bytes) -> Path:
    """Как materials.save_upload, но в папку обращения, без подпапки даты."""
    folder.mkdir(parents=True, exist_ok=True)
    name = safe_filename(filename)
    path = folder / name
    n = 2
    while path.exists():
        path = folder / f"{Path(name).stem} ({n}){Path(name).suffix}"
        n += 1
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    return path
