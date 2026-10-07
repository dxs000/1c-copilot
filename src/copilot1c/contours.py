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

KINDS = {"system": "система", "subsystem": "подсистема", "process": "процесс", "project": "проект"}
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
        if kind == "subsystem" and not data.get("parent_id"):
            raise ContourError("у подсистемы должен быть родитель — система (например, «УТ 11»)")
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
        """Контуры, названные в тексте по имени или псевдониму — с учётом окончаний: «Командировки» находит
        «командировок» и «командировочные»; «БП 3.0» и «УТ11» — как есть (короткие слова и числа — целиком)."""
        words = _tokens(text)
        found = []
        for c in self.list():
            if any(_contains(words, _tokens(name)) for name in [c["name"], *c["aliases"]] if name):
                found.append(c)
        return found


def _tokens(text: str) -> list[str]:
    import re

    return re.findall(r"[0-9a-zа-яё]+(?:[.:][0-9a-zа-яё]+)*", (text or "").casefold().replace("ё", "е"))


def _contains(words: list[str], name: list[str]) -> bool:
    if not name:
        return False
    for i in range(len(words) - len(name) + 1):
        if all(_word_match(words[i + j], w) for j, w in enumerate(name)):
            return True
    return False


def _word_match(word: str, original: str) -> bool:
    """Слово текста против слова названия с учётом окончаний: «склад» находит «складе», «реализация» —
    «реализации»; короткие слова и аббревиатуры (ОС, НДС, RDP) — только целиком, числа — с продолжением
    («11» находит «11.5.27.75»)."""
    if original.isdigit():
        return word == original or word.startswith(original + ".")
    if not original.isalpha() or len(original) <= 3:
        return word == original
    stem = original[: -(2 if len(original) > 6 else 1)]
    return word.startswith(stem) and len(word) <= len(original) + 4


# ---------- предложение контуров для текста (обращение, письмо) ----------

def path_label(c: dict[str, Any], by_id: dict[int, dict[str, Any]]) -> str:
    parent = by_id.get(c.get("parent_id") or 0)
    return f"{parent['name']} › {c['name']}" if parent else c["name"]


SUGGEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "contour_ids": {"type": "array", "items": {"type": "integer"},
                        "description": "id системы и подсистемы из справочника; пусто — ни одна не подходит"},
        "new_system": {"type": "string", "description": "Если подходящей системы нет — короткое название новой"},
        "new_subsystem": {"type": "string", "description": "Если система есть, а блока нет — название нового блока"},
        "new_parent_id": {"type": "integer", "description": "id системы для нового блока"},
        "reason": {"type": "string"},
    },
    "required": ["contour_ids"],
}
SUGGEST_SYSTEM = ("Ты относишь обращение пользователя к системе и её функциональному блоку по справочнику. Системы — "
                  "конфигурации 1С (УТ, БП, ЗУП) и не-1С области (инфраструктура: серверы, сеть, доступ, рабочие "
                  "места, почта). Проблема с подключением к серверу, сессиями, паролями, принтерами — инфраструктура, "
                  "даже если сервер называется по-1С-овски. Выбирай id только из справочника; если подходящего блока "
                  "нет — предложи новый с родителем-системой.")


