"""Письмо → черновик обращения: кто инициатор, когда сообщил, тема, текст, файлы.

Аналитики и пользователи — сотрудники одной организации (свои домены, COPILOT_INTERNAL_DOMAINS), поэтому
по домену их не отличить. Аналитиков узнаём по адресам (COPILOT_ANALYST_EMAILS) и, запасным путём, по
фамилии из COPILOT_ANALYSTS.

Как выбирается инициатор:
1. Из файла собирается вся цепочка: само письмо, процитированные ниже письма (thread.split_thread —
   тот же разбор, что при индексации) и письма, вложенные файлом (.msg в .msg, message/rfc822), рекурсивно.
2. Цепочка упорядочивается по времени (письма без даты — по месту в цепочке: чем глубже, тем раньше).
3. Инициатор — самый поздний автор из своих доменов, который не аналитик: тот, кто обратился к аналитику.
   Не самый ранний — в длинной цепочке внизу бывает старая переписка (согласование ТЗ, рассылка об
   обновлении), её авторы к проблеме отношения не имеют. Если сотрудников нет — самый поздний автор без
   адреса (только имя в цитате) или внешний, с пометкой «проверьте». Аналитик в карточке видит всю
   цепочку и может выбрать другого человека.

Должность и телефон берутся из подписи письма инициатора (эвристика по строкам после «С уважением»);
это подсказка для контакта, аналитик её проверяет.

Дубли: ключ письма — Message-ID письма, в котором инициатор автор (если это письмо-файл), иначе
отпечаток «адрес + время + тема». Одно и то же письмо пользователя, пересланное двумя аналитиками,
второй раз не регистрируется.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import re
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from copilot1c.ingest.cleaning import (
    _DISCLAIMER_START_RE,
    _PHONE_RE,
    _SIGNATURE_RE,
    clean_email_text,
    dedent_quoted,
    normalize_whitespace,
    unwrap_safelinks,
)
from copilot1c.ingest.msg import thread_id
from copilot1c.ingest.thread import split_address, split_thread

EMAIL_SUFFIXES = (".msg", ".eml")
MIN_INLINE_IMAGE = 8 * 1024  # встроенные картинки меньше — логотипы подписей, не скриншоты
ROLE_LABELS = {"analyst": "аналитик", "internal": "сотрудник", "external": "внешний", "unknown": "без адреса"}
ORIGIN_LABELS = {"file": "письмо", "quoted": "цитата", "nested": "вложенное письмо"}
_INTL_PHONE_RE = re.compile(r"\+\d[\d\s().-]{8,}\d")
_ORG_RE = re.compile(r"\b(ООО|АО|ЗАО|ПАО|LLC|Ltd|GmbH|S\.?A\.?S?|Inc)\b|Pierre\s+Fabre|Пьер\s+Фабр", re.IGNORECASE)
_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
# Строка-имя в начале подписи: 2–3 слова с заглавной (имя в заголовке бывает латиницей, а в подписи кириллицей)
_NAME_LINE_RE = re.compile(r"^[A-ZА-ЯЁ][a-zа-яё'’-]+(?:\s+[A-ZА-ЯЁ][a-zа-яё'’.-]*){1,2}$")


@dataclass
class ChainMessage:
    name: str
    email: str
    date: datetime | None
    subject: str
    raw: str                       # текст письма без следующих цитат, до очистки (нужна подпись)
    origin: str                    # file | quoted | nested
    position: int                  # место в цепочке: чем больше, тем глубже (раньше)
    message_id: str | None = None  # только у писем-файлов
    role: str = "unknown"


@dataclass
class Intake:
    chain: list[ChainMessage]                  # от ранних к поздним
    initiator: ChainMessage | None
    confidence: str                            # high | low | none
    reason: str
    files: list[tuple[str, bytes]] = field(default_factory=list)
    top_message_id: str | None = None


def is_email_file(name: str) -> bool:
    return Path(name).suffix.lower() in EMAIL_SUFFIXES


# ---------- сбор цепочки ----------

def _as_aware(d: datetime | None, tz) -> datetime | None:
    if d is None:
        return None
    if d.tzinfo is None:
        return d.replace(tzinfo=tz or UTC)
    return d


def _letter(out: list[ChainMessage], files: list[tuple[str, bytes]], *, subject: str, sender: str, date,
            body: str, message_id: str | None, origin: str) -> None:
    """Письмо-файл и его цитаты — в цепочку. Управляющие символы (NUL из свойств MAPI) убираются сразу."""
    from copilot1c.issues import text_safe

    subject, sender, body = text_safe(subject or ""), text_safe(sender or ""), text_safe(body or "")
    message_id = text_safe(message_id) if message_id else None
    tz = date.tzinfo if date else None
    own, quoted = split_thread(body or "", tz)
    name, addr = split_address(sender or "")
    out.append(ChainMessage(name, addr, _as_aware(date, tz), subject, own, origin, len(out), message_id))
    for q in quoted:
        out.append(ChainMessage(q.sender, q.sender_email, _as_aware(q.date, tz), q.subject or subject, q.text,
                                "quoted", len(out)))


def _mapi_sender(msg) -> str:
    """Отправитель .msg; у внутренних писем Exchange вместо адреса бывает X500 (/O=…) — тогда SMTP из свойств."""
    sender = msg.sender or ""
    if "@" in sender and "/o=" not in sender.lower():
        return sender
    for stream in ("__substg1.0_5D01", "__substg1.0_5D02", "__substg1.0_0C1F", "__substg1.0_0065"):
        addr = msg.getStringStream(stream)
        if addr and "@" in addr and not addr.startswith("/"):
            name = re.sub(r"<[^>]*>", "", sender).strip() or msg.getStringStream("__substg1.0_0C1A") or ""
            return f"{name} <{addr}>"
    return sender


def _walk_msg(msg, out: list[ChainMessage], files: list[tuple[str, bytes]], origin: str) -> None:
    from extract_msg.msg_classes import MessageBase

    date = msg.date if isinstance(msg.date, datetime) else None
    _letter(out, files, subject=msg.subject or "", sender=_mapi_sender(msg), date=date, body=msg.body or "",
            message_id=(msg.messageId or "").strip() or None, origin=origin)
    for i, att in enumerate(msg.attachments):
        data = att.data
        if isinstance(data, MessageBase):
            _walk_msg(data, out, files, "nested")
        elif isinstance(data, bytes) and data:
            name = att.longFilename or att.shortFilename or att.displayName or f"вложение_{i}"
            hidden = bool(getattr(att, "hidden", False))
            if not hidden or len(data) >= MIN_INLINE_IMAGE:
                files.append((name, data))


def _walk_eml(m, out: list[ChainMessage], files: list[tuple[str, bytes]], origin: str) -> None:
    from copilot1c.ingest.msg import _eml_body

    try:
        date = email.utils.parsedate_to_datetime(m["Date"]) if m["Date"] else None
    except (TypeError, ValueError):
        date = None
    _letter(out, files, subject=str(m["Subject"] or ""), sender=str(m["From"] or ""), date=date, body=_eml_body(m),
            message_id=(str(m["Message-ID"] or "").strip() or None), origin=origin)
    for i, part in enumerate(m.iter_attachments()):
        if part.get_content_type() == "message/rfc822":
            inner = part.get_payload()[0] if part.is_multipart() else part.get_content()
            _walk_eml(inner, out, files, "nested")
            continue
        data = part.get_payload(decode=True) or b""
        if not data:
            continue
        inline = part.get_content_maintype() == "image" and (
            (part.get("Content-Disposition") or "").lower().startswith("inline") or bool(part.get("Content-ID")))
        if not inline or len(data) >= MIN_INLINE_IMAGE:
            files.append((part.get_filename() or f"вложение_{i}", data))


def read_chain(filename: str, data: bytes) -> tuple[list[ChainMessage], list[tuple[str, bytes]]]:
    """Цепочка писем файла в порядке появления (сверху вниз) и вложенные файлы."""
    out: list[ChainMessage] = []
    files: list[tuple[str, bytes]] = []
    if Path(filename).suffix.lower() == ".msg":
        import extract_msg

        with tempfile.NamedTemporaryFile(suffix=".msg") as tmp:  # extract_msg надёжнее всего читает файл с диска
            tmp.write(data)
            tmp.flush()
            msg = extract_msg.openMsg(tmp.name)
            try:
                _walk_msg(msg, out, files, "file")
            finally:
                msg.close()
    else:
        _walk_eml(email.message_from_bytes(data, policy=email.policy.default), out, files, "file")
    return out, files


# ---------- выбор инициатора ----------

def _surname(name: str) -> str:
    return (re.split(r"[\s,]+", name.strip())[0] if name.strip() else "").casefold().strip(".")


def classify(m: ChainMessage, internal_domains: tuple[str, ...], analyst_emails: tuple[str, ...],
             analysts: tuple[str, ...]) -> str:
    addr = m.email.lower()
    if addr and addr in {a.lower() for a in analyst_emails}:
        return "analyst"
    if m.name and _surname(m.name) and _surname(m.name) in {_surname(a) for a in analysts}:
        return "analyst"
    if not addr:
        return "unknown"
    domain = addr.rsplit("@", 1)[1]
    if any(domain == d.lower() or domain.endswith("." + d.lower()) for d in internal_domains):
        return "internal"
    return "external"


def chronological(chain: list[ChainMessage]) -> list[ChainMessage]:
    """По времени; без даты — по глубине в цепочке (глубже = раньше), после датированных того же места."""
    far = datetime.max.replace(tzinfo=UTC)
    return sorted(chain, key=lambda m: (m.date or far, -m.position))


def analyze(filename: str, data: bytes, internal_domains: tuple[str, ...] = (),
            analyst_emails: tuple[str, ...] = (), analysts: tuple[str, ...] = ()) -> Intake:
    raw_chain, files = read_chain(filename, data)
    # одно письмо приходит файлом и цитатой — оставляем по одному на автора и минуту
    seen: set[tuple[str, str]] = set()
    chain = []
    for m in sorted(raw_chain, key=lambda x: x.origin == "quoted"):  # письма-файлы приоритетнее цитат
        key = ((m.email or _surname(m.name)), f"{m.date:%Y-%m-%d %H:%M}" if m.date else f"#{m.position}")
        if key in seen:
            continue
        seen.add(key)
        m.role = classify(m, internal_domains, analyst_emails, analysts)
        chain.append(m)
    chain = chronological(chain)
    top_id = next((m.message_id for m in raw_chain if m.origin == "file"), None)

    latest = list(reversed(chain))
    pick = next((m for m in latest if m.role == "internal"), None)
    if pick is not None:
        return Intake(chain, pick, "high", "последний автор цепочки из своей организации, не аналитик", files, top_id)
    for role, why in (("unknown", "в цепочке нет сотрудников с адресом — выбран последний автор без адреса"),
                      ("external", "в цепочке нет сотрудников организации — выбран последний внешний автор")):
        pick = next((m for m in latest if m.role == role), None)
        if pick is not None:
            return Intake(chain, pick, "low", why + "; проверьте", files, top_id)
    why = "все авторы цепочки — аналитики" if chain else "в файле не найдено писем"
    return Intake(chain, None, "none", why + "; выберите инициатора вручную", files, top_id)


# ---------- подпись: должность, телефон, организация ----------

def signature(m: ChainMessage) -> dict[str, str | None]:
    """Должность, телефон и организация из подписи письма (эвристика; аналитик проверяет)."""
    text = normalize_whitespace(unwrap_safelinks(dedent_quoted(m.raw or "")))
    end = _DISCLAIMER_START_RE.search(text)
    if end:
        text = text[: end.start()]
    start = _SIGNATURE_RE.search(text)
    # «С уважением,» — остаток строки после совпадения («,») не строка подписи
    lines = [ln.strip(" ,;|") for ln in (text[start.end():] if start else text).split("\n")]
    lines = [ln for ln in lines if ln]
    lines = lines[:10] if start else lines[-8:]
    name_parts = {p.casefold().strip(".,") for p in m.name.split() if len(p.strip(".,")) > 2}
    phone = position = org = None
    for i, ln in enumerate(lines):
        low = ln.casefold()
        p = _PHONE_RE.search(ln) or _INTL_PHONE_RE.search(ln)
        if p:
            phone = phone or p.group(0).strip()
            continue
        if "@" in ln or _URL_RE.search(ln) or len(ln) > 90:
            continue
        if (any(p in low for p in name_parts) and len(ln.split()) <= 5) or (start and i < 2 and _NAME_LINE_RE.match(ln)):
            continue  # строка с именем
        if _ORG_RE.search(ln):
            org = org or ln
            continue
        if position is None and not re.search(r"\d", ln) and start:
            position = ln
    return {"position": position, "phone": phone, "organization": org}


# ---------- черновик обращения ----------

def _iso(d: datetime | None) -> str | None:
    return d.isoformat(timespec="seconds") if d else None


def message_key(intake: Intake) -> str | None:
    """Ключ для защиты от повторной регистрации того же письма."""
    m = intake.initiator
    if m is not None and m.message_id:
        return m.message_id
    if m is not None:
        raw = f"{m.email or m.name}|{m.date:%Y-%m-%dT%H:%M}|{thread_id(m.subject)}" if m.date else \
            f"{m.email or m.name}|{thread_id(m.subject)}|{clean_email_text(m.raw, mask=False)[:200]}"
        return "<quote-" + hashlib.sha1(raw.encode()).hexdigest()[:20] + ">"
    return intake.top_message_id


def proposal(filename: str, intake: Intake) -> dict[str, Any]:
    """Ответ API: черновик полей, инициатор с подсказками из подписи, вся цепочка, файлы."""
    m = intake.initiator
    base = m or (intake.chain[0] if intake.chain else None)
    draft: dict[str, Any] = {"source": "email", "source_ref": filename, "source_message_id": message_key(intake)}
    if base is not None:
        draft["title"] = thread_id(base.subject) or None
        draft["description"] = clean_email_text(base.raw, mask=False) or None
        draft["reported_at"] = _iso(base.date)
    initiator = None
    if m is not None:
        initiator = {"name": m.name, "email": m.email or None, "role": m.role, "date": _iso(m.date), **signature(m)}
    chain = [{"index": i, "name": c.name, "email": c.email or None, "date": _iso(c.date), "role": c.role,
              "role_label": ROLE_LABELS[c.role], "origin": c.origin, "origin_label": ORIGIN_LABELS[c.origin],
              "subject": c.subject, "chosen": c is m,
              "excerpt": clean_email_text(c.raw, mask=False)[:300],
              "signature": signature(c) if c.role != "analyst" else None}
             for i, c in enumerate(intake.chain)]
    return {"draft": draft, "initiator": initiator, "confidence": intake.confidence, "reason": intake.reason,
            "chain": chain, "files": [{"filename": n, "size": len(d)} for n, d in intake.files]}
