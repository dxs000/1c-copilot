"""Запись чанков, сущностей и связей в PostgreSQL."""

from __future__ import annotations

import json
from collections.abc import Iterable
from importlib import resources

import psycopg
from psycopg.rows import dict_row

from copilot1c.config import Settings, get_settings
from copilot1c.ingest.document import ParsedDocument
from copilot1c.models import Chunk, Entity, Relation


def try_connect(settings: Settings | None = None) -> GraphStore | None:
    """Граф необязателен: без PostgreSQL индексация и поиск работают, недоступны только реестры и SQL."""
    try:
        return GraphStore(settings=settings)
    except psycopg.OperationalError:
        return None


class GraphStore:
    def __init__(self, dsn: str | None = None, settings: Settings | None = None):
        s = settings or get_settings()
        self.conn = psycopg.connect(dsn or s.pg_dsn, autocommit=False, connect_timeout=5)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> GraphStore:
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()
        self.close()

    def init_schema(self) -> None:
        sql = resources.files("copilot1c.graph").joinpath("schema.sql").read_text(encoding="utf-8")
        self.conn.execute(sql)

    def upsert_chunks(self, chunks: Iterable[Chunk], vs_ids: dict[str, str] | None = None) -> None:
        vs_ids = vs_ids or {}
        with self.conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO chunks (chunk_id, project, doc_type, source, title, doc_version, created_at,
                                       author, objects, attrs, text, vs_file_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (chunk_id) DO UPDATE SET vs_file_id = COALESCE(EXCLUDED.vs_file_id,
                                                                               chunks.vs_file_id)""",
                [
                    (c.chunk_id, c.project, c.doc_type.value, c.source, c.title, c.doc_version, c.date,
                     c.author, c.objects, json.dumps(c.extra, ensure_ascii=False), c.text,
                     vs_ids.get(c.chunk_id))
                    for c in chunks
                ],
            )

    def upsert_entities(self, entities: Iterable[Entity], chunk_id: str | None = None) -> None:
        with self.conn.cursor() as cur:
            for e in entities:
                cur.execute(
                    """INSERT INTO entities (key, kind, name, attrs) VALUES (%s,%s,%s,%s)
                       ON CONFLICT (key) DO UPDATE SET attrs = entities.attrs || EXCLUDED.attrs""",
                    (e.key, e.kind.value, e.name, json.dumps(e.attrs, ensure_ascii=False)),
                )
                if chunk_id:
                    cur.execute("INSERT INTO mentions VALUES (%s,%s) ON CONFLICT DO NOTHING", (e.key, chunk_id))

    def upsert_relations(self, relations: Iterable[Relation]) -> None:
        with self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO relations VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                [(r.src, r.rel, r.dst, r.source_chunk) for r in relations],
            )

    def upsert_registries(self, project: str, doc: ParsedDocument) -> None:
        """Тест-кейсы, пункты плана и покрытие документа — в реестры для SQL-аналитики агента."""
        name = doc.title[:200] + (f" (ред. {doc.version})" if doc.version else "")
        with self.conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO test_cases (project, doc, num, section, function, steps, result, objects, source)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (project, doc, num) DO UPDATE SET steps = EXCLUDED.steps, result = EXCLUDED.result""",
                [(project, name, t.num, t.section, t.function,
                  json.dumps([vars(st) for st in t.steps], ensure_ascii=False), t.result, t.objects, doc.source)
                 for t in doc.test_cases],
            )
            cur.executemany(
                """INSERT INTO requirements (project, req_id, doc, grp, object, text, objects, source)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (project, req_id, doc) DO UPDATE SET text = EXCLUDED.text, objects = EXCLUDED.objects""",
                [(project, p.num, name, p.group, p.object, p.text(), p.objects, doc.source) for p in doc.plan_items],
            )
            cur.executemany(
                """INSERT INTO requirement_tests (project, req_id, doc, test_doc, test_num, coverage)
                   VALUES (%s,%s,%s,%s,'*',%s) ON CONFLICT DO NOTHING""",
                [(project, item, name, c.document, c.coverage) for c in doc.coverage for item in c.items],
            )

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        """Только чтение: используется инструментом агента sql."""
        self.conn.commit()  # SET TRANSACTION должен открывать новую транзакцию
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(sql, params or None)  # без параметров «%» в LIKE не считается плейсхолдером
            rows = cur.fetchall()
        self.conn.rollback()
        return rows