def suggest(conn, project: str, text: str, objects: list[str] | tuple = (), settings=None,
            use_llm: bool = True) -> dict[str, Any]:
    """Система и подсистема для текста: объекты 1С из текста ошибки → подсистема (самый точный признак), слова
    и псевдонимы, модель — если по словам не определилось. Возвращает id (система первой), подписи, почему, и
    предложение нового пункта справочника, если модель его предложила."""
    from copilot1c.intent import md_objects

    reg = ContourRegistry(conn, project)
    items = reg.list()
    by_id = {c["id"]: c for c in items}
    names = {o.split(".")[-1].casefold() for o in [*objects, *md_objects(text or "")]}
    words = _tokens(text)
    score: dict[int, float] = {}
    why: dict[int, list[str]] = {}

    def add(cid: int, pts: float, reason: str) -> None:
        score[cid] = score.get(cid, 0) + pts
        why.setdefault(cid, [])
        if reason not in why[cid]:
            why[cid].append(reason)

    for c in items:
        aliases = [c["name"], *c["aliases"]]
        hit_obj = next((a for a in aliases if a.casefold() in names), None)
        if hit_obj:
            add(c["id"], 3, f"объект {hit_obj}")
        hits = [a for a in aliases if a and _contains(words, _tokens(a))]
        if hits:
            add(c["id"], min(1.0 + 0.5 * (len(hits) - 1), 2.5), "слова: " + ", ".join(hits[:3]))
    # подсистема, чья система тоже названа в тексте, — вероятнее («Продажи» есть и в УТ, и в БП)
    for cid in list(score):
        p = by_id[cid].get("parent_id")
        if p in score:
            add(cid, 1, f"есть признаки системы «{by_id[p]['name']}»")
    subs = sorted((cid for cid in score if by_id[cid]["kind"] == "subsystem"), key=lambda x: -score[x])
    chosen: list[int] = []
    if subs and (len(subs) == 1 or score[subs[0]] > score[subs[1]]):
        best = subs[0]
        chosen = [by_id[best]["parent_id"], best] if by_id[best].get("parent_id") else [best]
    elif not subs:
        systems = sorted((cid for cid in score if by_id[cid]["kind"] == "system"), key=lambda x: -score[x])
        if systems and (len(systems) == 1 or score[systems[0]] > score[systems[1]]):
            chosen = [systems[0]]
    method, new = "heuristic", None
    confident = len(chosen) == 2 or (chosen and not subs and not [c for c in items if c.get("parent_id") == chosen[0]])
    llm_ok = use_llm and settings is not None and getattr(settings, "intake_llm", True) and bool(
        settings.yc_api_key and settings.yc_folder_id)
    if not confident and llm_ok and items:
        from copilot1c.index.yandex import chat_json

        tree = "\n".join(f"{c['id']}: {path_label(c, by_id)}"
                         + (f" ({', '.join(c['aliases'][:6])})" if c["aliases"] else "")
                         for c in items if c["kind"] in ("system", "subsystem"))
        try:
            res = chat_json(f"Справочник:\n{tree}\n\nОбращение:\n{(text or '')[:4000]}", SUGGEST_SCHEMA,
                            model=settings.model_batch, system=SUGGEST_SYSTEM, settings=settings)
            ids = [i for i in res.get("contour_ids") or [] if i in by_id]
            if ids:
                leaf = next((i for i in ids if by_id[i]["kind"] == "subsystem"), ids[0])
                parent = by_id[leaf].get("parent_id")
                chosen = [parent, leaf] if parent and parent in by_id else [leaf]
                for i in chosen:
                    why.setdefault(i, []).append("модель: " + (res.get("reason") or "по смыслу")[:200])
            if res.get("new_subsystem") and res.get("new_parent_id") in by_id:
                new = {"kind": "subsystem", "name": res["new_subsystem"].strip()[:200], "parent_id": res["new_parent_id"],
                       "label": f"{by_id[res['new_parent_id']]['name']} › {res['new_subsystem'].strip()}"}
            elif res.get("new_system") and not ids:
                new = {"kind": "system", "name": res["new_system"].strip()[:200], "parent_id": None,
                       "label": res["new_system"].strip()}
            method = "llm"
        except Exception as exc:  # noqa: BLE001 — модель недоступна: остаются эвристики
            import logging

            logging.getLogger("copilot1c.contours").warning("контуры моделью не определены: %s", exc)
    candidates = sorted(score, key=lambda x: -score[x])[:6]
    return {"contours": chosen, "labels": [path_label(by_id[i], by_id) for i in chosen],
            "why": {str(i): why.get(i, []) for i in chosen}, "new": new, "method": method,
            "candidates": [{"id": i, "label": path_label(by_id[i], by_id), "score": round(score[i], 2)}
                           for i in candidates]}


def learn_objects(conn, project: str, contour_ids: list[int], objects: list[str]) -> int:
    """Аналитик отнёс обращение к подсистеме — объекты 1С из него становятся псевдонимами этой подсистемы:
    следующее обращение с тем же объектом попадёт туда само."""
    reg = ContourRegistry(conn, project)
    names = [o.split(".")[-1] for o in objects if o]
    added = 0
    for cid in contour_ids:
        c = reg.get(cid)
        if c is None or c["kind"] != "subsystem":
            continue
        merged = _clean_aliases([*c["aliases"], *names])
        if len(merged) > len(c["aliases"]):
            reg.update(cid, {"aliases": merged})
            added += len(merged) - len(c["aliases"])
    return added
