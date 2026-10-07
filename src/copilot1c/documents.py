"""Документы и их редакции.

Документ — логическая единица («Бизнес-требования: командировки и билеты»), редакция — конкретный файл
его содержания (docx ред. 2, затем ред. 3). Новая редакция становится текущей; фрагменты прежней получают
status = 'superseded' и выходят из поиска по умолчанию (остаются для вопросов «что поменялось»).

Как узнать, что пришло:
- тот же отпечаток содержания (corpus._doc_fingerprint) — та же редакция, ничего нового;
- похожее название того же вида — вероятно, новая редакция существующего документа (решает аналитик);
- иначе — новый документ.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from psycopg.rows import dict_row

KINDS = {
    "tz": "техническое задание",
    "ds": "допсоглашение",
    "pimi": "ПиМИ",
    "requirements": "бизнес-требования",
    "process": "описание процесса (AS-IS / TO-BE)",
    "protocol": "протокол / решение",
    "instruction": "инструкция",
    "report": "отчёт / выгрузка",
    "email": "письмо / переписка",
    "image": "скриншот / изображение",
    "other": "прочее",
}
_KIND_PATTERNS = [
    ("pimi", r"программ\w*\s+и\s+методик\w*\s+испытан|(^|[^а-яё])п(и)?ми([^а-яё]|$)"),
    ("ds", r"дополнительн\w*\s+соглашени|(^|[^а-яё])дс\s*№?\s*\d"),
    ("tz", r"техническ\w*\s+задани|(^|[^а-яё])тз([^а-яё]|$)"),
    ("requirements", r"бизнес[\s-]*требован|функциональн\w*\s+требован|(^|[^а-яё])бт([^а-яё]|$)|"
                     r"правила\s+и\s+ограничени"),
    ("process", r"as[\s_-]*is|to[\s_-]*be|описани\w*\s+(бизнес[\s-]*)?процесс|(^|[^а-яё])бп\s+as|схем\w*\s+процесс"),
    ("protocol", r"протокол|решени\w*\s+совещани|итоги\s+встреч"),
    ("instruction", r"инструкци|руководств\w*\s+пользовател|регламент"),
    ("report", r"отч[её]т|выгрузк|реестр|оборотн"),
]
_STOP = {"и", "в", "на", "по", "с", "для", "о", "об", "к", "из", "от", "до", "ред", "редакция", "версия", "ver", "v",
         "docx", "doc", "pdf", "xlsx", "final", "итог", "итоговая", "новая", "копия", "copy"}


def content_fingerprint(d) -> str:
    """Отпечаток содержания документа без названия: название часто берётся из имени файла, а тот же документ
    приходит под разными именами («копия требований.docx»)."""
    import hashlib

    parts = [p for sec in d.sections for p in sec.paragraphs] + [t.text() for t in d.test_cases] \
        + [p.text() for p in d.plan_items] + [" | ".join(r.values.values()) for t in d.tables for r in t.rows]
    norm = re.sub(r"\W+", "", "".join(parts).casefold())
    if not norm:
        norm = re.sub(r"\W+", "", (d.title or "").casefold())
    return hashlib.sha1(norm.encode()).hexdigest()


def guess_kind(*texts: str) -> str:
    for text in texts:
        low = (text or "").casefold().replace("_", " ")
        for kind, pattern in _KIND_PATTERNS:
            if re.search(pattern, low):
                return kind
    return "other"


def title_words(title: str) -> set[str]:
    words = re.findall(r"[a-zа-яё]+", (title or "").casefold().replace("ё", "е").replace("_", " "))
    return {w[:6] for w in words if w not in _STOP and len(w) > 1}  # первые 6 букв — грубая основа слова


def title_similarity(a: str, b: str) -> float:
    wa, wb = title_words(a), title_words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


SIMILAR_TITLE = 0.75


class DocumentRegistry:
    def __init__(self, conn, project: str):
        self.conn, self.project = conn, project

    def _rows(self, sql: str, params: tuple | list) -> list[dict[str, Any]]:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(sql, params)
            return cur.fetchall() if cur.description else []

    def by_fingerprint(self, fingerprint: str) -> dict[str, Any] | None:
        rows = self._rows("""SELECT v.*, d.title, d.kind FROM document_versions v JOIN documents d ON d.id = v.document_id
                             WHERE d.project = %s AND v.fingerprint = %s ORDER BY v.id LIMIT 1""",
                          (self.project, fingerprint))
        return rows[0] if rows else None

    def similar(self, title: str, kind: str | None = None, limit: int = 3) -> list[dict[str, Any]]:
        """Документы с похожим названием (кандидаты «новая редакция»), лучшие первыми."""
        rows = self._rows("""SELECT d.*, v.version_label, v.doc_date, v.filename AS current_filename
                             FROM documents d LEFT JOIN document_versions v ON v.id = d.current_version_id
                             WHERE d.project = %s""", (self.project,))
        scored = []
        for r in rows:
            score = title_similarity(title, r["title"])
            if kind and kind != "other" and r["kind"] not in (kind, "other"):
                score *= 0.8  # другой вид документа — менее вероятно та же бумага
            if score >= SIMILAR_TITLE:
                scored.append({**r, "score": round(score, 2)})
        return sorted(scored, key=lambda x: -x["score"])[:limit]

    def get(self, doc_id: int) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM documents WHERE project = %s AND id = %s", (self.project, doc_id))
        if not rows:
            return None
        d = rows[0]
        d["versions"] = self._rows("SELECT * FROM document_versions WHERE document_id = %s ORDER BY id", (doc_id,))
        return d

    def list(self, q: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        return self._rows("""SELECT d.*, v.version_label, v.doc_date, v.filename AS current_filename,
                                    (SELECT count(*) FROM document_versions x WHERE x.document_id = d.id) AS versions
                             FROM documents d LEFT JOIN document_versions v ON v.id = d.current_version_id
                             WHERE d.project = %s AND (%s::text IS NULL OR d.title ILIKE '%%' || %s || '%%')
                             ORDER BY d.updated_at DESC LIMIT %s""", (self.project, q, q, limit))

    def add_version(self, *, title: str, kind: str, source: str, filename: str, fingerprint: str,
                    chunk_ids: list[str], material_id: int | None = None, version_label: str | None = None,
                    doc_date: date | None = None, contours: list[int] | None = None,
                    document_id: int | None = None) -> dict[str, Any]:
        """Новая редакция: документа document_id (прежняя текущая → superseded вместе с фрагментами) или нового
        документа. Фрагменты помечаются attrs.document_id / attrs.version_id."""
        from psycopg.types.json import Jsonb

        superseded = 0
        if document_id is not None and self.get(document_id) is None:
            document_id = None  # документ удалили — заводим заново
        if document_id is None:
            document_id = self._rows("""INSERT INTO documents (project, title, kind, contours) VALUES (%s, %s, %s, %s)
                                        RETURNING id""", (self.project, title[:500], kind, list(contours or [])))[0]["id"]
        vid = self._rows("""INSERT INTO document_versions (document_id, material_id, source, filename, version_label,
                                doc_date, fingerprint, chunks) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                         (document_id, material_id, source, filename, version_label, doc_date, fingerprint,
                          len(chunk_ids)))[0]["id"]
        old = self._rows("""SELECT id FROM document_versions WHERE document_id = %s AND id <> %s AND status = 'current'""",
                         (document_id, vid))
        if old:
            old_ids = [str(r["id"]) for r in old]
            self._rows("UPDATE document_versions SET status = 'superseded' WHERE id = ANY(%s)", ([r["id"] for r in old],))
            rows = self._rows("""UPDATE chunks SET status = 'superseded' WHERE attrs->>'version_id' = ANY(%s)
                                 AND status = 'active' RETURNING chunk_id""", (old_ids,))
            superseded = len(rows)
        if chunk_ids:
            self._rows("UPDATE chunks SET attrs = attrs || %s WHERE chunk_id = ANY(%s)",
                       (Jsonb({"document_id": str(document_id), "version_id": str(vid)}), chunk_ids))
        self._rows("""UPDATE documents SET current_version_id = %s, updated_at = now(),
                          contours = (SELECT array(SELECT DISTINCT x FROM unnest(contours || %s::bigint[]) x ORDER BY x))
                      WHERE id = %s""", (vid, list(contours or []), document_id))
        return {"document_id": document_id, "version_id": vid, "superseded_chunks": superseded,
                "superseded_versions": len(old)}


