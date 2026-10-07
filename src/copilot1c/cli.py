"""Командная строка: copilot1c <команда>.

Команды разбора (parse-*) работают локально без облака — удобно проверять чанкинг на своих файлах.
Команды index-*, search и ask требуют настроенного .env (AI Studio, PostgreSQL с pgvector).
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from copilot1c.config import get_settings
from copilot1c.models import Chunk

app = typer.Typer(help="1С Project Copilot", no_args_is_help=True)


def _corpus(paths: list[Path]):
    from copilot1c.ingest.corpus import Corpus

    return Corpus(project=get_settings().project).add_paths(paths)


def _collect(paths: list[Path]) -> list[Chunk]:
    return _corpus(paths).chunks()


def _print(chunks: list[Chunk], as_json: bool) -> None:
    for c in chunks:
        if as_json:
            typer.echo(json.dumps({"id": c.chunk_id, "attributes": c.attributes(), "text": c.text},
                                  ensure_ascii=False))
        else:
            typer.echo(f"── {c.doc_type.value} · {c.title} · {c.source}")
            typer.echo(c.text[:600] + ("…" if len(c.text) > 600 else ""))
            typer.echo()
    typer.echo(f"Чанков: {len(chunks)}", err=True)


@app.command("parse-docs")
def parse_docs(paths: list[Path], json_out: bool = typer.Option(False, "--json"),
               report_only: bool = typer.Option(False, "--report", help="Только сводка без чанков")):
    """Разобрать письма (.msg/.eml с вложениями), документы (.docx/.pdf/.xlsx/…) и архивы в чанки."""
    corpus = _corpus(paths)
    if not report_only:
        _print(corpus.chunks(), json_out)
    typer.echo(corpus.report(), err=json_out)


@app.command("parse-code")
def parse_code(dump_dir: Path, config: str = typer.Option(..., help="Метка выгрузки, напр. «УТ 11.5.27.75»"),
               json_out: bool = typer.Option(False, "--json")):
    """Разобрать выгрузку конфигурации (XML/BSL) в чанки методов и карточки объектов."""
    from copilot1c.code1c.indexer import config_chunks

    _print(config_chunks(dump_dir, config=config, project=get_settings().project), json_out)


@app.command("entities")
def entities(paths: list[Path]):
    """Показать сущности, найденные regex-извлечением (версии, серверы, документы, объекты КС)."""
    from copilot1c.ingest.entities import extract_regex_entities

    seen: dict[str, str] = {}
    for c in _collect(paths):
        for e in extract_regex_entities(c.text):
            seen.setdefault(e.key, f"{e.kind.value:18} {e.name}")
    for line in sorted(seen.values()):
        typer.echo(line)


@app.command("unpack")
def unpack(file: Path, out_dir: Path, extension: str | None = typer.Option(None, help="Имя расширения для .cfe")):
    """Распаковать .cf/.cfe/.epf в XML/BSL платформой 1С (на ВМ-песочнице)."""
    from copilot1c.code1c.platform import Designer

    for r in Designer().unpack(file, out_dir, extension):
        typer.echo(f"{'OK ' if r.ok else 'ERR'} {' '.join(r.argv[7:])}\n{r.log}")


@app.command("init-db")
def init_db():
    """Создать схему графа и реестров в PostgreSQL."""
    import psycopg

    from copilot1c.graph.store import GraphStore

    try:
        with GraphStore() as g:
            g.init_schema()
    except psycopg.Error as exc:  # понятное сообщение вместо трассировки (например, нет прав на pgvector)
        hint = getattr(exc.diag, "message_hint", None)
        typer.echo(f"Схема не создана: {exc.diag.message_primary or exc}" + (f"\n{hint}" if hint else ""), err=True)
        raise typer.Exit(1) from exc
    typer.echo("Схема создана")


PG_HINT = ("PostgreSQL недоступен ({dsn}) — база поиска, граф и реестры живут в нём. Локально: docker run -d "
           "--name copilot-pg -p 5432:5432 -e POSTGRES_USER=copilot -e POSTGRES_PASSWORD=copilot -e POSTGRES_DB=copilot "
           "pgvector/pgvector:pg16, затем copilot1c init-db и повторный index-docs.")


def _connect():
    from copilot1c.graph.store import try_connect

    s = get_settings()
    g = try_connect(s)
    if g is None:
        typer.echo(PG_HINT.format(dsn=s.pg_dsn.split("@")[-1]), err=True)
        raise typer.Exit(1)
    return g


@app.command("index-docs")
def index_docs(paths: list[Path],
               llm_entities: bool = typer.Option(False, help="Дополнительно извлекать сущности LLM")):
    """Проиндексировать почту и документы в базу поиска PostgreSQL (эмбеддинги AI Studio), граф и реестры."""
    from copilot1c.ingest.entities import extract_regex_entities
    from copilot1c.letters import ingest_emails, results_report
    from copilot1c.search import PgIndex

    s = get_settings()
    corpus = _corpus(paths)
    typer.echo(corpus.report())
    chunks = corpus.chunks(messages=False)
    typer.echo(f"Фрагментов документов: {len(chunks)}")

    with _connect() as g:
        index = PgIndex(g.conn, s)
        new = set(index.add(chunks, progress=typer.echo))
        typer.echo(f"База поиска: новых фрагментов документов {len(new)} из {len(chunks)}")
        results = ingest_emails(g.conn, s, corpus.emails, index)
        mail = results_report(results)
        typer.echo(f"Письма: новых {mail['letters_new']}, уже известных {mail['letters_known']}, "
                   f"веток {len(mail['threads'])}")
        letter_chunks = [x.chunk for r in results for x in r.new_letters]
        chunks += letter_chunks
        new |= {c.chunk_id for c in letter_chunks}
        for d in corpus.documents:
            g.upsert_registries(s.project, d)
        for c in chunks:
            if c.chunk_id not in new:
                continue
            g.upsert_entities(extract_regex_entities(c.entity_text()), c.chunk_id)
            if llm_entities:
                from copilot1c.ingest.entities_llm import extract_llm_entities

                ents, rels = extract_llm_entities(c.text, c.chunk_id)
                g.upsert_entities(ents, c.chunk_id)
                g.upsert_relations(rels)
    typer.echo("Граф и реестры записаны в PostgreSQL")


KNOWLEDGE_TABLES = ("mentions", "relations", "entities", "requirement_tests", "requirements", "test_cases", "letters",
                    "threads", "document_versions", "documents", "chunks", "bsl_calls", "bsl_methods", "md_objects")


@app.command("reset-knowledge")
def reset_knowledge(yes: bool = typer.Option(False, "--yes", help="Подтверждение: без него ничего не удаляется"),
                    materials: bool = typer.Option(True, help="Убрать и реестр загрузок (файлы на диске остаются)")):
    """Очистить базу знаний: фрагменты, письма и ветки, граф, реестры (и реестр загрузок). Обращения, контакты и
    контуры не трогаются."""
    if not yes:
        typer.echo("Удалит все фрагменты, письма и ветки, документы и редакции, граф, реестры тест-кейсов и требований"
                   + (" и реестр загруженных материалов" if materials else "")
                   + ". Обращения, контакты и контуры останутся. Повторите с --yes.")
        raise typer.Exit(1)
    with _connect() as g:
        g.init_schema()
        for t in KNOWLEDGE_TABLES:
            g.conn.execute(f"DELETE FROM {t}")
        if materials:  # обращения ссылаются на материалы (kb_material_id) — ссылки обнулятся
            g.conn.execute("DELETE FROM materials")
    typer.echo("База знаний очищена")


@app.command("contours")
def contours_cmd(add: str = typer.Option(None, help="Добавить: «вид:название», вид — system, process или project"),
                 parent: int = typer.Option(None, help="id родительского контура для --add"),
                 aliases: str = typer.Option("", help="Псевдонимы через запятую для --add"),
                 notes: str = typer.Option(None, help="Пояснения для агента для --add")):
    """Справочник контуров: показать или добавить."""
    from copilot1c.contours import ContourError, ContourRegistry

    s = get_settings()
    with _connect() as g:
        reg = ContourRegistry(g.conn, s.project)
        if add:
            kind, _, name = add.partition(":")
            try:
                c = reg.create({"kind": kind.strip(), "name": name, "parent_id": parent,
                                "aliases": [a for a in aliases.split(",") if a.strip()], "notes": notes})
            except ContourError as exc:
                raise typer.BadParameter(str(exc)) from exc
            typer.echo(f"Добавлен: {c['id']} {c['kind_label']} «{c['name']}»")
            return
        for c in reg.list(include_inactive=True):
            extra = (f" ← {c['parent_id']}" if c["parent_id"] else "") + (f" ({', '.join(c['aliases'])})"
                                                                           if c["aliases"] else "")
            typer.echo(f"{c['id']:>4} {c['kind_label']:8} {c['name']}{extra}{'' if c['active'] else ' [выключен]'}")


@app.command("search")
def search_cmd(query: str, k: int = typer.Option(10), contour: list[int] = typer.Option(None, help="id контура"),
               superseded: bool = typer.Option(False, help="Включать заменённые редакции")):
    """Поиск по базе так, как его видит агент: место в векторном и лексическом списках, итог слияния."""
    from copilot1c.retrieval import smart_search, source_label
    from copilot1c.search import PgIndex

    s = get_settings()
    with _connect() as g:
        index = PgIndex(g.conn, s)
        statuses = ("active", "superseded") if superseded else ("active",)
        hits = smart_search(lambda q, f, kk: index.search(q, filters=f, k=kk, contours=contour or None,
                                                          statuses=statuses), query, {"project": s.project}, k)
    for i, h in enumerate(hits, 1):
        r = h.get("ranks", {})
        typer.echo(f"{i:>2}. {h['score']:.4f} [вектор {r.get('vector') or '–'} / слова {r.get('lexical') or '–'}] "
                   f"{source_label(h['attributes'], h['text'])}")


@app.command("ask")
def ask(question: str, dumps_root: Path = typer.Option(Path("data/dumps")),
        trace: bool = typer.Option(False, help="Показать вызовы инструментов агентом")):
    """Задать вопрос агенту (нужен PostgreSQL: база поиска, граф, реестры, обращения)."""
    from copilot1c.agent.tools import ToolContext, run_agent

    s = get_settings()
    g = _connect()
    try:
        result = run_agent(question, ToolContext(s, dumps_root, g))
        typer.echo(result.answer)
        if trace:
            typer.echo(f"\n— шагов модели: {result.steps}", err=True)
            for t in result.trace:
                status = "повтор" if t.get("repeat") else t.get("error") or f"{t.get('result_chars', 0)} симв."
                typer.echo(f"  {t['tool']}({t['args']}) → {status}", err=True)
    finally:
        g.close()


@app.command("serve")
def serve(host: str = typer.Option("127.0.0.1", help="Только localhost: наружу ядро не выставляется"),
          port: int = typer.Option(8100)):
    """Запустить демон ядра: HTTP API для веба, бота и MCP (под systemd — сервис copilot1c-core)."""
    try:
        import uvicorn

        from copilot1c.server import create_app
    except ImportError as exc:
        raise typer.BadParameter("Нужны зависимости демона: uv sync --extra server") from exc
    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
    uvicorn.run(create_app(start_worker=True), host=host, port=port, log_level="info")


@app.command("classify")
def classify_cmd(text: str, llm: bool = typer.Option(True, help="Уточнять моделью, если эвристики не уверены")):
    """Тип сообщения чата, как его видит ядро: эвристики, вызов модели (COPILOT_MODEL_BATCH), черновик обращения."""
    import json as _json

    from copilot1c.intent import classify, issue_draft

    r = classify(text, settings=get_settings(), use_llm=llm)
    typer.echo(_json.dumps(r.to_dict(), ensure_ascii=False, indent=2))
    if r.is_issue:
        typer.echo("Черновик обращения:\n" + _json.dumps(issue_draft(text), ensure_ascii=False, indent=2))


@app.command("web-search")
def web_search_cmd(query: str, sites: list[str] = typer.Option(None, "--site", help="Ограничить сайтом (можно несколько)"),
                   read: bool = typer.Option(False, help="Прочитать первую найденную страницу")):
    """Проверить поиск в интернете (Yandex Search API) так, как его видит агент: что ушло и что вернулось."""
    from copilot1c.web import WebError, read_page, web_search

    s = get_settings()
    try:
        found = web_search(query, s, sites=sites)
    except WebError as exc:
        raise typer.Exit(typer.echo(f"Ошибка: {exc}") or 1) from exc
    typer.echo(f"Отправлено: {found['query_sent']}")
    for r in found["removed"]:
        typer.echo(f"  вырезано — {r}")
    for i, r in enumerate(found["results"], 1):
        typer.echo(f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet'][:200]}")
    if read and found["results"]:
        page = read_page(found["results"][0]["url"], s)
        typer.echo(f"\n--- {page['title']} ({len(page['text'])} знаков) ---\n{page['text'][:1500]}")


@app.command("eval")
def eval_cmd(golden: Path = typer.Argument(..., help="JSON с эталонными вопросами (см. tests/eval/example.json)"),
             answers: bool = typer.Option(False, help="Проверять и ответы агента (дольше и дороже)"),
             k: int = typer.Option(10, help="Сколько чанков смотреть в поиске"),
             dumps_root: Path = typer.Option(Path("data/dumps"))):
    """Оценка качества: recall@k поиска и наличие обязательных фактов в ответах агента."""
    from copilot1c import eval as ev
    from copilot1c.retrieval import smart_search
    from copilot1c.search import PgIndex, search_fn

    s = get_settings()
    cases = ev.load_cases(golden)
    store = _connect()
    raw = search_fn(PgIndex(store.conn, s))

    def search(question: str, filters: dict, top_k: int) -> list[dict]:
        return smart_search(raw, question, {"project": s.project, **filters}, top_k)

    answer = None
    if answers:
        from copilot1c.agent.tools import ToolContext, run_agent

        ctx = ToolContext(s, dumps_root, store)

        def answer(question: str):
            return run_agent(question, ctx)

    typer.echo(f"Вопросов: {len(cases)}")
    try:
        results = ev.run(cases, search, answer, k=k, progress=typer.echo)
    finally:
        store.close()
    typer.echo(ev.report_text(results, k))
    typer.echo(f"Подробно: {ev.save(results, k, Path(s.cache_dir or '.cache') / 'eval')}")


if __name__ == "__main__":
    app()
