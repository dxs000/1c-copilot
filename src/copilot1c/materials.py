"""Материалы, загруженные через веб: сохранение файла и реестр его судьбы в PostgreSQL.

Файл кладётся в <materials_dir>/<ГГГГ-ММ-ДД>/<имя> (по умолчанию data/uploads — внутри data, рядом с
остальными материалами проекта), запись — в таблицу materials. Один и тот же файл (по SHA-256) второй
раз не сохраняется: возвращается уже существующая запись.

Статусы по жизненному циклу: queued → parsing → indexing → graph → done; duplicate — всё содержимое
уже было в базе; error — с причиной в detail; deleted — фрагменты убраны из базы, файл и запись остаются.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

STATUS_LABELS = {
    "queued": "в очереди",
    "parsing": "разбор",
    "indexing": "индексация",
    "graph": "запись в базу",
    "done": "готово",
    "duplicate": "уже есть",
    "error": "ошибка",
    "deleted": "удалён",
}
ACTIVE = ("queued", "parsing", "indexing", "graph")
MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # как предел разбора одного файла в ingest.attachments
_BAD_CHARS = re.compile(r'[\x00-\x1f/\\:*?"<>|]+')


def safe_filename(name: str) -> str:
    """Имя файла без пути и опасных символов; расширение сохраняется."""
    name = _BAD_CHARS.sub("_", Path(name.replace("\\", "/")).name).strip(" .")
    if not name:
        return "файл"
    stem, suffix = Path(name).stem, Path(name).suffix
    return stem[: 150 - len(suffix)] + suffix


def save_upload(root: Path, filename: str, data: bytes, today: date | None = None) -> Path:
    """Сохраняет файл в <root>/<дата>/<имя>; при совпадении имени добавляет (2), (3)…"""
    folder = root / (today or date.today()).isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    name = safe_filename(filename)
    path = folder / name
    n = 2
    while path.exists():
        path = folder / f"{Path(name).stem} ({n}){Path(name).suffix}"
        n += 1
    tmp = path.with_name(path.name + ".part")
    tmp.write_bytes(data)
    tmp.replace(path)  # файл появляется целиком — разбор не увидит недописанный
    return path


def row_out(r: dict[str, Any]) -> dict[str, Any]:
    """Запись реестра для API: даты строками, подпись статуса по-русски."""
    out = {k: (v.isoformat(timespec="seconds") if hasattr(v, "isoformat") else v) for k, v in r.items()}
    out["status_label"] = STATUS_LABELS.get(r["status"], r["status"])
    out.pop("sha256", None)
    return out


class MaterialRegistry:
    """Реестр материалов поверх открытого подключения psycopg (GraphStore.conn)."""

    def __init__(self, conn, project: str):
        self.conn, self.project = conn, project

    def find_sha(self, sha256: str) -> dict[str, Any] | None:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM materials WHERE project = %s AND sha256 = %s", (self.project, sha256))
            row = cur.fetchone()
        self.conn.commit()
        return row

    def add(self, filename: str, path: str, sha256: str, size: int) -> dict[str, Any]:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute("INSERT INTO materials (project, filename, path, sha256, size) VALUES (%s, %s, %s, %s, %s) "
                        "RETURNING *", (self.project, filename, path, sha256, size))
            row = cur.fetchone()
        self.conn.commit()
        return row

    def list(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM materials WHERE project = %s ORDER BY id DESC LIMIT %s", (self.project, limit))
            rows = cur.fetchall()
        self.conn.commit()
        return rows

    def get(self, material_id: int) -> dict[str, Any] | None:
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM materials WHERE project = %s AND id = %s", (self.project, material_id))
            row = cur.fetchone()
        self.conn.commit()
        return row

    def set_status(self, material_id: int, status: str, detail: str | None = None,
                   report: dict[str, Any] | None = None) -> None:
        stamps = {"parsing": ", started_at = now()", "done": ", finished_at = now()",
                  "duplicate": ", finished_at = now()", "error": ", finished_at = now()",
                  "deleted": ", finished_at = now()"}.get(status, "")
        with self.conn.cursor() as cur:
            cur.execute(f"UPDATE materials SET status = %s, detail = %s, report = coalesce(%s, report){stamps} "
                        "WHERE id = %s", (status, detail, Jsonb(report) if report is not None else None, material_id))
        self.conn.commit()


def register_upload(registry: MaterialRegistry, root: Path, filename: str, data: bytes,
                    base: Path | None = None) -> tuple[dict[str, Any], bool]:
    """Сохраняет файл и добавляет в реестр. Возвращает (запись, загружен_раньше)."""
    sha = hashlib.sha256(data).hexdigest()
    existing = registry.find_sha(sha)
    if existing is not None and existing["status"] == "deleted":  # удалённый из базы загружают снова — в очередь
        registry.set_status(existing["id"], "queued", "загружен повторно после удаления")
        return registry.get(existing["id"]), False
    if existing is not None:
        return existing, True
    path = save_upload(root, filename, data)
    rel = path.relative_to(base) if base and path.is_relative_to(base) else path
    return registry.add(safe_filename(filename), rel.as_posix(), sha, len(data)), False