def forget_material(conn, material_id: int) -> dict[str, int]:
    """Перед удалением фрагментов материала: его редакции документов и письма. Если удалённая редакция была
    текущей — текущей снова становится предыдущая (её фрагменты возвращаются в поиск); документ без редакций
    и ветка без писем удаляются."""
    from psycopg.rows import dict_row

    out = {"versions": 0, "documents": 0, "letters": 0, "threads": 0}
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("DELETE FROM document_versions WHERE material_id = %s RETURNING document_id", (material_id,))
        docs = {r["document_id"] for r in cur.fetchall()}
        out["versions"] = cur.rowcount
        for doc in docs:
            cur.execute("SELECT id, status FROM document_versions WHERE document_id = %s ORDER BY id DESC", (doc,))
            rest = cur.fetchall()
            if not rest:
                cur.execute("DELETE FROM documents WHERE id = %s", (doc,))
                out["documents"] += 1
                continue
            if not any(r["status"] == "current" for r in rest):
                last = rest[0]["id"]
                cur.execute("UPDATE document_versions SET status = 'current' WHERE id = %s", (last,))
                cur.execute("UPDATE chunks SET status = 'active' WHERE attrs->>'version_id' = %s", (str(last),))
            cur.execute("""UPDATE documents SET current_version_id = (SELECT id FROM document_versions
                           WHERE document_id = %s AND status = 'current' ORDER BY id DESC LIMIT 1), updated_at = now()
                           WHERE id = %s""", (doc, doc))
        cur.execute("DELETE FROM letters WHERE material_id = %s RETURNING thread_id", (material_id,))
        threads = {r["thread_id"] for r in cur.fetchall()}
        out["letters"] = cur.rowcount
        if threads:  # ветка без писем — вместе со сводкой в базе поиска
            cur.execute("""DELETE FROM chunks WHERE chunk_id IN (SELECT summary_chunk FROM threads t WHERE t.id = ANY(%s)
                           AND NOT EXISTS (SELECT 1 FROM letters l WHERE l.thread_id = t.id))""", (list(threads),))
            cur.execute("""DELETE FROM threads WHERE id = ANY(%s) AND NOT EXISTS
                           (SELECT 1 FROM letters l WHERE l.thread_id = threads.id) RETURNING id""", (list(threads),))
            out["threads"] = len(cur.fetchall())
    return out
