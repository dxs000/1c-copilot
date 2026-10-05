"""Фоновая обработка материалов, загруженных через веб: разбор → индексация → запись в базу.

Обработчик — один поток внутри демона (AI Studio ограничивает частоту запросов, параллелить
нечего). Он забирает из реестра materials всё, что в очереди, и обрабатывает пачкой:

1. Разбор. Каждый файл сначала разбирается отдельно — что в нём есть (письма, документы, картинки,
   пропуски с причинами). Затем собирается корпус проекта целиком: основные материалы data, потом
   уже обработанные загрузки и текущая пачка — строго в порядке загрузки. Так работает склейка дублей
   ядра (письмо файлом, вложением и цитатой; документ docx и PDF): «главным» остаётся то, что было в
   базе раньше, и уже проиндексированные фрагменты не меняют идентификаторы.
2. Индексация. Корпус уходит в Vector Store; манифест пропускает всё, что уже загружено, — в индекс
   попадают только новые фрагменты.
3. Запись в базу. Фрагменты, реестры тест-кейсов и плана, сущности — в PostgreSQL (вставки
   идемпотентны).

Итог по каждому файлу: «готово» (добавлено N фрагментов), «уже есть» (всё содержимое уже было в
базе — с указанием, где именно) или «ошибка» (с причиной). После перезапуска службы прерванная
обработка возвращается в очередь.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row

from copilot1c.config import Settings

log = logging.getLogger("copilot1c.worker")
BATCH_LIMIT = 20


def _rows(conn, sql: str, params: tuple) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall() if cur.description else []
    conn.commit()
    return rows


def recover(conn, project: str) -> int:
    """Прерванное перезапуском — обратно в очередь."""
    return len(_rows(conn, "UPDATE materials SET status = 'queued', detail = 'обработка прервана перезапуском — повтор' "
                           "WHERE project = %s AND status IN ('parsing', 'indexing', 'graph') RETURNING id", (project,)))


def claim(conn, project: str, limit: int = BATCH_LIMIT) -> list[dict[str, Any]]:
    return _rows(conn, """UPDATE materials SET status = 'parsing', detail = 'разбираю файл', started_at = now(),
                                               finished_at = NULL
                          WHERE id IN (SELECT id FROM materials WHERE project = %s AND status = 'queued'
                                       ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED)
                          RETURNING *""", (project, limit))


def _owned(source: str, path: str) -> bool:
    return source == path or source.startswith(path + "#")


def _file_summary(path: str, s: Settings) -> tuple[dict[str, Any], int]:
    """Что в файле само по себе: письма, документы, картинки, пропуски; число фрагментов."""
    from copilot1c.ingest.corpus import Corpus

    c = Corpus(project=s.project, settings=s).add_paths([path])
    docs = [{"title": d.title[:150], "type": d.doc_type.value, "test_cases": len(d.test_cases),
             "plan_items": len(d.plan_items)} for d in c.documents]
    report = {"emails": len(c.messages), "documents": docs, "images": len(c.images),
              "skipped": [{"file": x.filename, "reason": x.reason} for x in c.skipped]}
    return report, len(c.chunks())


def _where_already(corpus, path: str) -> list[str]:
    """Где в базе лежит то, что пришло в этом файле (для статуса «уже есть»)."""
    out = []
    for d in corpus.documents:
        if any(_owned(a, path) for a in d.aliases) and not _owned(d.source, path):
            out.append(f"документ «{d.title[:100]}» — {Path(d.source.split('#')[0]).name}")
    return out


def process_batch(batch: list[dict[str, Any]], conn, s: Settings, index=None) -> None:
    """Обрабатывает пачку записей реестра (уже в статусе parsing). index — VectorIndex (подменяется в тестах)."""
    from copilot1c.graph.store import GraphStore
    from copilot1c.index.yandex import VectorIndex
    from copilot1c.ingest.corpus import Corpus
    from copilot1c.ingest.entities import extract_regex_entities
    from copilot1c.materials import MaterialRegistry

    reg = MaterialRegistry(conn, s.project)
    summaries: dict[int, tuple[dict[str, Any], int]] = {}
    for m in batch:
        try:
            if not Path(m["path"]).exists():
                raise FileNotFoundError(f"файл не найден: {m['path']}")
            summaries[m["id"]] = _file_summary(m["path"], s)
        except Exception as exc:  # noqa: BLE001 — один битый файл не останавливает пачку
            reg.set_status(m["id"], "error", f"не удалось разобрать: {type(exc).__name__}: {exc}"[:500])
    batch = [m for m in batch if m["id"] in summaries]
    if not batch:
        return

    try:
        # Порядок важен для склейки дублей: основные материалы → прежние загрузки → текущая пачка
        earlier = [r["path"] for r in _rows(conn, "SELECT path FROM materials WHERE project = %s AND status IN "
                                                  "('done', 'duplicate') ORDER BY id", (s.project,))]
        corpus = Corpus(project=s.project, settings=s).add_paths([s.corpus_dir])
        for p in earlier + [m["path"] for m in batch]:
            if Path(p).exists():
                corpus.add_paths([p])
        chunks = corpus.chunks()

        index = index or VectorIndex(s.vector_store_id, s)
        before = set(index.load_manifest())
        for m in batch:  # подпись этапа — по самому файлу, а не по всей пачке
            own_new = sum(c.chunk_id not in before for c in chunks if _owned(c.source, m["path"]))
            reg.set_status(m["id"], "indexing", f"загружаю в индекс новые фрагменты: {own_new}" if own_new else
                           "новых фрагментов в файле нет — жду окончания обработки пачки")
        ids = index.add(chunks)

        for m in batch:
            reg.set_status(m["id"], "graph", "записываю фрагменты и реестры в PostgreSQL")
        store = GraphStore.__new__(GraphStore)  # то же подключение, без второго коннекта
        store.conn = conn
        fresh = [c for c in chunks if c.chunk_id not in before]
        store.upsert_chunks(chunks, ids)
        for d in corpus.documents:
            store.upsert_registries(s.project, d)
        for c in fresh:
            store.upsert_entities(extract_regex_entities(c.entity_text()), c.chunk_id)
        conn.commit()
    except Exception as exc:  # noqa: BLE001 — ошибка общего этапа: вся пачка в «ошибку» с причиной
        conn.rollback()
        log.exception("обработка материалов")
        for m in batch:
            reg.set_status(m["id"], "error", f"{type(exc).__name__}: {exc}"[:500], summaries[m["id"]][0])
        return

    for m in batch:
        summary, alone = summaries[m["id"]]
        mine = [c for c in chunks if _owned(c.source, m["path"])]
        new = sum(c.chunk_id not in before for c in mine)
        report = {**summary, "chunks": len(mine), "chunks_new": new, "already_in_base": _where_already(corpus, m["path"])}
        if alone == 0:
            reasons = "; ".join(f"{x['file']}: {x['reason']}" for x in summary["skipped"]) or "текст не найден"
            reg.set_status(m["id"], "error", f"нечего индексировать — {reasons}"[:500], report)
        elif not mine:
            where = report["already_in_base"]
            detail = "всё содержимое уже есть в базе" + (f": {'; '.join(where)}" if where else
                                                        " (те же письма и документы уже в базе проекта)")
            reg.set_status(m["id"], "duplicate", detail[:500], report)
        elif new == 0:
            reg.set_status(m["id"], "duplicate", "все фрагменты уже были в индексе", report)
        else:
            reg.set_status(m["id"], "done", f"добавлено фрагментов: {new} из {len(mine)}", report)


class MaterialWorker:
    """Поток, который раз в несколько секунд проверяет очередь и обрабатывает её пачками."""

    def __init__(self, settings: Settings, poll_seconds: float = 3.0):
        self.s, self.poll = settings, poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.busy = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="materials-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _loop(self) -> None:
        from copilot1c.graph.store import try_connect

        recovered = False
        while not self._stop.is_set():
            batch: list[dict[str, Any]] = []
            g = try_connect(self.s)
            if g is None:
                self._stop.wait(30)  # PostgreSQL недоступен — ждём и пробуем снова
                continue
            try:
                if not recovered:
                    n = recover(g.conn, self.s.project)
                    recovered = True
                    if n:
                        log.info("возвращено в очередь после перезапуска: %s", n)
                batch = claim(g.conn, self.s.project)
                if batch:
                    self.busy = True
                    log.info("обрабатываю материалы: %s", ", ".join(m["filename"] for m in batch))
                    process_batch(batch, g.conn, self.s)
            except Exception:  # noqa: BLE001 — поток не должен умирать
                log.exception("обработчик материалов")
            finally:
                self.busy = False
                g.close()
            if not batch:  # после пачки сразу проверяем, не пришло ли ещё
                self._stop.wait(self.poll)
