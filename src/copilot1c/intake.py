"""«Входящие»: разбор того, что аналитик принёс в систему, и приём по его решению.

Аналитик перетаскивает файлы в чат или на экран «Входящие». Разбор (analyze) ничего не сохраняет и по каждому
файлу говорит:
- что это: вид документа (ТЗ, бизнес-требования, AS-IS / TO-BE, протокол…), название, редакция, дата, о чём;
- к чему относится: контуры из справочника (по названиям и псевдонимам в тексте; модель — если есть ключи)
  или предложение нового контура;
- отношение к базе: этот файл уже загружали; то же содержание уже есть; похоже на новую редакцию документа
  «…»; новое. Для писем — ветка, сколько писем новых и сколько уже известно, связь с обращением;
- предлагаемое действие: добавить, новая редакция, сохранить без индексации, не добавлять.

Аналитик правит и нажимает «Принять» (accept): файлы ложатся в реестр материалов с решением (materials.decision),
фоновый обработчик (worker.py) индексирует их с выбранными контурами и редакциями. Новые контуры из решения
сначала добавляются в справочник.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("copilot1c.intake")

ACTIONS = {"add": "добавить в базу", "new_version": "новая редакция документа", "archive": "сохранить без индексации",
           "skip": "не добавлять"}
TEXT_FOR_MODEL = 1500


@dataclass
class Item:
    filename: str
    sha256: str
    size: int
    kind: str = "unsupported"            # email | document | image | unsupported
    doc_kind: str = "other"
    title: str = ""
    version: str | None = None
    doc_date: str | None = None
    about: str = ""                      # о чём — одна фраза
    text_head: str = ""                  # начало текста (для модели и контуров)
    stats: dict[str, Any] = field(default_factory=dict)
    relation: dict[str, Any] = field(default_factory=lambda: {"type": "new"})
    email: dict[str, Any] | None = None
    inner: list[dict[str, Any]] = field(default_factory=list)   # документы внутри письма
    contours: list[int] = field(default_factory=list)
    new_contour: dict[str, str] | None = None
    action: str = "add"
    reason: str = ""
    notes: list[str] = field(default_factory=list)

    def out(self) -> dict[str, Any]:
        from copilot1c.documents import KINDS

        d = {k: v for k, v in self.__dict__.items() if k not in ("text_head", "sha256")}
        d["doc_kind_label"] = KINDS.get(self.doc_kind, self.doc_kind)
        d["action_label"] = ACTIONS[self.action]
        return d


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _first_sentence(text: str, limit: int = 220) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    m = re.search(r"(.{30,}?[.!?])\s", t)
    out = m.group(1) if m else t
    return out[:limit] + ("…" if len(out) > limit else "")


def _doc_info(item: Item, d, reg) -> None:
    from copilot1c.documents import content_fingerprint, guess_kind

    item.kind = "document"
    item.title = d.title[:300]
    item.version = d.version
    item.doc_date = d.doc_date.isoformat() if d.doc_date else None
    item.doc_kind = guess_kind(d.title, d.filename) if d.doc_type.value == "doc" else d.doc_type.value
    text = d.full_text()
    item.text_head = text[:TEXT_FOR_MODEL]
    # «о чём» — первый содержательный абзац, а не заголовок документа
    paras = [p.strip() for sec in d.sections for p in sec.paragraphs if len(p.strip()) >= 30]
    prose = [p for p in paras if re.search(r"[.!?;]", p) and len(p) > 40]  # заголовок — без точки и короткий
    item.about = _first_sentence((prose or paras or [text])[0])
    item.stats = {"chars": len(text), "sections": len(d.sections), "test_cases": len(d.test_cases),
                  "plan_items": len(d.plan_items), "tables": len(d.tables)}
    if reg is None:
        return
    fp = content_fingerprint(d)
    same = reg.by_fingerprint(fp)
    if same:
        item.relation = {"type": "same_content", "document_id": same["document_id"], "document_title": same["title"],
                         "version_label": same["version_label"], "filename": same["filename"],
                         "status": same["status"]}
        return
    cands = reg.similar(d.title, item.doc_kind)
    if cands:
        c = cands[0]
        item.relation = {"type": "new_version", "document_id": c["id"], "document_title": c["title"],
                         "version_label": c["version_label"], "filename": c["current_filename"],
                         "doc_date": c["doc_date"].isoformat() if c.get("doc_date") else None, "score": c["score"],
                         "candidates": [{"id": x["id"], "title": x["title"]} for x in cands]}


def analyze(files: list[tuple[str, bytes]], conn=None, settings=None, use_llm: bool = True) -> dict[str, Any]:
    """Разбор без сохранения. conn — подключение к базе (без него — только что внутри файлов)."""
    from copilot1c.contours import ContourRegistry
    from copilot1c.documents import DocumentRegistry
    from copilot1c.email_intake import is_email_file
    from copilot1c.ingest.attachments import ImageText, Skipped, parse_bytes
    from copilot1c.ingest.document import ParsedDocument
    from copilot1c.ingest.msg import ParsedEmail, walk
    from copilot1c.letters import LetterStore, results_report

    s = settings
    quick = s.model_copy(update={"ocr_backend": "none"}) if s is not None else None  # разбор — без OCR, быстро
    project = getattr(s, "project", "")
    docs = DocumentRegistry(conn, project) if conn is not None else None
    letters = LetterStore(conn, project) if conn is not None else None
    contours = ContourRegistry(conn, project) if conn is not None else None
    top_sha = {_sha(d): n for n, d in files}
    items: list[Item] = []

    for name, data in files:
        it = Item(name, _sha(data), len(data))
        if conn is not None:
            with conn.cursor() as cur:
                cur.execute("SELECT id, status, filename FROM materials WHERE project = %s AND sha256 = %s",
                            (project, it.sha256))
                row = cur.fetchone()
            if row and row[1] not in ("deleted",):
                it.relation = {"type": "already_uploaded", "material_id": row[0], "status": row[1], "filename": row[2]}
        try:
            parsed = parse_bytes(data, name, name, settings=quick)
        except Exception as exc:  # noqa: BLE001
            it.notes.append(f"не разобран: {type(exc).__name__}")
            parsed = []
        for p in parsed:
            if isinstance(p, ParsedEmail):
                it.kind, it.doc_kind = "email", "email"
                it.title = p.message.subject or name
                it.text_head = "\n\n".join(m.body for m in p.messages)[:TEXT_FOR_MODEL]
                it.about = _first_sentence(p.message.body)
                if letters is not None:
                    res = [r for part in [p] for r in letters.check(part)]
                    rep = results_report(res)
                    it.email = {**rep, "chains": [r.out() for r in res]}
                    if it.relation["type"] == "new" and rep["letters_new"] == 0 and rep["letters_known"]:
                        it.relation = {"type": "known_letters"}
                for part in walk(p):  # документы внутри письма — что это и не повтор ли отдельно приложенного
                    for att in part.attachments:
                        if att.inline:
                            continue
                        twin = top_sha.get(_sha(att.data))
                        inner = {"filename": att.filename, "same_as": twin}
                        if not twin:
                            sub = Item(att.filename, _sha(att.data), len(att.data))
                            for x in parse_bytes(att.data, att.filename, att.filename, settings=quick):
                                if isinstance(x, ParsedDocument):
                                    _doc_info(sub, x, docs)
                                    break
                            inner.update({"title": sub.title, "doc_kind": sub.doc_kind, "relation": sub.relation})
                        it.inner.append(inner)
                if is_email_file(name) and not p.messages:
                    it.notes.append("в письме нет текста")
                break
            if isinstance(p, ParsedDocument):
                _doc_info(it, p, docs)
                break
            if isinstance(p, ImageText):
                it.kind, it.doc_kind, it.title = "image", "image", name
            if isinstance(p, Skipped):
                if Path(name).suffix.lower() in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff"):
                    it.kind, it.doc_kind, it.title = "image", "image", name  # текст распознаем при обработке
                else:
                    it.notes.append(p.reason)
        if not it.title:
            it.title = name
        items.append(it)

    # контуры: по названиям и псевдонимам из справочника
    known = contours.list() if contours is not None else []
    for it in items:
        if contours is not None:
            text = " ".join([it.title, it.filename, it.text_head])
            it.contours = [c["id"] for c in contours.match(text)]
    method = "heuristic"
    if use_llm and s is not None and getattr(s, "intake_llm", True) and s.yc_api_key and s.yc_folder_id:
        try:
            _refine_with_llm(items, known, s)
            method = "llm"
        except Exception as exc:  # noqa: BLE001 — модель недоступна: остаются эвристики
            log.warning("разбор входящих моделью не удался: %s: %s", type(exc).__name__, exc)

    for it in items:
        _suggest_action(it)
    return {"items": [it.out() for it in items], "method": method,
            "contours": known, "kinds": _kinds(), "actions": ACTIONS}


def _kinds() -> dict[str, str]:
    from copilot1c.documents import KINDS

    return KINDS


def _suggest_action(it: Item) -> None:
    t = it.relation.get("type")
    if it.kind == "unsupported":
        it.action, it.reason = "skip", "; ".join(it.notes) or "формат не поддерживается"
    elif t == "already_uploaded":
        it.action = "skip"
        it.reason = f"этот файл уже загружали («{it.relation['filename']}», {it.relation['status']})"
    elif t == "same_content":
        it.action = "skip"
        it.reason = f"то же содержание уже в базе: «{it.relation['document_title']}»"
    elif t == "known_letters":
        it.action, it.reason = "skip", "все письма этого файла уже в базе"
    elif t == "new_version":
        it.action = "new_version"
        it.reason = f"похоже на новую редакцию «{it.relation['document_title']}»"
    else:
        it.action = "add"
        if it.email:
            it.reason = f"новых писем: {it.email['letters_new']}, уже известных: {it.email['letters_known']}"
        else:
            it.reason = "нового документа в базе нет"


LLM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
        "index": {"type": "integer"},
        "kind": {"type": "string", "enum": []},  # заполняется при вызове
        "about": {"type": "string", "description": "О чём материал — одна фраза по-русски"},
        "contour_ids": {"type": "array", "items": {"type": "integer"},
                        "description": "id подходящих контуров из списка; пусто — ни один не подходит"},
        "new_contour_kind": {"type": "string", "enum": ["system", "process", "project"]},
        "new_contour_name": {"type": "string", "description": "Если ни один контур не подходит — короткое название"},
    }, "required": ["index", "kind", "about", "contour_ids"]}}},
    "required": ["items"],
}
LLM_SYSTEM = ("Ты разбираешь материалы, которые аналитик ИТ-отдела принёс в базу знаний по системам 1С (переписка, "
              "ТЗ, бизнес-требования, описания процессов, протоколы, скриншоты). Для каждого материала определи вид, "
              "в одной фразе — о чём он, и к каким контурам из справочника относится (система 1С, процесс или тема, "
              "проект). Если ни один не подходит, предложи один новый контур с коротким названием. Не выдумывай id.")


def _refine_with_llm(items: list[Item], known: list[dict], s) -> None:
    from copilot1c.documents import KINDS
    from copilot1c.index.yandex import chat_json

    schema = {**LLM_SCHEMA}
    item_schema = schema["properties"]["items"]["items"]
    item_schema["properties"] = {**item_schema["properties"], "kind": {"type": "string", "enum": list(KINDS)}}
    ref = "\n".join(f"{c['id']}: {c['kind_label']} «{c['name']}»" + (f" ({', '.join(c['aliases'])})" if c["aliases"] else "")
                    for c in known) or "(справочник пуст)"
    parts = []
    for i, it in enumerate(items):
        if it.kind == "unsupported":
            continue
        parts.append(f"[{i}] файл «{it.filename}»; заголовок: {it.title}\n{it.text_head[:TEXT_FOR_MODEL]}")
    if not parts:
        return
    prompt = f"Справочник контуров:\n{ref}\n\nМатериалы:\n\n" + "\n\n".join(parts)
    res = chat_json(prompt, schema, model=s.model_batch, system=LLM_SYSTEM, settings=s)
    ids = {c["id"] for c in known}
    for r in res.get("items", []):
        i = r.get("index")
        if not isinstance(i, int) or not 0 <= i < len(items):
            continue
        it = items[i]
        if r.get("kind") in KINDS and it.kind != "email":
            it.doc_kind = r["kind"]
        if r.get("about"):
            it.about = r["about"].strip()[:300]
        chosen = [c for c in r.get("contour_ids") or [] if c in ids]
        it.contours = list(dict.fromkeys(it.contours + chosen))
        if not it.contours and r.get("new_contour_name") and r.get("new_contour_kind"):
            it.new_contour = {"kind": r["new_contour_kind"], "name": r["new_contour_name"].strip()[:200]}


# ---------- приём ----------

def accept(files: list[tuple[str, bytes]], decisions: list[dict[str, Any]], conn, settings,
           base: Path | None = None) -> dict[str, Any]:
    """Файлы с решениями аналитика → реестр материалов (в очередь обработки или «без индексации»).
    decisions — по имени файла: {filename, action, contours, document_id, kind, new_contours: [{kind, name}]}.
    Новые контуры создаются (или находятся по имени) и добавляются к решению."""
    from copilot1c.contours import ContourError, ContourRegistry
    from copilot1c.email_intake import is_email_file
    from copilot1c.materials import MaterialRegistry, register_upload, row_out

    by_name = {d.get("filename"): d for d in decisions}
    contours = ContourRegistry(conn, settings.project)
    reg = MaterialRegistry(conn, settings.project)
    created: dict[tuple[str, str], int] = {}
    out = []
    # документы — раньше писем: если тот же документ пришёл и вложением письма, его редакцией станет отдельный файл
    ordered = sorted(files, key=lambda f: is_email_file(f[0]))
    for name, data in ordered:
        d = by_name.get(name) or {"action": "add"}
        action = d.get("action", "add")
        if action not in ACTIONS:
            out.append({"filename": name, "error": f"неизвестное действие: {action}"})
            continue
        if action == "skip":
            out.append({"filename": name, "skipped": True})
            continue
        ids = [int(x) for x in d.get("contours") or [] if str(x).isdigit()]
        for nc in d.get("new_contours") or []:
            key = (nc.get("kind", ""), (nc.get("name") or "").strip())
            if not key[1]:
                continue
            if key not in created:
                try:
                    created[key] = contours.create({"kind": key[0], "name": key[1]})["id"]
                except ContourError as exc:
                    existing = next((c for c in contours.list(include_inactive=True)
                                     if c["kind"] == key[0] and c["name"].casefold() == key[1].casefold()), None)
                    if existing is None:
                        out.append({"filename": name, "error": f"контур «{key[1]}»: {exc}"})
                        continue
                    created[key] = existing["id"]
            ids.append(created[key])
        decision = {"action": action, "contours": list(dict.fromkeys(ids)), "kind": d.get("kind"),
                    "document_id": d.get("document_id") if action == "new_version" else None,
                    "title": d.get("title")}
        status = "archived" if action == "archive" else "queued"
        row, seen = register_upload(reg, Path(settings.materials_dir), name, data, base=base, decision=decision,
                                    status=status)
        if seen:  # файл уже был: решение по контурам — к его фрагментам и веткам сразу
            from psycopg.types.json import Jsonb

            union = "(SELECT array(SELECT DISTINCT x FROM unnest(contours || %s::bigint[]) x ORDER BY x))"
            with conn.cursor() as cur:
                cur.execute(f"UPDATE chunks SET contours = {union} WHERE material_id = %s",
                            (decision["contours"], row["id"]))
                cur.execute(f"UPDATE threads SET contours = {union} WHERE id IN (SELECT thread_id FROM letters "
                            "WHERE material_id = %s)", (decision["contours"], row["id"]))
                cur.execute("UPDATE materials SET decision = decision || %s WHERE id = %s",
                            (Jsonb({"contours": decision["contours"]}), row["id"]))
            conn.commit()
        out.append({**row_out(reg.get(row["id"])), "already_uploaded": seen})
    return {"materials": out, "contours_created": [{"kind": k, "name": n, "id": i} for (k, n), i in created.items()]}


def agent_block(analysis: dict[str, Any]) -> str:
    """Разбор приложенного — для агента: по строке на файл (что это, отношение к базе, предложение)."""
    names = {c["id"]: c["name"] for c in analysis.get("contours", [])}
    lines = []
    for it in analysis.get("items", []):
        rel = it["relation"]
        rel_text = {"already_uploaded": f"этот файл уже загружен («{rel.get('filename')}»)",
                    "same_content": f"то же содержание уже в базе: «{rel.get('document_title')}»",
                    "new_version": f"похоже на новую редакцию «{rel.get('document_title')}»"
                                   + (f" (сейчас ред. {rel['version_label']})" if rel.get("version_label") else ""),
                    "known_letters": "все письма уже в базе"}.get(rel["type"], "в базе нет")
        cont = ", ".join(names.get(c, str(c)) for c in it.get("contours") or [])
        if not cont and it.get("new_contour"):
            cont = f"новый контур? {it['new_contour']['name']}"
        email = ""
        if it.get("email"):
            email = f"; писем новых {it['email']['letters_new']}, известных {it['email']['letters_known']}"
            for t in it["email"].get("threads", []):
                email += f", ветка «{t['title']}»" + (f" (обращение ОБР-{t['issue_id']:04d})" if t.get("issue_id") else "")
        inner = [x for x in it.get("inner", []) if x.get("same_as")]
        dup = "; вложения, приложенные и отдельно: " + ", ".join(x["filename"] for x in inner) if inner else ""
        lines.append(f"- «{it['filename']}»: {it['doc_kind_label']}, «{it['title']}»"
                     + (f", ред. {it['version']}" if it.get("version") else "")
                     + (f" — {it['about']}" if it.get("about") else "")
                     + f". В базе: {rel_text}{email}{dup}. Контуры: {cont or 'не определены'}. "
                     + f"Предложение: {it['action_label']}.")
    return "\n".join(lines)


INTAKE_TASK = ("Аналитик просит разобрать приложенные материалы для базы знаний системы. Ниже — автоматический "
               "разбор каждого файла. Ответь коротко: по каждому файлу — что это и о чём, к чему относится, есть ли "
               "уже в базе (дубль, новая редакция, новые письма в известной ветке) и что ты рекомендуешь; отметь "
               "противоречия между документами, если видишь. Добавляет материалы аналитик кнопкой «Принять» в "
               "карточке под ответом — не давай инструкций, как загружать файлы в 1С, Outlook или другие программы.")
