"""Справочник контуров: к чему относится материал.

Контур — система (УТ 11, БП 3.0…), процесс или тема (командировки, НСИ…) или проект (обновление УТ 11 до
11.5.27.75). У процесса и проекта может быть родитель — система. Каждый фрагмент базы помечен контурами
(chunks.contours), поиск можно ограничить ими. Справочник ведут аналитики; разбор входящих предлагает контур
из этого списка или новый пункт, который аналитик утверждает.

aliases — как контур называют в письмах и документах («УТ11», «Управление торговлей», «Trade»), по ним
разбор узнаёт контур без модели; notes — предметные пояснения для промпта агента (префиксы доработок,
версии, особенности).
"""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row

KINDS = {"system": "система", "process": "процесс", "project": "проект"}
_FIELDS = ("name", "parent_id", "aliases", "notes", "active")


class ContourError(Exception):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _clean_aliases(values) -> list[str]:
    out: list[str] = []
    for v in values or []:
        v = str(v).strip()
        if v and v.casefold() not in {x.casefold() for x in out}:
            out.append(v[:200])
    return out


def _out(r: dict[str, Any]) -> dict[str, Any]:
    out = dict(r)
    out["kind_label"] = KINDS.get(r["kind"], r["kind"])
    if out.get("created_at") is not None:
        out["created_at"] = out["created_at"].isoformat(timespec="seconds")
    return out


class ContourRegistry:
    KINDS = KINDS

    def __init__(self, conn, project: str):
        self.conn, self.project = conn, project

    def _rows(self, sql: str, params: tuple) -> list[dict[str, Any]]:
        try:
            with self.conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                rows = cur.fetchall() if cur.description else []
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return rows

    def list(self, include_inactive: bool = False) -> list[dict[str, Any]]:
        rows = self._rows("SELECT * FROM contours WHERE project = %s AND (active OR %s) ORDER BY kind, name",
                          (self.project, include_inactive))
        return [_out(r) for r in rows]

    def get(self, contour_id: int) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM contours WHERE project = %s AND id = %s", (self.project, contour_id))
        return _out(rows[0]) if rows else None

    def _check_parent(self, parent_id: int | None, own_id: int | None = None) -> None:
        if parent_id is None:
            return
        if parent_id == own_id:
            raise ContourError("контур не может быть родителем самого себя")
        if self.get(parent_id) is None:
            raise ContourError(f"родительский контур {parent_id} не найден")

    def create(self, data: dict[str, Any]) -> dict[str, Any]:
        import psycopg

        kind = data.get("kind")
        if kind not in KINDS:
            raise ContourError(f"вид контура — один из: {', '.join(KINDS)}")
        name = (data.get("name") or "").strip()
        if not name:
            raise ContourError("пустое название контура")
        self._check_parent(data.get("parent_id"))
        try:
            rows = self._rows("""INSERT INTO contours (project, kind, name, parent_id, aliases, notes)
                                 VALUES (%s, %s, %s, %s, %s, %s) RETURNING *""",
                              (self.project, kind, name[:200], data.get("parent_id"),
                               _clean_aliases(data.get("aliases")), data.get("notes")))
        except psycopg.errors.UniqueViolation as exc:
            raise ContourError(f"{KINDS[kind]} «{name}» уже есть в справочнике", 409) from exc
        return _out(rows[0])

    def update(self, contour_id: int, data: dict[str, Any]) -> dict[str, Any]:
        import psycopg

        if self.get(contour_id) is None:
            raise ContourError("контур не найден", 404)
        changes = {k: v for k, v in data.items() if k in _FIELDS}
        if "name" in changes:
            changes["name"] = (changes["name"] or "").strip()[:200]
            if not changes["name"]:
                raise ContourError("пустое название контура")
        if "aliases" in changes:
            changes["aliases"] = _clean_aliases(changes["aliases"])
        if "parent_id" in changes:
            self._check_parent(changes["parent_id"], contour_id)
        if not changes:
            return self.get(contour_id)
        sets = ", ".join(f"{k} = %s" for k in changes)
        try:
            rows = self._rows(f"UPDATE contours SET {sets} WHERE project = %s AND id = %s RETURNING *",
                              (*changes.values(), self.project, contour_id))
        except psycopg.errors.UniqueViolation as exc:
            raise ContourError("контур с таким названием уже есть", 409) from exc
        return _out(rows[0])

    def match(self, text: str) -> list[dict[str, Any]]:
        """Контуры, названные в тексте по имени или псевдониму (целым словом, без учёта регистра)."""
        import re

        low = text.casefold()
        found = []
        for c in self.list():
            for name in [c["name"], *c["aliases"]]:
                if name and re.search(rf"(?<![\wЁё]){re.escape(name.casefold())}(?![\wЁё])", low):
                    found.append(c)
                    break
        return found
