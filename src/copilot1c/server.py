"""Демон ядра: HTTP API на localhost для веба, бота и MCP (copilot1c serve).

Слушает только 127.0.0.1 — наружу ядро не выставляется, клиенты ходят через веб-часть. Секреты
(.env с ключом AI Studio, DSN PostgreSQL) остаются у демона.

Методы появляются по шагам; сейчас:
  GET /health — что видит ядро: настройки AI Studio, база поиска (фрагменты с эмбеддингами), PostgreSQL,
               платформа 1С.
  POST /ask  — вопрос агенту: ответ, найденные фрагменты-источники, шаги агента. Формат ответа совпадает
               с /api/ask веб-части, чтобы веб проксировал запрос без изменений интерфейса. Плюс intent — тип
               сообщения (вопрос, проблема, сводка, документ, знание) и issue_draft — черновик обращения, если
               это сообщение о проблеме (intent.py; ничего не сохраняется).
  POST /classify — только тип сообщения и черновик обращения, без агента.
  POST /escalations — пакет для эксперта (Claude) через оператора: multipart payload (JSON: question,
               expert_question, reason, answer, sources, web_sources, tools, issue_id, include_raw) + files;
               архив в .cache/escalations (escalate.py). GET /escalations/{id} — скачать архив.
  POST /ask/files — вопрос с файлами («+» в чате, multipart: question, files): текст писем, документов и
               картинок идёт агенту контекстом этого вопроса (в базу не сохраняется, chat_files.py); для
               письма о проблеме черновик обращения дополняется инициатором, датой и Message-ID.
  POST /materials — загрузить файлы (multipart, поле files): сохраняются в data/uploads, попадают в реестр.
  GET  /materials, GET /materials/{id} — реестр загруженных материалов и их статусы. Обработку
               (разбор → индексация → запись в базу) ведёт фоновый поток, см. worker.py.
  DELETE /materials/{id} — убрать фрагменты материала из базы поиска (файл и запись реестра остаются,
               статус «удалён»; повторная загрузка того же файла ставит его в очередь заново).
  POST /materials/{id}/retry — материал с ошибкой снова в очередь.
  GET /contours, POST /contours, PATCH /contours/{id} — справочник контуров (система / процесс / проект).
  POST /intake/analyze (multipart files) — «Входящие»: что это, к какому контуру, что уже в базе, что предложить
               (intake.py; ничего не сохраняет); POST /intake/accept (files + decisions JSON) — приём по решению
               аналитика: материалы в очередь или «без индексации», новые контуры — в справочник.
  GET /documents?q=, GET /documents/{id} — документы и их редакции (documents.py).
  POST /letters/check (multipart files) — письма: что уже известно, что ново, к какой ветке (ничего не пишет);
  GET /threads?q=, GET /threads/{id} — ветки переписки и их письма; PATCH /threads/{id} — название, обращение,
               контуры; POST /threads/{id}/summary — пересобрать сводку ветки (letters.py).
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
  POST  /issues/suggest-contours — система и подсистема для обращения; GET /issues?contour= — фильтр по системе
        с её подсистемами; POST /contours/seed — стартовый справочник (УТ 11, БП 3.0, ЗУП 3, инфраструктура).
  POST  /issues/triage (multipart files, question) — письмо по обращениям (mail_triage.py): что нового, к какому
        обращению относится, предложение «обновить ОБР-…» (что изменилось, комментарий, статус) / «новое» / «в базу».
        Письмо, прикреплённое к обращению, записывается в ветки переписки, ветка связывается с обращением, номера
        заявок из темы — в external_refs: следующий ответ по той же переписке узнаётся сам.
  Секретарь (secretary/) — распорядок дня, записи по person (поле «Я»):
  POST /secretary/say {person, text} — фраза («я в Варшаве», «работаем час», «стоп»…) → ответ, действия, состояние;
  GET  /secretary/state?person= — место, местное время, идущая сессия, планы, непрочитанные сообщения таймера;
  GET  /secretary/notices?person=&after= — сообщения таймера; POST /secretary/notices/{id}/read {person};
  GET  /secretary/places?person= — журнал мест. Таймер — поток демона (secretary/timer.py), запускается с serve.
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
            out.update(g.query("SELECT (SELECT count(*) FROM chunks) AS chunks, (SELECT count(*) FROM chunks WHERE "
                               "embedding IS NOT NULL AND status = 'active') AS searchable, (SELECT count(*) FROM "
                               "test_cases) AS test_cases, (SELECT count(*) FROM requirements) AS requirements")[0])
        except Exception as exc:  # noqa: BLE001
            out["detail"] = f"схема не создана (copilot1c init-db): {type(exc).__name__}"
        return out
    except Exception as exc:  # noqa: BLE001 — health не должен падать из-за схемы
        return {"ok": False, "detail": f"{type(exc).__name__}"}
    finally:
        g.close()


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

    pg = {**_check_postgres(s), "host": s.pg_dsn.rsplit("@", 1)[-1]}  # без логина и пароля
    search = {"ok": bool(pg["ok"] and "searchable" in pg), "backend": "postgres",
              "chunks": pg.get("searchable", 0), "embedding": s.embedding_doc}
    checks = {
        "ai_studio": {"ok": bool(s.yc_api_key and s.yc_folder_id), "folder": s.yc_folder_id or None,
                      "model": s.model_orchestrator},
        "search": search,
        "vector_store": search,  # прежнее имя — для веба до web-0011
        "postgres": pg,
        "platform_1c": {"ok": Path(s.onec_bin).exists(), "optional": True},
        "ocr": {"ok": True, "backend": backend(s), "optional": True},
        "issues_files": _check_writable(Path(s.issues_dir)),
    }
    required_ok = all(checks[k]["ok"] for k in ("ai_studio", "search", "postgres"))
    return {"status": "ok" if required_ok else "degraded", "version": __version__, "project": s.project,
            "checks": checks}


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=2000)


class ContourIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    name: str = Field(min_length=1, max_length=200)
    parent_id: int | None = None
    aliases: list[str] = Field(default_factory=list)
    notes: str | None = Field(default=None, max_length=4000)


class ContourPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    parent_id: int | None = None
    aliases: list[str] | None = None
    notes: str | None = Field(default=None, max_length=4000)
    active: bool | None = None


class ThreadPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, max_length=500)
    issue_id: int | None = None
    contours: list[int] | None = None


class SecretarySay(BaseModel):
    model_config = ConfigDict(extra="forbid")
    person: str = Field(default="", max_length=200)
    text: str = Field(min_length=1, max_length=2000)


class SecretaryPerson(BaseModel):
    person: str = Field(default="", max_length=200)


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
    from copilot1c.graph.store import try_connect
    from copilot1c.retrieval import smart_search, source_label
    from copilot1c.search import PgIndex, search_fn

    g = try_connect(s)
    if g is None:
        raise RuntimeError("PostgreSQL недоступен — поиск по базе проекта невозможен")
    try:
        hits = smart_search(search_fn(PgIndex(g.conn, s)), question, {"project": s.project}, k)
    finally:
        g.close()
    out = []
    for i, h in enumerate(hits, 1):
        attrs = h.get("attributes") or {}
        out.append({"n": i, "label": source_label(attrs, h.get("text", "")), "doc_type": attrs.get("doc_type", ""),
                    "date": attrs.get("date", ""), "text": h.get("text", "")})
    return out


def run_question(s: Settings, question: str, attached: str = "", search_query: str | None = None,
                 task: str = ""):
    from copilot1c.agent.tools import ToolContext, run_agent
    from copilot1c.graph.store import try_connect

    store = try_connect(s)
    try:
        ctx = ToolContext(s, Path("data/dumps"), store)
        if attached or search_query or task:
            return run_agent(question, ctx, attached=attached, search_query=search_query, task=task)
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
    external_refs: list[str] | None = None
    contours: list[int] | None = None      # система и подсистема (id контуров)
    transferred_to: str | None = None      # кому передано обращение не по профилю


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

    from copilot1c.secretary.timer import SecretaryTimer
    from copilot1c.worker import MaterialWorker

    s = settings or get_settings()
    worker = MaterialWorker(s)
    timer = SecretaryTimer(s)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_worker:
            worker.start()
            timer.start()
        yield
        if worker.running:
            worker.stop()
        if timer.running:
            timer.stop()

    app = FastAPI(title="1С Project Copilot — ядро", version=__version__, lifespan=lifespan)
    app.state.worker = worker
    app.state.secretary_timer = timer

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
        report["checks"]["timer"] = {"ok": timer.running, "fired": timer.fired, "optional": True}
        return report

    @app.post("/ask")
    def ask(req: AskRequest) -> dict[str, Any]:
        # Синхронный обработчик: FastAPI выполняет его в пуле потоков, долгий ответ агента не блокирует /health
        if not (s.yc_api_key and s.yc_folder_id):
            raise HTTPException(503, "Не настроены ключи Yandex AI Studio в .env ядра")
        question = req.question.strip()
        t0 = time.monotonic()
        kind = classify_message(s, question)
        try:
            sources = search_sources(s, question)
            result = run_question(s, question)
        except Exception as exc:  # noqa: BLE001 — клиенту причина текстом, а не 500 без объяснения
            raise HTTPException(502, f"Ошибка поиска или Yandex AI Studio: {type(exc).__name__}: {str(exc)[:300]}") \
                from exc
        return {"answer": result.answer, "sources": sources, "seconds": round(time.monotonic() - t0, 1),
                "steps": result.steps, "tools": [t.get("tool", "") for t in result.trace], **kind,
                "web_sources": getattr(result, "web_sources", []), "escalation": getattr(result, "escalation", None),
                "issues": getattr(result, "issues", [])}

    @app.post("/ask/files")
    def ask_files(question: str = Form(..., min_length=2, max_length=2000),
                  files: list[UploadFile] = File(...)) -> dict[str, Any]:
        from copilot1c.chat_files import MAX_FILES, context_block, extract
        from copilot1c.materials import MAX_UPLOAD_BYTES

        if not (s.yc_api_key and s.yc_folder_id):
            raise HTTPException(503, "Не настроены ключи Yandex AI Studio в .env ядра")
        if len(files) > MAX_FILES:
            raise HTTPException(413, f"Не больше {MAX_FILES} файлов к одному вопросу")
        question = question.strip()
        t0 = time.monotonic()
        raw = []
        for f in files:
            data = f.file.read(MAX_UPLOAD_BYTES + 1)
            if data and len(data) <= MAX_UPLOAD_BYTES:
                raw.append((f.filename or "файл", data))
        from copilot1c.graph.store import try_connect

        g = try_connect(s)
        try:
            items = extract(raw, s, conn=g.conn if g is not None else None)
        finally:
            if g is not None:
                g.close()
        attached = context_block(items)
        emails = [it for it in items if it.kind == "email"]
        # «принять в базу» решают слова аналитика, а не текст приложенных писем: в переписке полно вопросов,
        # жалоб и просьб «подготовить», и по общему тексту модель видела «подготовить документ»
        own = classify_message(s, question, has_files=True, use_llm=False)
        if own.get("intent", {}).get("primary") in ("intake", "issue_mail") and own["intent"].get("confident"):
            kind = own
        else:  # тип по вопросу и тексту писем (проблему обычно описывает письмо, а не вопрос)
            kind = classify_message(s, "\n\n".join([question, *(e.text[:4000] for e in emails)]), has_files=True)
        email_info = _email_for_issue(s, raw) if kind.get("issue_draft") else None
        if email_info:
            kind["issue_draft"] = {**kind["issue_draft"], **email_info}
        subjects = " ".join(dict.fromkeys(e.subject for e in emails if e.subject))
        search_query = f"{question} {subjects}".strip() if subjects else None
        intake, task, triage_res = None, "", None
        primary = kind.get("intent", {}).get("primary")
        if emails and (primary == "issue_mail" or kind.get("issue_draft")):
            # письмо по обращениям: новое, обновление существующего или только в базу — решает разбор письма
            from copilot1c.mail_triage import TRIAGE_TASK, agent_block, triage

            g = try_connect(s)
            if g is not None:
                try:
                    triage_res = triage(raw, question, g.conn, s)
                except Exception:  # noqa: BLE001 — разбор не удался: ответ агента всё равно будет
                    import logging

                    logging.getLogger("copilot1c.mail_triage").exception("разбор письма по обращениям")
                    g.conn.rollback()
                finally:
                    g.close()
            if triage_res and triage_res["emails"]:
                task = f"{TRIAGE_TASK}\n\nРазбор письма:\n{agent_block(triage_res)}"
                draft = kind.get("issue_draft") or {}
                if any(e.get("decision") == "update" for e in triage_res["emails"]) and not draft.get("already_registered"):
                    kind["issue_draft"] = None  # это обновление существующего — «Зарегистрировать» не предлагаем
        if primary == "intake":  # «разбери и добавь в базу» — разбор приложенного
            from copilot1c.intake import INTAKE_TASK, agent_block, analyze

            g = try_connect(s)
            try:
                intake = analyze(raw, g.conn if g is not None else None, s)
            finally:
                if g is not None:
                    g.conn.rollback()
                    g.close()
            task = f"{INTAKE_TASK}\n\nРазбор приложенного:\n{agent_block(intake)}"
        try:
            sources = search_sources(s, search_query or question)
            result = run_question(s, question, attached, search_query, task)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"Ошибка поиска или Yandex AI Studio: {type(exc).__name__}: {str(exc)[:300]}") \
                from exc
        return {"answer": result.answer, "sources": sources, "seconds": round(time.monotonic() - t0, 1),
                "steps": result.steps, "tools": [t.get("tool", "") for t in result.trace], **kind,
                "attachments": [it.out() for it in items], "intake": intake, "triage": triage_res,
                "web_sources": getattr(result, "web_sources", []),
                "escalation": getattr(result, "escalation", None), "issues": getattr(result, "issues", [])}

    @app.post("/escalations")
    def escalation_create(payload: str = Form("{}"), files: list[UploadFile] | None = File(None)) -> dict[str, Any]:
        from copilot1c.escalate import EscalationInput, build, package_name, store
        from copilot1c.materials import MAX_UPLOAD_BYTES

        try:
            p = json.loads(payload or "{}")
        except ValueError as exc:
            raise HTTPException(422, "payload — не JSON") from exc
        question = str(p.get("question") or "").strip()
        issue, related, raw = None, None, []
        for f in files or []:
            data = f.file.read(MAX_UPLOAD_BYTES + 1)
            if data and len(data) <= MAX_UPLOAD_BYTES:
                raw.append((f.filename or "файл", data))
        if p.get("issue_id"):
            from copilot1c.related import find_related

            g, reg = _issues(s)
            try:
                issue = reg.get(int(p["issue_id"]))
                if issue is None:
                    raise HTTPException(404, "Обращение не найдено")
                related = find_related(g.conn, s.project, issue, exclude_id=issue["id"])
                related = _related_out(related)
                for a in issue.get("attachments") or []:  # вложения обращения — тоже материалы пакета
                    row = reg.attachment(issue["id"], a["id"])
                    path = Path(row["path"]) if row else None
                    if path is not None and not path.is_absolute():
                        path = Path.cwd() / path
                    if path is not None and path.is_file() and path.stat().st_size <= MAX_UPLOAD_BYTES:
                        raw.append((row["filename"], path.read_bytes()))
            finally:
                g.close()
            question = question or f"{issue['number']}: {issue['title']}"
        if len(question) < 2:
            raise HTTPException(422, "Нужен вопрос или обращение")
        inp = EscalationInput(
            question=question, expert_question=str(p.get("expert_question") or ""), reason=str(p.get("reason") or ""),
            answer=str(p.get("answer") or ""), sources=list(p.get("sources") or [])[:30],
            web_sources=list(p.get("web_sources") or [])[:20], tools=[str(t) for t in p.get("tools") or []][:30],
            attachments=raw[:20], include_raw=bool(p.get("include_raw")), issue=issue, related=related)
        try:
            data, manifest = build(inp, s)
        except ValueError as exc:
            raise HTTPException(413, str(exc)) from exc
        store(Path(s.cache_dir or ".cache") / "escalations", data, manifest)
        return {"id": manifest["id"], "filename": package_name(manifest), "size": manifest["size"],
                "files": manifest["files"], "raw_attachments": manifest["raw_attachments"]}

    @app.get("/escalations/{package_id}")
    def escalation_get(package_id: str):
        from copilot1c.escalate import load, package_name

        found = load(Path(s.cache_dir or ".cache") / "escalations", package_id)
        if found is None:
            raise HTTPException(404, "Пакет не найден")
        path, manifest = found
        return FileResponse(path, media_type="application/zip", filename=package_name(manifest))

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

    @app.delete("/materials/{material_id}")
    def material_delete(material_id: int) -> dict[str, Any]:
        """Фрагменты материала — из базы поиска; файл и запись реестра остаются (статус «удалён»)."""
        from copilot1c.materials import ACTIVE, row_out
        from copilot1c.search import PgIndex

        g, reg = _registry(s)
        try:
            row = reg.get(material_id)
            if row is None:
                raise HTTPException(404, "Материал не найден")
            if row["status"] in ACTIVE:
                raise HTTPException(409, "Материал ещё обрабатывается — удалить можно после окончания")
            from copilot1c.documents import forget_material

            gone = forget_material(g.conn, material_id)
            removed = PgIndex(g.conn, s).delete_material(material_id)
            g.conn.commit()
            detail = f"удалено из базы фрагментов: {removed}" + (f", писем: {gone['letters']}" if gone["letters"] else "") \
                + (f", редакций документов: {gone['versions']}" if gone["versions"] else "")
            reg.set_status(material_id, "deleted", detail)
            return {"removed_chunks": removed, **{f"removed_{k}": v for k, v in gone.items()},
                    "material": row_out(reg.get(material_id))}
        finally:
            g.close()

    @app.post("/materials/{material_id}/retry")
    def material_retry(material_id: int) -> dict[str, Any]:
        """Материал с ошибкой — снова в очередь (с тем же решением аналитика)."""
        from copilot1c.materials import row_out

        g, reg = _registry(s)
        try:
            row = reg.get(material_id)
            if row is None:
                raise HTTPException(404, "Материал не найден")
            if row["status"] != "error":
                raise HTTPException(409, "Повторить можно только материал с ошибкой")
            reg.set_status(material_id, "queued", "повтор после ошибки")
            return row_out(reg.get(material_id))
        finally:
            g.close()

    # ---------- контуры ----------

    @app.get("/contours")
    def contours_list(all: bool = False) -> dict[str, Any]:  # noqa: A002 — имя параметра запроса
        from copilot1c.contours import ContourRegistry

        g, _ = _registry(s)
        try:
            return {"contours": ContourRegistry(g.conn, s.project).list(include_inactive=all),
                    "kinds": ContourRegistry.KINDS}
        finally:
            g.close()

    @app.post("/contours")
    def contours_create(req: ContourIn) -> dict[str, Any]:
        from copilot1c.contours import ContourError, ContourRegistry

        g, _ = _registry(s)
        try:
            return ContourRegistry(g.conn, s.project).create(req.model_dump(exclude_none=True))
        except ContourError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        finally:
            g.close()

    @app.patch("/contours/{contour_id}")
    def contours_patch(contour_id: int, req: ContourPatch) -> dict[str, Any]:
        from copilot1c.contours import ContourError, ContourRegistry

        g, _ = _registry(s)
        try:
            return ContourRegistry(g.conn, s.project).update(contour_id, req.model_dump(exclude_unset=True))
        except ContourError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
        finally:
            g.close()

    # ---------- «Входящие»: разбор и приём ----------

    def _read_files(files: list[UploadFile]) -> list[tuple[str, bytes]]:
        from copilot1c.materials import MAX_UPLOAD_BYTES

        out = []
        for f in files:
            data = f.file.read(MAX_UPLOAD_BYTES + 1)
            if len(data) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, f"Файл «{f.filename}» больше {MAX_UPLOAD_BYTES // 2**20} МБ")
            out.append((f.filename or "файл", data))
        return out

    @app.post("/intake/analyze")
    def intake_analyze(files: list[UploadFile] = File(...)) -> dict[str, Any]:
        """Разбор принесённых файлов без сохранения: что это, к чему относится, что уже в базе, что предложить."""
        from copilot1c.intake import analyze

        if len(files) > 20:
            raise HTTPException(413, "Не больше 20 файлов за раз")
        raw = _read_files(files)
        g, _ = _registry(s)
        try:
            res = analyze(raw, g.conn, s)
            g.conn.rollback()
            return res
        finally:
            g.close()

    @app.post("/intake/accept")
    def intake_accept(decisions: str = Form("[]"), files: list[UploadFile] = File(...)) -> dict[str, Any]:
        """Файлы с решениями аналитика → реестр материалов: в очередь обработки или «без индексации»."""
        from copilot1c.intake import accept

        try:
            items = json.loads(decisions or "[]")
            assert isinstance(items, list)
        except (ValueError, AssertionError) as exc:
            raise HTTPException(422, "decisions — JSON-список решений по файлам") from exc
        raw = _read_files(files)
        g, _ = _registry(s)
        try:
            return accept(raw, items, g.conn, s, base=Path.cwd())
        finally:
            g.close()

    @app.get("/documents")
    def documents_list(q: str | None = None, limit: int = 200) -> dict[str, Any]:
        from copilot1c.documents import KINDS, DocumentRegistry

        g, _ = _registry(s)
        try:
            rows = DocumentRegistry(g.conn, s.project).list(q or None, min(max(limit, 1), 1000))
            g.conn.commit()
            return {"documents": [_thread_out(r) for r in rows], "kinds": KINDS}
        finally:
            g.close()

    @app.get("/documents/{doc_id}")
    def documents_get(doc_id: int) -> dict[str, Any]:
        from copilot1c.documents import DocumentRegistry

        g, _ = _registry(s)
        try:
            d = DocumentRegistry(g.conn, s.project).get(doc_id)
            g.conn.commit()
        finally:
            g.close()
        if d is None:
            raise HTTPException(404, "Документ не найден")
        return {**_thread_out({k: v for k, v in d.items() if k != "versions"}),
                "versions": [_thread_out(v) for v in d["versions"]]}

    # ---------- письма и ветки ----------

    def _thread_out(t: dict[str, Any]) -> dict[str, Any]:
        def iso(v):
            return v.isoformat(timespec="minutes") if hasattr(v, "isoformat") else v

        out = {k: iso(v) for k, v in t.items() if k != "letters"}
        if "letters" in t:
            out["letters"] = [{k: iso(v) for k, v in x.items()} for x in t["letters"]]
        return out

    @app.post("/letters/check")
    def letters_check(files: list[UploadFile] = File(...)) -> dict[str, Any]:
        """Пробный прогон писем: что в них уже известно, что ново, к какой ветке относятся. Ничего не пишет."""
        from copilot1c.email_intake import is_email_file
        from copilot1c.ingest.attachments import parse_bytes
        from copilot1c.ingest.msg import ParsedEmail
        from copilot1c.letters import LetterStore, agent_view
        from copilot1c.materials import MAX_UPLOAD_BYTES

        g, _ = _registry(s)
        try:
            st = LetterStore(g.conn, s.project)
            out = []
            for f in files:
                name = f.filename or "письмо"
                data = f.file.read(MAX_UPLOAD_BYTES + 1)
                if not is_email_file(name) or len(data) > MAX_UPLOAD_BYTES:
                    out.append({"filename": name, "error": "не письмо .msg/.eml или файл слишком большой"})
                    continue
                parsed = [x for x in parse_bytes(data, name, name, settings=s) if isinstance(x, ParsedEmail)]
                res = [r for p in parsed for r in st.check(p)]
                out.append({"filename": name, "chains": [r.out() for r in res], "agent_view": agent_view(res)})
            g.conn.rollback()
            return {"files": out}
        finally:
            g.close()

    @app.get("/threads")
    def threads_list(q: str | None = None, limit: int = 100) -> dict[str, Any]:
        from copilot1c.letters import LetterStore

        g, _ = _registry(s)
        try:
            rows = LetterStore(g.conn, s.project).list(q or None, min(max(limit, 1), 500))
            g.conn.commit()
            return {"threads": [_thread_out(r) for r in rows]}
        finally:
            g.close()

    @app.get("/threads/{thread_id}")
    def thread_get(thread_id: int) -> dict[str, Any]:
        from copilot1c.letters import LetterStore

        g, _ = _registry(s)
        try:
            t = LetterStore(g.conn, s.project).thread(thread_id)
            g.conn.commit()
        finally:
            g.close()
        if t is None:
            raise HTTPException(404, "Ветка не найдена")
        return _thread_out(t)

    @app.patch("/threads/{thread_id}")
    def thread_patch(thread_id: int, req: ThreadPatch) -> dict[str, Any]:
        from copilot1c.letters import LetterStore

        g, _ = _registry(s)
        try:
            st = LetterStore(g.conn, s.project)
            if st.thread(thread_id) is None:
                raise HTTPException(404, "Ветка не найдена")
            data = req.model_dump(exclude_unset=True)
            if data.get("issue_id") is not None and not st._rows("SELECT 1 FROM issues WHERE id = %s AND project = %s",
                                                                 (data["issue_id"], s.project)):
                raise HTTPException(422, "Обращение не найдено")
            t = st.update(thread_id, data)
            g.conn.commit()
            return _thread_out(t)
        finally:
            g.close()

    @app.post("/threads/{thread_id}/summary")
    def thread_summary(thread_id: int) -> dict[str, Any]:
        """Пересобрать сводку ветки (модель или шаблон) и её фрагмент в базе поиска."""
        from copilot1c.letters import LetterStore, summarize
        from copilot1c.search import PgIndex

        g, _ = _registry(s)
        try:
            st = LetterStore(g.conn, s.project)
            if st.thread(thread_id) is None:
                raise HTTPException(404, "Ветка не найдена")
            res = summarize(st, thread_id, s, use_llm=s.thread_summary_llm, index=PgIndex(g.conn, s))
            g.conn.commit()
            return res
        finally:
            g.close()

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

    @app.post("/issues/suggest-contours")
    def issues_suggest_contours(req: RelatedIn) -> dict[str, Any]:
        """Система и подсистема для обращения (черновика): объекты 1С, слова, модель; новый пункт справочника,
        если подходящего нет."""
        from copilot1c.contours import suggest

        g, _ = _registry(s)
        try:
            text = "\n".join(x for x in (req.title, req.description, req.error_text) if x)
            res = suggest(g.conn, s.project, text, req.objects, settings=s)
            g.conn.rollback()
            return res
        finally:
            g.close()

    @app.post("/contours/seed")
    def contours_seed() -> dict[str, Any]:
        """Стартовый справочник: УТ 11, БП 3.0, ЗУП 3, инфраструктура и их функциональные блоки (повтор безопасен)."""
        from copilot1c.contour_catalog import seed

        g, _ = _registry(s)
        try:
            res = seed(g.conn, s.project)
            g.conn.commit()
            return res
        finally:
            g.close()

    @app.get("/issues/meta")
    def issues_meta() -> dict[str, Any]:
        from copilot1c.issues import meta

        return meta(s.analysts)

    @app.get("/issues")
    def issues_list(status: str | None = None, priority: str | None = None, category: str | None = None,
                    assignee: str | None = None, open: bool = False, q: str | None = None,
                    limit: int = 200, contour: int | None = None) -> dict[str, Any]:
        g, reg = _issues(s)
        try:
            return {"issues": reg.list(status, priority, category, assignee, q, open, min(max(limit, 1), 1000),
                                       contour)}
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
            if issue is not None and changes.get("contours") and issue.get("objects"):
                # аналитик отнёс обращение к подсистеме — её объекты 1С становятся псевдонимами подсистемы
                from copilot1c.contours import learn_objects

                try:
                    learn_objects(g.conn, s.project, issue["contours"], issue["objects"])
                except Exception:  # noqa: BLE001 — обучение справочника не должно ломать сохранение
                    g.conn.rollback()
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
                                                   Path(s.materials_dir), base=Path.cwd(), analysts=s.analysts,
                                                   contours=issue.get("contours") or []))
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

    @app.post("/issues/triage")
    def issues_triage(files: list[UploadFile] = File(...), question: str = Form("")) -> dict[str, Any]:
        """Письма по обращениям: что нового, к какому обращению, что предложить (обновить / новое / в базу).
        Ничего не сохраняет."""
        from copilot1c.mail_triage import triage

        raw = _read_files(files)
        g, _ = _registry(s)
        try:
            res = triage(raw, question, g.conn, s)
            g.conn.rollback()
            return res
        finally:
            g.close()

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
                if is_email_file(name):  # письмо — в ветки переписки, ветка — к обращению (следующий ответ узнается)
                    try:
                        from copilot1c.mail_triage import link_email
                        from copilot1c.search import PgIndex

                        index = PgIndex(g.conn, s) if s.yc_api_key and s.yc_folder_id else None
                        out[-1]["linked"] = link_email(g.conn, s, issue_id, name, data, index=index, actor=actor)
                    except Exception as exc:  # noqa: BLE001 — вложение сохранено, связь с перепиской не вышла
                        g.conn.rollback()
                        out[-1]["link_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
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

    # ---------- секретарь ----------

    def _secretary():
        from copilot1c.graph.store import try_connect
        from copilot1c.secretary.service import Secretary

        g = try_connect(s)
        if g is None:
            raise HTTPException(503, "PostgreSQL недоступен — секретарь не работает")
        return g, Secretary(g.conn, s)

    @app.post("/secretary/say")
    def secretary_say(req: SecretarySay) -> dict[str, Any]:
        g, sec = _secretary()
        try:
            return sec.say(req.person, req.text)
        finally:
            g.close()

    @app.get("/secretary/state")
    def secretary_state(person: str = "") -> dict[str, Any]:
        g, sec = _secretary()
        try:
            return sec.state(person.strip())
        finally:
            g.close()

    @app.get("/secretary/notices")
    def secretary_notices(person: str = "", after: int = 0, unread: bool = False) -> dict[str, Any]:
        from copilot1c.secretary.service import _notice_out

        g, sec = _secretary()
        try:
            return {"notices": [_notice_out(n) for n in sec.store.notices(person.strip(), after, unread)]}
        finally:
            g.close()

    @app.post("/secretary/notices/{notice_id}/read")
    def secretary_notice_read(notice_id: int, req: SecretaryPerson) -> dict[str, Any]:
        g, sec = _secretary()
        try:
            if not sec.store.mark_read(req.person.strip(), notice_id):
                raise HTTPException(404, "Сообщение не найдено")
            return {"ok": True}
        finally:
            g.close()

    @app.get("/secretary/places")
    def secretary_places(person: str = "", limit: int = 50) -> dict[str, Any]:
        from copilot1c.secretary.service import _place_out

        g, sec = _secretary()
        try:
            return {"places": [_place_out(p) for p in sec.store.history(person.strip(), min(max(limit, 1), 500))]}
        finally:
            g.close()

    return app
