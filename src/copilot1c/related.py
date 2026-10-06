"""Похожие обращения и связанные тест-кейсы ПиМИ и пункты ТЗ — для карточки обращения и чата.

Без модели и без индекса: всё считается по PostgreSQL на лету (обращений сотни, тест-кейсов и пунктов
плана — сотни), быстро и предсказуемо, у каждой находки есть объяснение «почему».

Совпадение объектов 1С. Объекты в разных источниках записаны по-разному: в обращении
«Документ.РеализацияТоваровУслуг» и «КС_Гамма», в ПиМИ «Справочник «Номенклатура»», в плане ТЗ
«Номенклатура контрагентов». Сравнивается нормализованное имя: без вида объекта, кавычек, пробелов и
союза «и» (Реализация товаров и услуг = РеализацияТоваровУслуг).

Похожесть текста — общие основы слов (первые 6 букв слов от 4 букв, без служебных) — косинус множеств.

Оценки:
- обращение: текст × 1 + общие объекты 0,4 + то же место ошибки 1С 0,6 (это почти наверняка дубль);
- тест-кейс и пункт ТЗ: общие объекты 0,6 + текст × 0,8.
"""

from __future__ import annotations

import math
import re
from typing import Any

from psycopg.rows import dict_row

from copilot1c.intent import _ERROR_LOCATION_RE

_KINDS = r"(?:Справочник|Документ|РегистрСведений|РегистрНакопления|РегистрБухгалтерии|Обработка|Отчет|Отчёт|ОбщийМодуль|" \
         r"Перечисление|ПланВидовХарактеристик|БизнесПроцесс|Задача|ЖурналДокументов|регистр\w*\s+\w+|справочник\w*|" \
         r"документ\w*|обработк\w*|отч[её]т\w*)"
_KIND_RE = re.compile(rf"^\s*{_KINDS}[.\s]*", re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Za-zА-ЯЁа-яё]{4,}")
_STOP = {"после", "перед", "когда", "который", "которые", "также", "этого", "этот", "через", "будет", "можно", "нужно",
         "добрый", "день", "коллеги", "пожалуйста", "спасибо", "уважением", "ошибка", "ошибки", "просьба", "прошу",
         "проверить", "проверка", "работа", "работает", "данные", "данных", "форма", "формы", "система", "системе",
         "программа", "результат", "критерий", "методика"}
ISSUE_MIN, LINK_MIN, TOP = 0.3, 0.35, 5


def obj_key(name: str) -> str:
    """«Справочник «Номенклатура контрагентов»», «Справочник.НоменклатураКонтрагентов» → «номенклатураконтрагентов»."""
    s = _KIND_RE.sub("", name.strip())
    s = re.sub(r"\s+и\s+", " ", s, flags=re.IGNORECASE)
    return re.sub(r"[^a-zа-яё0-9_]", "", s.casefold())


def stems(text: str) -> set[str]:
    return {w.casefold()[:6] for w in _WORD_RE.findall(text or "") if w.casefold() not in _STOP}


def cosine(a: set[str], b: set[str]) -> float:
    return len(a & b) / math.sqrt(len(a) * len(b)) if a and b else 0.0


def _error_locations(text: str) -> set[str]:
    return {m.group(0) for m in _ERROR_LOCATION_RE.finditer(text or "")}


def _issue_text(d: dict[str, Any]) -> str:
    return " ".join(str(d.get(k) or "") for k in ("title", "description", "error_text"))


def _rows(conn, sql: str, params: tuple) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.commit()
    return rows


def find_related(conn, project: str, issue: dict[str, Any], exclude_id: int | None = None) -> dict[str, list]:
    """issue — карточка или черновик (title, description, error_text, objects). Возвращает три списка."""
    text = _issue_text(issue)
    words = stems(text)
    # объекты из поля и, дополнительно, из текста ошибки и описания (поле часто пустое у нового обращения)
    from copilot1c.intent import md_objects

    free_text = f"{issue.get('error_text') or ''}\n{issue.get('description') or ''}"
    named = list(issue.get("objects") or []) + md_objects(free_text)
    objs = {obj_key(o): o for o in named if obj_key(o)}
    errs = _error_locations(issue.get("error_text") or issue.get("description") or "")

    def shared(other_objects) -> list[str]:
        return [objs[k] for k in dict.fromkeys(obj_key(o) for o in other_objects or []) if k in objs]

    # --- похожие обращения ---
    similar = []
    for r in _rows(conn, "SELECT id, title, description, error_text, objects, status, created_at FROM issues "
                         "WHERE project = %s AND id IS DISTINCT FROM %s ORDER BY id DESC LIMIT 1000",
                   (project, exclude_id)):
        why, score = [], 0.0
        t = cosine(words, stems(_issue_text(r)))
        if t >= 0.15:
            score += t
            why.append(f"похожий текст ({round(t * 100)} %)")
        common = shared(r["objects"])
        if common:
            score += 0.4
            why.append("те же объекты: " + ", ".join(common[:3]))
        same_err = errs & _error_locations((r["error_text"] or "") + "\n" + (r["description"] or ""))
        if same_err:
            score += 0.6
            why.append("то же место ошибки: " + next(iter(same_err)))
        if score >= ISSUE_MIN:
            similar.append({"id": r["id"], "title": r["title"], "status": r["status"], "score": round(score, 2),
                            "why": why, "created_at": r["created_at"]})

    # --- тест-кейсы ПиМИ и пункты ТЗ ---
    def link(row: dict[str, Any], row_text: str) -> tuple[float, list[str]]:
        why, score = [], 0.0
        common = shared(row["objects"])
        if common:
            score += 0.6
            why.append("объекты: " + ", ".join(common[:3]))
        t = cosine(words, stems(row_text))
        if t >= 0.12:
            score += 0.8 * t
            why.append(f"похожий текст ({round(t * 100)} %)")
        return score, why

    tests = []
    for r in _rows(conn, "SELECT doc, num, section, function, steps, result, objects FROM test_cases WHERE project = %s",
                   (project,)):
        steps = " ".join(f"{s.get('method', '')} {s.get('criterion', '')}" for s in (r["steps"] or []))
        score, why = link(r, f"{r['function'] or ''} {r['section'] or ''} {steps}")
        if score >= LINK_MIN:
            tests.append({"doc": r["doc"], "num": r["num"], "function": r["function"],
                          "section": (r["section"] or "").split(" / ")[-1], "result": r["result"],
                          "score": round(score, 2), "why": why})

    reqs = []
    for r in _rows(conn, "SELECT doc, req_id, grp, object, text, status, objects FROM requirements WHERE project = %s",
                   (project,)):
        score, why = link(r, f"{r['object'] or ''} {r['text'] or ''}")
        if score >= LINK_MIN:
            reqs.append({"doc": r["doc"], "num": r["req_id"], "group": r["grp"], "object": r["object"],
                         "text": (r["text"] or "")[:300], "score": round(score, 2), "why": why})

    def top(items: list[dict]) -> list[dict]:
        return sorted(items, key=lambda x: -x["score"])[:TOP]

    return {"issues": top(similar), "test_cases": top(tests), "requirements": top(reqs)}
