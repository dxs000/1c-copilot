"""Записи секретаря в PostgreSQL: места, сессии, сообщения таймера. Каждая операция — своя транзакция."""

from __future__ import annotations

import json
import threading
from datetime import date, datetime
from importlib import resources
from typing import Any

from psycopg.rows import dict_row

from copilot1c.secretary.places import Place

_SCHEMA_READY: set[str] = set()
_SCHEMA_LOCK = threading.Lock()


def ensure_schema(conn) -> None:
    """Таблицы секретаря — один раз на процесс и базу (CREATE IF NOT EXISTS; init-db не нужен)."""
    key = conn.info.dsn
    with _SCHEMA_LOCK:
        if key in _SCHEMA_READY:
            return
        sql = resources.files("copilot1c.secretary").joinpath("schema.sql").read_text(encoding="utf-8")
        conn.execute(sql)
        conn.commit()
        _SCHEMA_READY.add(key)


class SecretaryStore:
    def __init__(self, conn):
        self.conn = conn
        ensure_schema(conn)

    def _rows(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        try:
            with self.conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                rows = cur.fetchall() if cur.description else []
            self.conn.commit()
            return rows
        except Exception:
            self.conn.rollback()
            raise

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    # ---------- места ----------

    def add_place(self, person: str, place: Place, effective_at: datetime, direction: str, said: str,
                  on_date: date | None = None) -> dict:
        return self._one("""INSERT INTO sec_places (person, city, country, tz, effective_at, direction, said, on_date)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                         (person, place.city, place.country, place.tz, effective_at, direction, said, on_date))

    def place_at(self, person: str, at: datetime) -> dict | None:
        return self._one("""SELECT * FROM sec_places WHERE person = %s AND NOT cancelled AND effective_at <= %s
                            ORDER BY effective_at DESC, id DESC LIMIT 1""", (person, at))

    def upcoming(self, person: str, now: datetime) -> list[dict]:
        return self._rows("""SELECT * FROM sec_places WHERE person = %s AND NOT cancelled AND effective_at > %s
                             ORDER BY effective_at, id""", (person, now))

    def cancel_upcoming(self, person: str, now: datetime) -> list[dict]:
        return self._rows("""UPDATE sec_places SET cancelled = true
                             WHERE person = %s AND NOT cancelled AND effective_at > %s RETURNING *""", (person, now))

    def history(self, person: str, limit: int = 20) -> list[dict]:
        return self._rows("""SELECT * FROM sec_places WHERE person = %s AND NOT cancelled
                             ORDER BY effective_at DESC, id DESC LIMIT %s""", (person, limit))

    # кэш городов, найденных моделью (интерфейс для places.resolve)
    def get(self, form: str) -> Place | None:
        r = self._one("SELECT * FROM sec_place_names WHERE form = %s", (form,))
        return Place(r["city"], r["country"], r["tz"], "кэш") if r else None

    def put(self, form: str, place: Place) -> None:
        self._rows("""INSERT INTO sec_place_names (form, city, country, tz) VALUES (%s,%s,%s,%s)
                      ON CONFLICT (form) DO UPDATE SET city = EXCLUDED.city, country = EXCLUDED.country,
                                                     tz = EXCLUDED.tz""",
                   (form, place.city, place.country, place.tz))

    # ---------- сессии ----------

    def running(self, person: str) -> dict | None:
        return self._one("""SELECT * FROM sec_sessions WHERE person = %s AND status = 'running'
                            ORDER BY started_at DESC LIMIT 1""", (person,))

    def start(self, person: str, kind: str, minutes: int, started_at: datetime, ends_at: datetime,
              said: str) -> tuple[dict, dict | None]:
        """Новая сессия; идущая (если была) — replaced. (новая, заменённая)."""
        replaced = self._one("""UPDATE sec_sessions SET status = 'replaced', finished_at = %s
                                WHERE person = %s AND status = 'running' RETURNING *""", (started_at, person))
        new = self._one("""INSERT INTO sec_sessions (person, kind, minutes, started_at, ends_at, said)
                           VALUES (%s,%s,%s,%s,%s,%s) RETURNING *""",
                        (person, kind, minutes, started_at, ends_at, said))
        return new, replaced

    def stop(self, person: str, at: datetime) -> dict | None:
        return self._one("""UPDATE sec_sessions SET status = 'stopped', finished_at = %s
                            WHERE person = %s AND status = 'running' RETURNING *""", (at, person))

    def recover(self) -> int:
        """После перезапуска: недоделанное завершение — снова к таймеру."""
        return len(self._rows("UPDATE sec_sessions SET status = 'running' WHERE status = 'finishing' RETURNING id"))

    def claim_due(self, now: datetime, limit: int = 20) -> list[dict]:
        return sorted(self._rows("""UPDATE sec_sessions SET status = 'finishing'
                                    WHERE id IN (SELECT id FROM sec_sessions WHERE status = 'running' AND ends_at <= %s
                                                 ORDER BY ends_at LIMIT %s FOR UPDATE SKIP LOCKED)
                                    RETURNING *""", (now, limit)), key=lambda r: r["ends_at"])

    def finish(self, session: dict, text: str, data: dict, at: datetime) -> dict:
        """Сессия done и сообщение — одной транзакцией (только если её не остановили, пока искали погоду)."""
        try:
            with self.conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""UPDATE sec_sessions SET status = 'done', finished_at = %s
                               WHERE id = %s AND status = 'finishing' RETURNING id""", (at, session["id"]))
                if cur.fetchone() is None:
                    self.conn.rollback()
                    return {}
                cur.execute("""INSERT INTO sec_notices (person, session_id, kind, text, data)
                               VALUES (%s,%s,'session_end',%s,%s) RETURNING *""",
                            (session["person"], session["id"], text, json.dumps(data, ensure_ascii=False, default=str)))
                row = cur.fetchone()
            self.conn.commit()
            return row
        except Exception:
            self.conn.rollback()
            raise

    def last_finished(self, person: str) -> dict | None:
        return self._one("""SELECT * FROM sec_sessions WHERE person = %s AND status IN ('done', 'stopped')
                            ORDER BY finished_at DESC NULLS LAST LIMIT 1""", (person,))

    def today_totals(self, person: str, since: datetime) -> dict[str, int]:
        """Минуты работы и отдыха с начала местных суток: по факту (досрочно остановленные — до остановки)."""
        rows = self._rows("""SELECT kind, sum(extract(epoch FROM (coalesce(finished_at, now()) - started_at)))::int
                                    AS seconds
                             FROM sec_sessions WHERE person = %s AND started_at >= %s GROUP BY kind""",
                          (person, since))
        return {r["kind"]: max(0, r["seconds"] or 0) // 60 for r in rows}

    # ---------- книги ----------

    def add_book(self, person: str, author: str, title: str, total_pages: int | None, said: str,
                 at: datetime) -> dict:
        """Новая книга со следующим номером человека (номера удалённых не переиспользуются)."""
        try:
            with self.conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT pg_advisory_xact_lock(hashtext('sec_books:' || %s))", (person,))
                cur.execute("""INSERT INTO sec_books (person, num, author, title, total_pages, said, created_at)
                               VALUES (%s, (SELECT coalesce(max(num), 0) + 1 FROM sec_books WHERE person = %s),
                                       %s, %s, %s, %s, %s) RETURNING *""",
                            (person, person, author, title, total_pages, said, at))
                row = cur.fetchone()
            self.conn.commit()
            return row
        except Exception:
            self.conn.rollback()
            raise

    def books(self, person: str, include_done: bool = True) -> list[dict]:
        statuses = ("reading", "paused", "done") if include_done else ("reading", "paused")
        return self._rows("""SELECT * FROM sec_books WHERE person = %s AND status = ANY(%s) ORDER BY num""",
                          (person, list(statuses)))

    def book(self, person: str, num: int) -> dict | None:
        return self._one("SELECT * FROM sec_books WHERE person = %s AND num = %s AND status <> 'deleted'",
                         (person, num))

    def book_by_id(self, person: str, book_id: int) -> dict | None:
        return self._one("SELECT * FROM sec_books WHERE person = %s AND id = %s AND status <> 'deleted'",
                         (person, book_id))

    def update_book(self, book_id: int, **fields) -> dict:
        cols = ", ".join(f"{k} = %s" for k in fields)
        return self._one(f"UPDATE sec_books SET {cols} WHERE id = %s RETURNING *", (*fields.values(), book_id))

    def add_reading(self, book: dict, page: int, at: datetime, city: str | None, tz: str | None, said: str) -> dict:
        return self._one("""INSERT INTO sec_reading (book_id, person, page, at, city, tz, said)
                            VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                         (book["id"], book["person"], page, at, city, tz, said))

    def reading_log(self, person: str, book_ids: list[int] | None = None) -> list[dict]:
        if book_ids is not None:
            return self._rows("SELECT * FROM sec_reading WHERE person = %s AND book_id = ANY(%s) ORDER BY at, id",
                              (person, book_ids))
        return self._rows("SELECT * FROM sec_reading WHERE person = %s ORDER BY at, id", (person,))

    def delete_reading(self, book_id: int, page: int | None) -> dict | None:
        """Последняя отметка книги (или последняя с этой страницей) — удалить; удалённая запись или None."""
        cond = "AND page = %s" if page is not None else ""
        params = (book_id, page) if page is not None else (book_id,)
        return self._one(f"""DELETE FROM sec_reading WHERE id = (SELECT id FROM sec_reading WHERE book_id = %s {cond}
                                                             ORDER BY at DESC, id DESC LIMIT 1) RETURNING *""", params)

    def upsert_toc(self, book_id: int, entries: list[tuple[str, int]]) -> tuple[int, int]:
        """(добавлено, обновлено) — раздел с тем же названием получает новую страницу."""
        added = updated = 0
        try:
            with self.conn.cursor(row_factory=dict_row) as cur:
                for title, page in entries:
                    cur.execute("""INSERT INTO sec_book_toc (book_id, title, page) VALUES (%s,%s,%s)
                                   ON CONFLICT (book_id, title) DO UPDATE SET page = EXCLUDED.page
                                   RETURNING (xmax = 0) AS inserted""", (book_id, title, page))
                    if cur.fetchone()["inserted"]:
                        added += 1
                    else:
                        updated += 1
            self.conn.commit()
            return added, updated
        except Exception:
            self.conn.rollback()
            raise

    def toc(self, book_ids: list[int]) -> list[dict]:
        return self._rows("SELECT * FROM sec_book_toc WHERE book_id = ANY(%s) ORDER BY book_id, page, id", (book_ids,))

    def delete_toc(self, book_id: int, title: str) -> int:
        return len(self._rows("DELETE FROM sec_book_toc WHERE book_id = %s AND lower(title) = lower(%s) RETURNING id",
                              (book_id, title)))

    # ---------- сообщения ----------

    def notices(self, person: str, after_id: int = 0, unread: bool = False, limit: int = 50) -> list[dict]:
        return self._rows(f"""SELECT * FROM sec_notices WHERE person = %s AND id > %s
                              {"AND read_at IS NULL" if unread else ""} ORDER BY id LIMIT %s""",
                          (person, after_id, limit))

    def mark_read(self, person: str, notice_id: int) -> bool:
        return bool(self._rows("""UPDATE sec_notices SET read_at = coalesce(read_at, now())
                                  WHERE person = %s AND id = %s RETURNING id""", (person, notice_id)))
