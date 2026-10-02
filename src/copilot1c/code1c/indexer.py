"""Чанки индекса из выгрузки конфигурации: методы BSL и карточки объектов метаданных."""

from __future__ import annotations

from pathlib import Path

from copilot1c.code1c.bsl import Method, iter_modules, module_owner, parse_module
from copilot1c.code1c.metadata import iter_objects
from copilot1c.ingest.entities import is_custom_object
from copilot1c.models import Chunk, DocType


def method_chunk(m: Method, *, config: str, project: str = "", description: str = "") -> Chunk:
    owner = module_owner(m.module)
    header = [f"{owner} · {m.module}:{m.start_line}-{m.end_line}", m.signature]
    if m.context:
        header.append("Контекст: " + ", ".join(m.context))
    if m.intercepts:
        header.append("Перехват: " + ", ".join(f"&{a}(\"{t}\")" for a, t in m.intercepts))
    if description:
        header.append(f"Описание: {description}")
    obj_name = owner.split(".", 1)[-1]
    return Chunk(
        text="\n".join(header) + "\n\n" + m.text,
        doc_type=DocType.BSL_METHOD,
        source=f"{config}/{m.module}#L{m.start_line}",
        title=f"{owner}.{m.name}",
        project=project,
        doc_version=config,
        objects=[owner, *m.md_refs],
        extra={
            "module": m.module,
            "method": m.name,
            "export": str(m.export).lower(),
            "custom": str(is_custom_object(obj_name) or is_custom_object(m.name)).lower(),
            "intercept": str(bool(m.intercepts)).lower(),
        },
    )


def config_chunks(dump_root: str | Path, *, config: str, project: str = "",
                  descriptions: dict[str, str] | None = None) -> list[Chunk]:
    """config — метка версии выгрузки, например «УТ 11.5.27.75» или «КС_Доработки 1.0.3»."""
    descriptions = descriptions or {}
    chunks: list[Chunk] = []
    for rel, text in iter_modules(dump_root):
        for m in parse_module(text, rel):
            key = f"{rel}::{m.name}"
            chunks.append(method_chunk(m, config=config, project=project, description=descriptions.get(key, "")))
    for md in iter_objects(dump_root):
        chunks.append(
            Chunk(
                text=md.card(),
                doc_type=DocType.MD_OBJECT,
                source=f"{config}/{Path(md.source).name}",
                title=md.full_name,
                project=project,
                doc_version=config,
                objects=[md.full_name],
                extra={"custom": str(md.custom).lower(), "adopted": str(md.adopted).lower()},
            )
        )
    return chunks
