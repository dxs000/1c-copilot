"""Общие структуры данных: чанки индекса, сущности и связи графа."""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class DocType(StrEnum):
    EMAIL = "email"
    TZ = "tz"  # техническое задание
    DS = "ds"  # дополнительное соглашение
    PIMI = "pimi"  # программа и методика испытаний
    DOC = "doc"  # прочий документ
    BSL_METHOD = "bsl_method"
    MD_OBJECT = "md_object"  # карточка объекта метаданных
    STANDARD = "standard"  # стандарты разработки, справка


class Chunk(BaseModel):
    """Единица индекса. Текст + метаданные для фильтров Vector Store и ссылок на источник."""

    text: str
    doc_type: DocType
    source: str  # путь к оригиналу в Object Storage или в выгрузке
    title: str = ""
    project: str = ""
    doc_version: str | None = None
    date: datetime | None = None
    author: str | None = None
    objects: list[str] = Field(default_factory=list)  # объекты метаданных 1С
    extra: dict[str, str] = Field(default_factory=dict)
    # Содержательная часть без шапки (название документа, раздел): по ней извлекаются сущности,
    # чтобы название документа не «упоминало» одни и те же версии в каждом чанке
    body: str = ""

    def entity_text(self) -> str:
        return self.body or self.text

    @property
    def chunk_id(self) -> str:
        h = hashlib.sha1(f"{self.source}\n{self.title}\n{self.text}".encode()).hexdigest()
        return h[:16]

    def attributes(self) -> dict[str, str]:
        """Плоские атрибуты для фильтров поиска (Vector Store принимает строки)."""
        attrs = {
            "title": self.title[:200],
            "doc_type": self.doc_type.value,
            "source": self.source,
            "project": self.project,
        }
        if self.doc_version:
            attrs["doc_version"] = self.doc_version
        if self.date:
            attrs["date"] = self.date.date().isoformat()
        if self.author:
            attrs["author"] = self.author
        if self.objects:
            attrs["objects"] = ",".join(sorted(set(self.objects)))
        attrs.update(self.extra)
        return attrs


class EntityKind(StrEnum):
    PERSON = "person"
    ORGANIZATION = "organization"
    DOCUMENT = "document"
    SOFTWARE_VERSION = "software_version"
    SERVER = "server"
    MD_OBJECT = "md_object"
    DECISION = "decision"
    OPEN_QUESTION = "open_question"
    REQUIREMENT = "requirement"
    TEST_CASE = "test_case"


class Entity(BaseModel):
    kind: EntityKind
    name: str
    attrs: dict[str, str] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.kind.value}:{self.name.casefold()}"


class Relation(BaseModel):
    src: str  # Entity.key
    dst: str
    rel: str  # mentions, decides, version_of, tests, implements, calls, intercepts…
    source_chunk: str | None = None
