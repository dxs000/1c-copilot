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


@app.command("index-docs")
def index_docs(paths: list[Path], vector_store: str = typer.Option(..., envvar="COPILOT_VECTOR_STORE_ID"),
               llm_entities: bool = typer.Option(False, help="Дополнительно извлекать сущности LLM")):
    """Проиндексировать почту и документы: Vector Store + граф и реестры в PostgreSQL."""
    from copilot1c.graph.store import GraphStore
    from copilot1c.index.yandex import VectorIndex
    from copilot1c.ingest.entities import extract_regex_entities

    corpus = _corpus(paths)
    chunks = corpus.chunks()
    ids = VectorIndex(vector_store).add(chunks)
    project = get_settings().project
    with GraphStore() as g:
        g.upsert_chunks(chunks, {c.chunk_id: i for c, i in zip(chunks, ids, strict=True)})
        for d in corpus.documents:
            g.upsert_registries(project, d)
        for c in chunks:
            g.upsert_entities(extract_regex_entities(c.entity_text()), c.chunk_id)
            if llm_entities:
                from copilot1c.ingest.entities_llm import extract_llm_entities

                ents, rels = extract_llm_entities(c.text, c.chunk_id)
                g.upsert_entities(ents, c.chunk_id)
                g.upsert_relations(rels)
    typer.echo(corpus.report())
    typer.echo(f"Проиндексировано чанков: {len(chunks)}")


@app.command("create-index")
def create_index(name: str):
    """Создать Vector Store в AI Studio и вывести его id."""
    from copilot1c.index.yandex import VectorIndex

    typer.echo(VectorIndex.create(name).id)


@app.command("ask")
def ask(question: str, vector_store: str = typer.Option(..., envvar="COPILOT_VECTOR_STORE_ID"),
        dumps_root: Path = typer.Option(Path("data/dumps"))):
    """Задать вопрос агенту."""
    from copilot1c.agent.tools import ToolContext
    from copilot1c.agent.tools import ask as agent_ask
    from copilot1c.graph.store import GraphStore

    s = get_settings()
    with GraphStore() as g:
        typer.echo(agent_ask(question, ToolContext(s, vector_store, dumps_root, g)))


if __name__ == "__main__":
    app()
