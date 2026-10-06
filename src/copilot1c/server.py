"""Демон ядра: HTTP API на localhost для веба, бота и MCP (copilot1c serve).

Слушает только 127.0.0.1 — наружу ядро не выставляется, клиенты ходят через веб-часть. Секреты
(.env с ключом AI Studio, DSN PostgreSQL) остаются у демона.

Методы появляются по шагам; сейчас:
  GET /health — что видит ядро: настройки AI Studio, индекс и его манифест, PostgreSQL, платформа 1С.
  POST /ask  — вопрос агенту: ответ, найденные фрагменты-источники, шаги агента. Формат ответа совпадает
               с /api/ask веб-части, чтобы веб проксировал запрос без изменений интерфейса. Плюс intent — тип
               сообщения (вопрос, проблема, сводка, документ, знание) и issue_draft — черновик обращения, если
               это сообщение о проблеме (intent.py; ничего не сохраняется).
  POST /classify — только тип сообщения и черновик обращения, без агента.
  POST /ask/files — вопрос с файлами («+» в чате, multipart: question, files): текст писем, документов и
               картинок идёт агенту контекстом этого вопроса (в базу не сохраняется, chat_files.py); для
               письма о проблеме черновик обращения дополняется инициатором, датой и Message-ID.
  POST /materials — загрузить файлы (multipart, поле files): сохраняются в data/uploads, попадают в реестр.
  GET  /materials, GET /materials/{id} — реестр загруженных материалов и их статусы. Обработку
               (разбор → индексация → запись в базу) ведёт фоновый поток, см. worker.py.
  Обращения (issues.py) — проблемы, которые заводят аналитики:
  GET   /issues/meta — справочники (статусы, категории, приоритеты) и список аналитиков;
  GET   /issues — список с фильтрами (status, priority, category, assignee, open, q);
  POST  /issues — новое обращение (JSON); GET /issues/{id} — карточка с вложениями и историей;
  PATCH /issues/{id} — правка полей с версией (409 при одновременной правке) и комментарием;
  POST  /issues/{id}/comments, POST /issues/{id}/attachments (multipart files),
  GET   /issues/{id}/attachments/{aid} — файл вложения;
  GET   /issues/{id}/related, POST /issues/related (черновик) — похожие обращения (дубли) и связанные
        тест-кейсы ПиМИ и пункты ТЗ (related.py);
  POST  /issues/{id}/kb-draft — разбор решённого обращения для базы знаний (kb.py), не сохраняется;
  POST  /issues/{id}/kb-publish — разбор → материал «Решение ОБР-… — тема.md» (дальше — конвейер «Материалов»);
  POST  /issues/from-email — разбор письма (.msg/.eml) в черновик: инициатор, дата, тема, текст, цепочка;
        ничего не сохраняет. POST /issues/{id}/attachments с expand=true — письмо прикрепляется вместе
        с файлами, вложенными в него (скриншоты, логи);
  GET   /contacts?q=, POST /contacts — инициаторы обращений (сотрудники заказчика).
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from copilot1c import __version__
from copilot1c.config import Settings, get_settings


def _check_postgres(s: Settings) -> dict[str, Any]:
    from copilot1c.graph.store import try_connect

    g = try_connect(s)
    if g is None:
        return {"ok": False, "detail": "нет подключения"}
    try:
        tables = g.query("SELECT count(*) AS n FROM information_schema.tables WHERE table_schema = 'public'")
        out: dict[str, Any] = {"ok": True, "tables": tables[0]["n"] if tables else 0}
        try:  # счётчики для веба; без схемы (до init-db) — только число таблиц
            out.update(g.query("SELECT (SELECT count(*) FROM chunks) AS chunks, (SELECT count(*) FROM test_cases) "
                               "AS test_cases, (SELECT count(*) FROM requirements) AS requirements")[0])
        except Exception as exc:  # noqa: BLE001
            out["detail"] = f"схема не создана (copilot1c init-db): {type(exc).__name__}"
        return out
    except Exception as exc:  # noqa: BLE001 — health не должен падать из-за схемы
        return {"ok": False, "detail": f"{type(exc).__name__}"}
    finally:
        g.close()


def _manifest_chunks(path: Path) -> int:
    try:
        return len(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return 0


def _check_writable(folder: Path) -> dict[str, Any]:
    """Можно ли писать вложения обращений: папка создаётся, пробный файл пишется и удаляется."""
    import os

    path = folder if folder.is_absolute() else Path.cwd() / folder
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write-test-{os.getpid()}"
        probe.write_bytes(b"")
        probe.unlink()
        return {"ok": True, "path": str(path), "optional": True}
    except OSError as exc:
        return {"ok": False, "path": str(path), "optional": True, "detail": exc.strerror or str(exc)}


def health_report(s: Settings) -> dict[str, Any]:
    from copilot1c.ingest.ocr import backend

    manifest = Path(s.cache_dir or ".cache") / "vector_store" / f"{s.vector_store_id}.json"
    has_manifest = manifest.exists() if s.vector_store_id else False
    checks = {
        "ai_studio": {"ok": bool(s.yc_api_key and s.yc_folder_id), "folder": s.yc_folder_id or None,
                      "model": s.model_orchestrator},
        "vector_store": {"ok": bool(s.vector_store_id), "id": s.vector_store_id or None,
                         "manifest": has_manifest, "chunks": _manifest_chunks(manifest) if has_manifest else 0},
        "postgres": {**_check_postgres(s), "host": s.pg_dsn.rsplit("@", 1)[-1]},  # без логина и пароля
        "platform_1c": {"ok": Path(s.onec_bin).exists(), "optional": True},
        "ocr": {"ok": True, "backend": backend(s), "optional": True},
        "issues_files": _check_writable(Path(s.issues_dir)),
    }
    required_ok = all(checks[k]["ok"] for k in ("ai_studio", "vector_store", "postgres"))
    return {"status": "ok" if required_ok else "degraded", "version": __version__, "project": s.project,
            "checks": checks}


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)


class ClassifyRequest(BaseModel):
    text: str = Field(min_length=2, max_length=20000)
    has_files: bool = False
    use_llm: bool = True


def classify_message(s: Settings, text: str, has_files: bool = False, use_llm: bool = True) -> dict[str, Any]:
    """Тип сообщения и, для проблемы, черновик обращения. Ошибка классификатора не ломает ответ агенту."""
    from copilot1c.intent import classify, issue_draft

    try:
        intent = classify(text, has_files, settings=s, use_llm=use_llm)
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger("copilot1c.intent").exception("классификация сообщения")
        return {"intent": {"primary": "question", "error": f"{type(exc).__name__}: {exc}"[:300]}, "issue_draft": None}
    return {"intent": intent.to_dict(), "issue_draft": issue_draft(text) if intent.is_issue else None}


def search_sources(s: Settings, question: str, k: int = 8) -> list[dict[str, Any]]:
    """Те же фрагменты, что агент получает перед ответом (тот же запрос и тот же поиск ядра)."""
    from copilot1c.index.yandex import VectorIndex
    from copilot1c.retrieval import smart_search, source_label

    index = VectorIndex(s.vector_store_id, s)
    hits = smart_search(lambda q, f, kk: index.search(q, filters=f, k=kk), question, {"project": s.project}, k)
    out = []
    for i, h in enumerate(hits, 1):
        attrs = h.get("attributes") or {}
        out.append({"n": i, "label": source_label(attrs, h.get("text", "")), "doc_type": attrs.get("doc_type", ""),
                    "date": attrs.get("date", ""), "text": h.get("text", "")})
    return out


def run_question(s: Settings, question: str, attached: str = "", search_query: str | None = None):
    from copilot1c.agent.tools import ToolContext, run_agent
    from copilot1c.graph.store import try_connect

    store = try_connect(s)
    try:
        ctx = ToolContext(s, s.vector_store_id, Path("data/dumps"), store)
        if attached or search_query:
            return run_agent(question, ctx, attached=attached, search_query=search_query)
        return run_agent(question, ctx)
    finally:
        if store is not None:
            store.close()


def _registry(s: Settings):
    """Подключение к PostgreSQL и реестр материалов; 503, если базы нет."""
    from copilot1c.graph.store import try_connect
    from copilot1c.materials import MaterialRegistry

    g = try_connect(s)
    if g is None:
        raise HTTPException(503, "PostgreSQL недоступен — реестр материалов не работает")
    return g, MaterialRegistry(g.conn, s.project)


class IssueFields(BaseModel):
    """Поля обращения, которые задаёт клиент. Типы проверяет pydantic, значения справочников — issues.py."""
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(None, max_length=500)
    description: str | None = None
    summary: str | None = None
    error_text: str | None = None
    steps: str | None = None
    expected: str | None = None
    actual: str | None = None
    category: str | None = None
    priority: str | None = None
    status: str | None = None
    tags: list[str] | None = None
    assignee: str | None = None
    due_date: date | None = None
    infobase: str | None = None
    server: str | None = None
    config_version: str | None = None
    platform_version: str | None = None
    objects: list[str] | None = None
    initiator_contact_id: int | None = None
    reported_at: datetime | None = None
    registered_by: str | None = None
    source: str | None = None
    source_ref: str | None = None
    source_message_id: str | None = None
    classifier_confidence: float | None = None
    duplicate_of: int | None = None
    requirement_ids: list[str] | None = None
    test_case_ids: list[str] | None = None
    root_cause: str | None = None
    resolution: str | None = None
    kb_material_id: int | None = None


class IssueCreate(IssueFields):
    title: str = Field(min_length=1, max_length=500)
    actor: str | None = None  # аналитик, от имени которого запись (идёт в историю и в registered_by)


class IssuePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int
    changes: IssueFields = Field(default_factory=IssueFields)
    comment: str | None = None
    actor: str | None = None


class RelatedIn(BaseModel):
    title: str | None = None
    description: str | None = None
    error_text: str | None = None
    objects: list[str] = Field(default_factory=list)


class KbDraftIn(BaseModel):
    use_llm: bool = True


class KbPublishIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=50000)
    actor: str | None = None


class CommentIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)
    actor: str | None = None


class ContactIn(BaseModel):
    name: str = ""
    email: str | None = None
    organization: str | None = None
    position: str | None = None
    phone: str | None = None


def iss_number(issue_id: int) -> str:
    from copilot1c.issues import number

    return number(issue_id)


def _email_for_issue(s: Settings, raw: list[tuple[str, bytes]]) -> dict[str, Any] | None:
    """Первое приложенное письмо → поля черновика обращения: тема, текст, дата, инициатор, Message-ID,
    найденный контакт и «уже зарегистрировано». Ошибка разбора — без этих полей, ответ не ломается."""
    from copilot1c.email_intake import analyze, is_email_file, proposal

    first = next(((n, d) for n, d in raw if is_email_file(n)), None)
    if first is None:
        return None
    try:
        p = proposal(first[0], analyze(first[0], first[1], s.internal_domains, s.analyst_emails, s.analysts))
    except Exception as exc:  # noqa: BLE001
        import logging

        logging.getLogger("copilot1c.chat_files").warning("письмо не разобрано для обращения: %s", exc)
        return None
    out = {k: v for k, v in p["draft"].items() if v}
    out["initiator"] = p["initiator"]
    out["contact"] = None
    out["already_registered"] = None
    from copilot1c.graph.store import try_connect

    g = try_connect(s)
    if g is not None:
        from copilot1c.issues import IssueRegistry, number

        try:
            reg = IssueRegistry(g.conn, s.project)
            if p["initiator"]:
                out["contact"] = reg.contact_by_email(p["initiator"]["email"])
            row = reg.find_by_message_id(out["source_message_id"]) if out.get("source_message_id") else None
            if row is not None:
                out["already_registered"] = {"id": row["id"], "number": number(row["id"]), "title": row["title"]}
        finally:
            g.close()
    return out


def _issues(s: Settings):
    """Подключение к PostgreSQL и реестр обращений; 503, если базы нет."""
    from copilot1c.graph.store import try_connect
    from copilot1c.issues import IssueRegistry

    g = try_connect(s)
    if g is None:
        raise HTTPException(503, "PostgreSQL недоступен — обращения не работают")
    return g, IssueRegistry(g.conn, s.project, Path(s.issues_dir), base=Path.cwd())


def create_app(settings: Settings | None = None, start_worker: bool = False) -> FastAPI:
    """start_worker — запустить фоновую обработку загруженных материалов (так делает copilot1c serve)."""
    from contextlib import asynccontextmanager

    from copilot1c.worker import MaterialWorker

    s = settings or get_settings()
    worker = MaterialWorker(s)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_worker:
            worker.start()
        yield
        if worker.running:
            worker.stop()

    app = FastAPI(title="1С Project Copilot — ядро", version=__version__, lifespan=lifespan)
    app.state.worker = worker

    @app.exception_handler(Exception)
    async def unexpected(request, exc: Exception):
        """Непредвиденная ошибка: трассировка — в журнал службы, клиенту — причина текстом вместо голого 500."""
        import logging

        from fastapi.responses import JSONResponse

        logging.getLogger("copilot1c.server").exception("%s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content={
            "detail": f"Внутренняя ошибка ядра: {type(exc).__name__}: {str(exc)[:300]} "
                      "(подробности: journalctl -u copilot1c-core)"})

    @app.get("/health")
    def health() -> dict[str, Any]:
        report = health_report(s)
        report["checks"]["materials_worker"] = {"ok": worker.running, "busy": worker.busy, "optional": True}
        return report

    @app.post("/ask")
    def ask(req: AskRequest) -> dict[str, Any]:
        # Синхронный обработчик: FastAPI выполняет его в пуле потоков, долгий ответ агента не блокирует /health
        if not (s.yc_api_key and s.yc_folder_id and s.vector_store_id):
            raise HTTPException(503, "Не настроены ключи Yandex или COPILOT_VECTOR_STORE_ID в .env ядра")
        question = req.question.strip()
        t0 = time.monotonic()
        kind = classify_message(s, question)
        try:
            sources = search_sources(s, question)
            result = run_question(s, question)
        except Exception as exc:  # noqa: BLE001 — клиенту причина текстом, а не 500 без объяснения
            raise HTTPException(502, f"Ошибка обращения к Yandex AI Studio: {type(exc).__name__}: {str(exc)[:300]}") \
                from exc
        return {"answer": result.answer, "sources": sources, "seconds": round(time.monotonic() - t0, 1),
                "steps": result.steps, "tools": [t.get("tool", "") for t in result.trace], **kind}

    @app.post("/ask/files")
    def ask_files(question: str = Form(..., min_length=2, max_length=2000),
                  files: list[UploadFile] = File(...)) -> dict[str, Any]:
        from copilot1c.chat_files import MAX_FILES, context_block, extract
        from copilot1c.materials import MAX_UPLOAD_BYTES

        if not (s.yc_api_key and s.yc_folder_id and s.vector_store_id):
            raise HTTPException(503, "Не настроены ключи Yandex или COPILOT_VECTOR_STORE_ID в .env ядра")
        if len(files) > MAX_FILES:
            raise HTTPException(413, f"Не больше {MAX_FILES} файлов к одному вопросу")
        question = question.strip()
        t0 = time.monotonic()
        raw = []
        for f in files:
            data = f.file.read(MAX_UPLOAD_BYTES + 1)
            if data and len(data) <= MAX_UPLOAD_BYTES:
                raw.append((f.filename or "файл", data))
        items = extract(raw, s)
        attached = context_block(items)
        emails = [it for it in items if it.kind == "email"]
        # тип сообщения — по вопросу и тексту писем (проблему обычно описывает письмо, а не вопрос)
        kind = classify_message(s, "\n\n".join([question, *(e.text[:4000] for e in emails)]), has_files=True)
        email_info = _email_for_issue(s, raw) if kind.get("issue_draft") else None
        if email_info:
            kind["issue_draft"] = {**kind["issue_draft"], **email_info}
        subjects = " ".join(dict.fromkeys(e.subject for e in emails if e.subject))
        search_query = f"{question} {subjects}".strip() if subjects else None
        try:
            sources = search_sources(s, search_query or question)
            result = run_question(s, question, attached, search_query)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"Ошибка обращения к Yandex AI Studio: {type(exc).__name__}: {str(exc)[:300]}") \
                from exc
        return {"answer": result.answer, "sources": sources, "seconds": round(time.monotonic() - t0, 1),
                "steps": result.steps, "tools": [t.get("tool", "") for t in result.trace], **kind,
                "attachments": [it.out() for it in items]}

    @app.post("/classify")
    def classify_endpoint(req: ClassifyRequest) -> dict[str, Any]:
        return classify_message(s, req.text.strip(), req.has_files, req.use_llm)

    @app.post("/materials")
    def upload(files: list[UploadFile] = File(...)) -> dict[str, Any]:
        from copilot1c.materials import MAX_UPLOAD_BYTES, register_upload, row_out

        root = Path(s.materials_dir)
        g, reg = _registry(s)
        out = []
        try:
            for f in files:
                data = f.file.read(MAX_UPLOAD_BYTES + 1)
                name = f.filename or "файл"
                if len(data) > MAX_UPLOAD_BYTES:
                    out.append({"filename": name, "error": f"файл больше {MAX_UPLOAD_BYTES // 2**20} МБ — не сохранён"})
                    continue
                if not data:
                    out.append({"filename": name, "error": "пустой файл — не сохранён"})
                    continue
                row, seen = register_upload(reg, root, name, data, base=Path.cwd())
                out.append({**row_out(row), "already_uploaded": seen})
        finally:
            g.close()
        return {"materials": out}

    @app.get("/materials")
    def materials(limit: int = 200) -> dict[str, Any]:
        from copilot1c.materials import row_out

        g, reg = _registry(s)
        try:
            return {"materials": [row_out(r) for r in reg.list(min(max(limit, 1), 1000))]}
        finally:
            g.close()

    @app.get("/materials/{material_id}")
    def material(material_id: int) -> dict[str, Any]:
        from copilot1c.materials import row_out

        g, reg = _registry(s)
        try:
            row = reg.get(material_id)
        finally:
            g.close()
        if row is None:
            raise HTTPException(404, "Материал не найден")
        return row_out(row)

    # ---------- обращения ----------
    # Ошибки данных (неизвестный статус, пустая тема) — 422 с текстом; обращения нет — 404;
    # обращение изменили после открытия — 409 и актуальная карточка в detail.current.

    def _call(fn):
        from copilot1c.issues import IssueError, StorageError, VersionConflict

        try:
            return fn()
        except StorageError as exc:
            raise HTTPException(503, str(exc)) from exc
        except IssueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except VersionConflict as exc:
            raise HTTPException(409, {"message": str(exc), "current": exc.current}) from exc

    @app.get("/issues/meta")
    def issues_meta() -> dict[str, Any]:
        from copilot1c.issues import meta

        return meta(s.analysts)

    @app.get("/issues")
    def issues_list(status: str | None = None, priority: str | None = None, category: str | None = None,
                    assignee: str | None = None, open: bool = False, q: str | None = None,
                    limit: int = 200) -> dict[str, Any]:
        g, reg = _issues(s)
        try:
            return {"issues": reg.list(status, priority, category, assignee, q, open, min(max(limit, 1), 1000))}
        finally:
            g.close()

    @app.post("/issues")
    def issues_create(req: IssueCreate) -> dict[str, Any]:
        g, reg = _issues(s)
        try:
            fields = req.model_dump(exclude_unset=True, exclude={"actor"})
            issue, seen = _call(lambda: reg.create(fields, req.actor))
        finally:
            g.close()
        return {**issue, "already_registered": seen}

    @app.get("/issues/{issue_id}")
    def issues_get(issue_id: int) -> dict[str, Any]:
        g, reg = _issues(s)
        try:
            issue = reg.get(issue_id)
        finally:
            g.close()
        if issue is None:
            raise HTTPException(404, "Обращение не найдено")
        return issue

    @app.patch("/issues/{issue_id}")
    def issues_patch(issue_id: int, req: IssuePatch) -> dict[str, Any]:
        g, reg = _issues(s)
        try:
            changes = req.changes.model_dump(exclude_unset=True)
            issue = _call(lambda: reg.update(issue_id, changes, req.version, req.actor, req.comment))
        finally:
            g.close()
        if issue is None:
            raise HTTPException(404, "Обращение не найдено")
        return issue

    @app.post("/issues/{issue_id}/comments")
    def issues_comment(issue_id: int, req: CommentIn) -> dict[str, Any]:
        g, reg = _issues(s)
        try:
            issue = _call(lambda: reg.add_comment(issue_id, req.text, req.actor))
        finally:
            g.close()
        if issue is None:
            raise HTTPException(404, "Обращение не найдено")
        return issue

    def _related_out(found: dict[str, list]) -> dict[str, Any]:
        from copilot1c.issues import STATUSES, number

        for x in found["issues"]:
            x["number"] = number(x["id"])
            x["status_label"] = STATUSES.get(x["status"], x["status"])
            x["created_at"] = x["created_at"].isoformat(timespec="seconds") if x.get("created_at") else None
        return found

    @app.get("/issues/{issue_id}/related")
    def issues_related(issue_id: int) -> dict[str, Any]:
        from copilot1c.related import find_related

        g, reg = _issues(s)
        try:
            issue = reg.get(issue_id)
            if issue is None:
                raise HTTPException(404, "Обращение не найдено")
            return _related_out(find_related(g.conn, s.project, issue, exclude_id=issue_id))
        finally:
            g.close()

    @app.post("/issues/related")
    def issues_related_draft(req: RelatedIn) -> dict[str, Any]:
        """Для черновика: нового обращения в карточке или карточки в чате."""
        from copilot1c.related import find_related

        g, _reg = _issues(s)
        try:
            return _related_out(find_related(g.conn, s.project, req.model_dump()))
        finally:
            g.close()

    @app.post("/issues/{issue_id}/kb-draft")
    def issues_kb_draft(issue_id: int, req: KbDraftIn | None = None) -> dict[str, Any]:
        from copilot1c.kb import compose

        g, reg = _issues(s)
        try:
            issue = reg.get(issue_id)
        finally:
            g.close()
        if issue is None:
            raise HTTPException(404, "Обращение не найдено")
        return _call(lambda: compose(issue, s, use_llm=(req.use_llm if req else True)))

    @app.post("/issues/{issue_id}/kb-publish")
    def issues_kb_publish(issue_id: int, req: KbPublishIn) -> dict[str, Any]:
        """Сохраняет разбор материалом и ставит ссылку на него в обращение (kb_material_id + история)."""
        from copilot1c.kb import publish
        from copilot1c.materials import row_out

        g, reg = _issues(s)
        try:
            issue = reg.get(issue_id)
            if issue is None:
                raise HTTPException(404, "Обращение не найдено")
            material, seen = _call(lambda: publish(g.conn, s.project, issue, req.title, req.text,
                                                   Path(s.materials_dir), base=Path.cwd(), analysts=s.analysts))
            card = issue
            if issue.get("kb_material_id") != material["id"]:
                note = f"Разбор отправлен в базу знаний: «{material['filename']}»"
                card = _call(lambda: reg.update(issue_id, {"kb_material_id": material["id"]}, issue["version"],
                                                req.actor, comment=note))
        finally:
            g.close()
        return {"material": {**row_out(material), "already_uploaded": seen}, "issue": card}

    @app.post("/issues/from-email")
    def issues_from_email(file: UploadFile = File(...)) -> dict[str, Any]:
        """Разбор письма в черновик обращения. Ничего не сохраняет: аналитик проверяет и сохраняет сам."""
        from copilot1c.email_intake import analyze, is_email_file, proposal
        from copilot1c.materials import MAX_UPLOAD_BYTES

        name = file.filename or "письмо"
        if not is_email_file(name):
            raise HTTPException(422, "Нужен файл письма .msg или .eml")
        data = file.file.read(MAX_UPLOAD_BYTES + 1)
        if not data or len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(422, "Файл пустой или слишком большой")
        try:
            intake = analyze(name, data, s.internal_domains, s.analyst_emails, s.analysts)
        except Exception as exc:  # noqa: BLE001 — битый файл: причина текстом
            raise HTTPException(422, f"Не удалось разобрать письмо: {type(exc).__name__}: {str(exc)[:200]}") from exc
        out = proposal(name, intake)
        out["contact"] = None
        out["already_registered"] = None
        g, reg = _issues(s)
        try:
            if out["initiator"]:
                out["contact"] = reg.contact_by_email(out["initiator"]["email"])
            key = out["draft"].get("source_message_id")
            row = reg.find_by_message_id(key) if key else None
            if row is not None:
                out["already_registered"] = {"id": row["id"], "number": iss_number(row["id"]), "title": row["title"]}
        finally:
            g.close()
        if not s.internal_domains:
            out["reason"] += " (не задан COPILOT_INTERNAL_DOMAINS — свои домены неизвестны)"
        return out

    @app.post("/issues/{issue_id}/attachments")
    def issues_attach(issue_id: int, files: list[UploadFile] = File(...), actor: str | None = Form(None),
                      expand: bool = Form(False)) -> dict[str, Any]:
        """expand — для писем .msg/.eml прикрепить ещё и вложенные в них файлы (скриншоты, логи)."""
        from copilot1c.email_intake import is_email_file, read_chain
        from copilot1c.materials import MAX_UPLOAD_BYTES

        g, reg = _issues(s)
        out = []

        def attach(name: str, data: bytes, inner_of: str | None = None) -> None:
            res = _call(lambda: reg.add_attachment(issue_id, name, data, actor))
            if res is None:
                raise HTTPException(404, "Обращение не найдено")
            att, seen = res
            out.append({**att, "already_attached": seen, **({"from_email": inner_of} if inner_of else {})})

        try:
            for f in files:
                data = f.file.read(MAX_UPLOAD_BYTES + 1)
                name = f.filename or "файл"
                if len(data) > MAX_UPLOAD_BYTES:
                    out.append({"filename": name, "error": f"файл больше {MAX_UPLOAD_BYTES // 2**20} МБ — не сохранён"})
                    continue
                if not data:
                    out.append({"filename": name, "error": "пустой файл — не сохранён"})
                    continue
                attach(name, data)
                if expand and is_email_file(name):
                    try:
                        inner = read_chain(name, data)[1]
                    except Exception as exc:  # noqa: BLE001 — письмо сохранено, разобрать вложения не вышло
                        out.append({"filename": name, "error": f"вложения письма не извлечены: {type(exc).__name__}"})
                        continue
                    for inner_name, inner_data in inner:
                        attach(inner_name, inner_data, name)
        finally:
            g.close()
        return {"attachments": out}

    @app.get("/issues/{issue_id}/attachments/{attachment_id}")
    def issues_attachment(issue_id: int, attachment_id: int):
        g, reg = _issues(s)
        try:
            att = reg.attachment(issue_id, attachment_id)
        finally:
            g.close()
        if att is None:
            raise HTTPException(404, "Вложение не найдено")
        path = Path(att["path"])
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.is_file():
            raise HTTPException(410, "Файл вложения отсутствует на диске")
        return FileResponse(path, media_type=att["mime"] or "application/octet-stream", filename=att["filename"])

    @app.get("/contacts")
    def contacts(q: str | None = None, limit: int = 50) -> dict[str, Any]:
        g, reg = _issues(s)
        try:
            return {"contacts": reg.contacts(q, min(max(limit, 1), 500))}
        finally:
            g.close()

    @app.post("/contacts")
    def contacts_upsert(req: ContactIn) -> dict[str, Any]:
        g, reg = _issues(s)
        try:
            return _call(lambda: reg.upsert_contact(req.name, req.email, req.organization, req.position, req.phone))
        finally:
            g.close()

    return app
