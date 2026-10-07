"""Поиск по базе проекта в PostgreSQL: векторы (pgvector) + полнотекстовый поиск с русской морфологией.

Почему не AI Studio Vector Store: там один фрагмент — один файл, а лимит — 10 000 файлов на индекс; удаление,
редакции и фильтр по контурам пришлось бы синхронизировать отдельно. Здесь фрагмент — строка таблицы chunks:
удаление и смена статуса — обычный UPDATE/DELETE, фильтр по контурам — условие WHERE.

Как ищется:
1. Векторная часть: эмбеддинг запроса (модель запросов AI Studio) против эмбеддингов фрагментов (модель
   документов), косинусная близость, точный перебор. На масштабе пилота (десятки тысяч фрагментов) это
   миллисекунды; при сотнях тысяч добавить HNSW-индекс.
2. Лексическая часть: леммы запроса объединяются через «или» и ранжируются ts_rank_cd по колонке tsv
   (russian). Номера версий («11.5.19.55», «8.3.27.2342») словарь хранит целиком — они находятся точно,
   чего не умеет векторный поиск.
3. Слияние RRF (reciprocal rank fusion): место в каждом списке даёт 1 / (60 + место), суммы сортируются.

Результат — в том же виде, что раньше отдавал Vector Store ({score, chunk_id, attributes, text}), поэтому
retrieval.smart_search, format_hits и source_label работают без изменений.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from psycopg.rows import dict_row

from copilot1c.config import Settings, get_settings
from copilot1c.models import Chunk

log = logging.getLogger("copilot1c.search")

RRF_K = 60
CANDIDATES = 40          # сколько кандидатов берёт каждая часть до слияния
ACTIVE = "active"        # статусы фрагментов: active — в поиске; superseded — заменён новой редакцией
STATUSES = ("active", "superseded")
# Ключи фильтров, которые лежат в колонках таблицы, а не в attrs
_COLUMNS = {"project": "project", "doc_type": "doc_type", "doc_version": "doc_version", "source": "source"}

EmbedFn = Callable[[Sequence[str], bool], list[list[float]]]


def _default_embed(settings: Settings) -> EmbedFn:
    from copilot1c.index.yandex import embed

    return lambda texts, query: embed(texts, query=query, settings=settings)


def vector_literal(v: Sequence[float]) -> str:
    return "[" + ",".join(f"{x:.6g}" for x in v) + "]"


class PgIndex:
    """Фрагменты базы проекта в таблице chunks. conn — открытое подключение psycopg (коммит — за вызывающим,
    кроме add, который коммитит пачками, чтобы долгая загрузка не терялась целиком при сбое)."""

    def __init__(self, conn, settings: Settings | None = None, embed: EmbedFn | None = None):
        self.conn = conn
        self.s = settings or get_settings()
        self._embed = embed or _default_embed(self.s)
        self._query_cache: dict[str, list[float]] = {}

    # ---------- запись ----------

    def known_ids(self, ids: Iterable[str] | None = None) -> set[str]:
        """chunk_id, у которых уже есть эмбеддинг (повторная загрузка их пропускает)."""
        with self.conn.cursor() as cur:
            if ids is None:
                cur.execute("SELECT chunk_id FROM chunks WHERE embedding IS NOT NULL")
            else:
                cur.execute("SELECT chunk_id FROM chunks WHERE embedding IS NOT NULL AND chunk_id = ANY(%s)",
                            (list(ids),))
            return {r[0] for r in cur.fetchall()}

    def add(self, chunks: Iterable[Chunk], *, material_id: int | None = None, contours: Sequence[int] = (),
            progress: Callable[[str], None] | None = None, every: int = 25) -> list[str]:
        """Записывает фрагменты, которых ещё нет, с эмбеддингами. Возвращает chunk_id новых фрагментов.
        Уже записанные не трогаются (материал и контуры остаются от первого появления)."""
        chunks = list({c.chunk_id: c for c in chunks}.values())
        have = self.known_ids(c.chunk_id for c in chunks)
        todo = [c for c in chunks if c.chunk_id not in have]
        if progress and have:
            progress(f"Уже в базе: {len(have)}, новых фрагментов: {len(todo)}")
        done: list[str] = []
        for i in range(0, len(todo), every):
            part = todo[i:i + every]
            vectors = self._embed([c.text for c in part], False)
            with self.conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO chunks (chunk_id, project, doc_type, source, title, doc_version, created_at, author,
                                           objects, attrs, text, embedding, material_id, contours, status)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector,%s,%s,'active')
                       ON CONFLICT (chunk_id) DO UPDATE SET embedding = EXCLUDED.embedding,
                           attrs = EXCLUDED.attrs,
                           material_id = COALESCE(chunks.material_id, EXCLUDED.material_id)""",
                    [(c.chunk_id, c.project, c.doc_type.value, c.source, c.title, c.doc_version, c.date, c.author,
                      c.objects, json.dumps(c.attributes(), ensure_ascii=False), c.text, vector_literal(v),
                      material_id, list(contours))
                     for c, v in zip(part, vectors, strict=True)])
            self.conn.commit()
            done += [c.chunk_id for c in part]
            if progress:
                progress(f"  записано {len(done)}/{len(todo)}")
        return done

    def set_status(self, chunk_ids: Sequence[str], status: str) -> int:
        if status not in STATUSES:
            raise ValueError(f"неизвестный статус фрагмента: {status}")
        with self.conn.cursor() as cur:
            cur.execute("UPDATE chunks SET status = %s WHERE chunk_id = ANY(%s)", (status, list(chunk_ids)))
            return cur.rowcount

    def delete_material(self, material_id: int) -> int:
        """Убирает из базы фрагменты материала и всё, что на них ссылается (упоминания, связи)."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT chunk_id FROM chunks WHERE material_id = %s", (material_id,))
            ids = [r[0] for r in cur.fetchall()]
            if not ids:
                return 0
            cur.execute("DELETE FROM mentions WHERE chunk_id = ANY(%s)", (ids,))
            cur.execute("UPDATE relations SET source_chunk = NULL WHERE source_chunk = ANY(%s)", (ids,))
            cur.execute("DELETE FROM chunks WHERE chunk_id = ANY(%s)", (ids,))
            return len(ids)

    # ---------- поиск ----------

    def _query_vector(self, query: str) -> list[float]:
        if query not in self._query_cache:  # smart_search ищет один запрос с разными фильтрами
            self._query_cache[query] = self._embed([query], True)[0]
        return self._query_cache[query]

    @staticmethod
    def _where(filters: dict[str, str] | None, contours: Sequence[int] | None,
               statuses: Sequence[str]) -> tuple[str, list[Any]]:
        conds, params = ["c.status = ANY(%s)"], [list(statuses)]
        for key, value in (filters or {}).items():
            if value is None or value == "":
                continue
            if key in _COLUMNS:
                conds.append(f"c.{_COLUMNS[key]} = %s")
            else:
                conds.append("c.attrs ->> %s = %s")
                params.append(key)
            params.append(str(value))
        if contours:
            conds.append("c.contours && %s::bigint[]")
            params.append(list(contours))
        return " AND ".join(conds), params

    def search(self, query: str, *, filters: dict[str, str] | None = None, k: int = 10,
               contours: Sequence[int] | None = None, statuses: Sequence[str] = (ACTIVE,)) -> list[dict[str, Any]]:
        where, wparams = self._where(filters, contours, statuses)
        qvec = vector_literal(self._query_vector(query))
        sql = f"""
            WITH q AS (
                SELECT to_tsquery('simple', string_agg(quote_literal(lexeme), ' | ')) AS tsq
                FROM unnest(to_tsvector('russian', %s))
            ),
            vec AS (
                SELECT c.chunk_id, row_number() OVER (ORDER BY c.embedding <=> %s::vector) AS rnk
                FROM chunks c
                WHERE {where} AND c.embedding IS NOT NULL
                ORDER BY c.embedding <=> %s::vector
                LIMIT {CANDIDATES}
            ),
            lex AS (
                SELECT c.chunk_id, row_number() OVER (ORDER BY ts_rank_cd(c.tsv, q.tsq) DESC) AS rnk
                FROM chunks c, q
                WHERE {where} AND q.tsq IS NOT NULL AND c.tsv @@ q.tsq
                ORDER BY ts_rank_cd(c.tsv, q.tsq) DESC
                LIMIT {CANDIDATES}
            ),
            fused AS (
                SELECT chunk_id, sum(1.0 / ({RRF_K} + rnk)) AS score,
                       min(rnk) FILTER (WHERE src = 'v') AS vec_rank,
                       min(rnk) FILTER (WHERE src = 'l') AS lex_rank
                FROM (SELECT chunk_id, rnk, 'v' AS src FROM vec UNION ALL SELECT chunk_id, rnk, 'l' FROM lex) u
                GROUP BY chunk_id
            )
            SELECT f.chunk_id, f.score, f.vec_rank, f.lex_rank, c.attrs, c.text, c.status, c.contours, c.material_id
            FROM fused f JOIN chunks c USING (chunk_id)
            ORDER BY f.score DESC, f.chunk_id
            LIMIT %s"""
        params = [query, qvec, *wparams, qvec, *wparams, k]
        try:
            with self.conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        except Exception:
            self.conn.rollback()  # прерванная транзакция не должна ломать следующие запросы того же подключения
            raise
        return [{"score": float(r["score"]), "chunk_id": r["chunk_id"], "file_id": r["chunk_id"],
                 "attributes": {**(r["attrs"] or {}), "status": r["status"]}, "text": r["text"],
                 "contours": list(r["contours"] or []), "material_id": r["material_id"],
                 "ranks": {"vector": r["vec_rank"], "lexical": r["lex_rank"]}} for r in rows]

    def stats(self) -> dict[str, int]:
        with self.conn.cursor() as cur:
            cur.execute("""SELECT count(*), count(*) FILTER (WHERE embedding IS NOT NULL),
                                  count(*) FILTER (WHERE status = 'active') FROM chunks""")
            total, embedded, active = cur.fetchone()
        return {"chunks": total, "embedded": embedded, "active": active}


def search_fn(index: PgIndex, contours: Sequence[int] | None = None):
    """Функция поиска в форме, которую ждёт retrieval.smart_search: (query, filters, k) → hits."""
    return lambda q, f, k: index.search(q, filters=f, k=k, contours=contours)
