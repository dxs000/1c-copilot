"""Командная строка: copilot1c <команда>.

Команды разбора (parse-*) работают локально без облака — удобно проверять чанкинг на своих файлах.
Команды index-* и ask требуют настроенного .env (AI Studio, PostgreSQL).
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
    from copilot1c.graph.store import GraphStore

    with GraphStore() as g:
        g.init_schema()
    typer.echo("Схема создана")


def _vector_store(value: str | None) -> str:
    """Идентификатор индекса: опция --vector-store, иначе COPILOT_VECTOR_STORE_ID из окружения или .env."""
    vs = value or get_settings().vector_store_id
    if not vs:
        raise typer.BadParameter("Задайте COPILOT_VECTOR_STORE_ID в .env или передайте --vector-store "
                                 "(создать индекс: copilot1c create-index <имя>)")
    return vs


PG_HINT = ("PostgreSQL недоступен ({dsn}) — граф и реестры (тест-кейсы, план тестирования, покрытие) не записаны; "
           "поиск по Vector Store работает. Локально: docker run -d --name copilot-pg -p 5432:5432 "
           "-e POSTGRES_USER=copilot -e POSTGRES_PASSWORD=copilot -e POSTGRES_DB=copilot pgvector/pgvector:pg16, "
           "затем copilot1c init-db и повторный index-docs (уже загруженные чанки не загружаются заново).")


@app.command("index-docs")
def index_docs(paths: list[Path], vector_store: str | None = typer.Option(None, help="По умолчанию из .env"),
               graph: bool = typer.Option(True, help="Записывать граф и реестры в PostgreSQL, если он доступен"),
               llm_entities: bool = typer.Option(False, help="Дополнительно извлекать сущности LLM")):
    """Проиндексировать почту и документы: Vector Store + (если доступен) граф и реестры в PostgreSQL."""
    from copilot1c.graph.store import try_connect
    from copilot1c.index.yandex import VectorIndex
    from copilot1c.ingest.entities import extract_regex_entities

    s = get_settings()
    corpus = _corpus(paths)
    typer.echo(corpus.report())
    chunks = corpus.chunks()
    typer.echo(f"Чанков: {len(chunks)}")

    index = VectorIndex(_vector_store(vector_store))
    if not index.load_manifest():
        try:  # индекс мог быть заполнен прошлым запуском, упавшим до записи манифеста
            index.rebuild_manifest(typer.echo)
        except Exception as exc:  # noqa: BLE001 — список файлов индекса не критичен
            typer.echo(f"Не удалось прочитать содержимое индекса ({type(exc).__name__}); если индекс не пустой, "
                       "возможны дубли — надёжнее создать новый: copilot1c create-index <имя>", err=True)
    ids = index.add(chunks, progress=typer.echo)
    typer.echo(f"Vector Store: в индексе {len(ids)} чанков этого корпуса")

    g = try_connect(s) if graph else None
    if g is None:
        if graph:
            typer.echo(PG_HINT.format(dsn=s.pg_dsn.split("@")[-1]), err=True)
        return
    with g:
        g.upsert_chunks(chunks, ids)
        for d in corpus.documents:
            g.upsert_registries(s.project, d)
        for c in chunks:
            g.upsert_entities(extract_regex_entities(c.entity_text()), c.chunk_id)
            if llm_entities:
                from copilot1c.ingest.entities_llm import extract_llm_entities

                ents, rels = extract_llm_entities(c.text, c.chunk_id)
                g.upsert_entities(ents, c.chunk_id)
                g.upsert_relations(rels)
    typer.echo("Граф и реестры записаны в PostgreSQL")


@app.command("create-index")
def create_index(name: str):
    """Создать Vector Store в AI Studio и вывести его id."""
    from copilot1c.index.yandex import VectorIndex

    typer.echo(VectorIndex.create(name).id)


@app.command("ask")
def ask(question: str, vector_store: str | None = typer.Option(None, help="По умолчанию из .env"),
        dumps_root: Path = typer.Option(Path("data/dumps"))):
    """Задать вопрос агенту. Без PostgreSQL работает только поиск по документам и коду."""
    from copilot1c.agent.tools import ToolContext
    from copilot1c.agent.tools import ask as agent_ask
    from copilot1c.graph.store import try_connect

    s = get_settings()
    g = try_connect(s)
    if g is None:
        typer.echo("PostgreSQL недоступен: граф и SQL-реестры отключены, отвечаю по поиску в Vector Store.", err=True)
    try:
        typer.echo(agent_ask(question, ToolContext(s, _vector_store(vector_store), dumps_root, g)))
    finally:
        if g is not None:
            g.close()


if __name__ == "__main__":
    app()
