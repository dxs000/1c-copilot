"""Запись чанков, сущностей и связей в PostgreSQL."""

from __future__ import annotations

import json
from collections.abc import Iterable
from importlib import resources

import psycopg
from psycopg.rows import dict_row

from copilot1c.config import Settings, get_settings
from copilot1c.ingest.docx import TestCase
from copilot1c.models import Chunk, Entity, Relation


class GraphStore:
    def __init__(self, dsn: str | None = None, settings: Settings | None = None):
        s = settings or get_settings()
        self.conn = psycopg.connect(dsn or s.pg_dsn, autocommit=False)

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

    def upsert_test_cases(self, project: str, doc: str, cases: Iterable[TestCase]) -> None:
        with self.conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO test_cases VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (project, doc, num) DO UPDATE SET result = EXCLUDED.result""",
                [(project, doc, t.num, t.section, t.function, t.method, t.criterion, t.result, t.objects)
                 for t in cases],
            )

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        """Только чтение: используется инструментом агента sql."""
        self.conn.commit()  # SET TRANSACTION должен открывать новую транзакцию
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(sql, params)
            rows = cur.fetchall()
        self.conn.rollback()
        return rows
