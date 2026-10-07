"""Решённое обращение → разбор для базы знаний («симптом → причина → решение»).

Зачем: опыт решённых проблем должен находиться в чате. Разбор сохраняется как материал
«Решение ОБР-0001 — <тема>.md» и проходит обычный путь «Материалов» (разбор → индексация → запись в
базу) — отдельного индекса и отдельной логики поиска не нужно; в ответе агента источник виден по имени.

Как готовится разбор:
- нужно заполненное поле «Решение» (без него разбирать нечего);
- модель (COPILOT_MODEL_BUSINESS_TEXT, JSON-схема) пишет тему, симптом, причину, решение и ключевые
  слова строго по полям обращения; без ключей AI Studio или при ошибке — шаблон из тех же полей;
- персональные данные убираются: имя инициатора и других контактов, e-mail и телефоны — ни в подсказку
  модели, ни в итоговый текст они не попадают (дополнительно маскируются после модели);
- аналитик правит текст в карточке и сам отправляет его в базу знаний: черновик ничего не сохраняет.

Отправка: файл кладётся в реестр материалов (data/uploads/<дата>/), у обращения заполняется
kb_material_id, в историю пишется событие. Повторная отправка исправленного текста создаёт новый
материал; прежний остаётся в индексе (удаления материалов пока нет).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from copilot1c.ingest.cleaning import mask_pii
from copilot1c.issues import CATEGORIES, IssueError, number

log = logging.getLogger("copilot1c.kb")

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Тема разбора: суть проблемы, до 100 знаков, без номера обращения"},
        "symptom": {"type": "string", "description": "Что видели пользователи: действие, ошибка, где"},
        "cause": {"type": "string", "description": "Почему это происходило; пусто, если причина не указана"},
        "solution": {"type": "string", "description": "Что сделали и как проверить; шаги, если они есть"},
        "keywords": {"type": "array", "items": {"type": "string"},
                     "description": "3–8 слов и объектов 1С, по которым разбор будут искать"},
    },
    "required": ["title", "symptom", "solution"],
}
SYSTEM = (
    "Ты готовишь короткий разбор решённого обращения для базы знаний ИТ-отдела по 1С (Управление торговлей 11). "
    "Пиши по-русски, деловым языком, кратко. Используй только факты из полей обращения, ничего не придумывай: "
    "если причина не указана — оставь её пустой. Не упоминай людей: никаких имён, должностей, e-mail и "
    "телефонов. Объекты 1С, тексты ошибок и номера версий сохраняй точно."
)


def _scrub(text: str, names: list[str], analysts: tuple[str, ...] = ()) -> str:
    """Без персональных данных: имена (полностью и по частям от 3 букв) → «пользователь» / «аналитик»,
    e-mail и телефоны маскируются. Текст остаётся читаемым: «Пользователь пишет: …»."""
    out = text or ""
    for name in names:
        role = "аналитик" if name in analysts else "пользователь"
        for part in sorted({name, *name.split()}, key=len, reverse=True):
            part = part.strip(" .,")
            if len(part) >= 3:
                out = re.sub(rf"(?<![\wЁё]){re.escape(part)}(?![\wЁё])", role, out, flags=re.IGNORECASE)
    out = re.sub(r"\b(пользователь|аналитик)(?:\s+\1)+\b", r"\1", out)  # «Мария Смирнова» → одно слово
    out = re.sub(r"(^|[.!?]\s+|\n)(пользователь|аналитик)\b", lambda m: m.group(1) + m.group(2).capitalize(), out)
    return mask_pii(out)


def _facts(issue: dict[str, Any], names: list[str], analysts: tuple[str, ...] = ()) -> dict[str, Any]:
    """Поля обращения для разбора — уже без персональных данных."""
    def clean(v):
        return _scrub(str(v), names, analysts).strip() if v else ""

    return {
        "title": clean(issue.get("title")),
        "category": CATEGORIES.get(issue.get("category"), issue.get("category") or ""),
        "description": clean(issue.get("description"))[:3000],
        "error_text": clean(issue.get("error_text"))[:1500],
        "steps": clean(issue.get("steps"))[:1500],
        "expected": clean(issue.get("expected")),
        "actual": clean(issue.get("actual")),
        "objects": issue.get("objects") or [],
        "config_version": issue.get("config_version") or "",
        "platform_version": issue.get("platform_version") or "",
        "root_cause": clean(issue.get("root_cause")),
        "resolution": clean(issue.get("resolution")),
        "test_case_ids": issue.get("test_case_ids") or [],
        "requirement_ids": issue.get("requirement_ids") or [],
    }


def _names(issue: dict[str, Any], extra: tuple[str, ...] = ()) -> list[str]:
    """Кого убрать из текста: инициатор, аналитики (обращения к ним — «Дмитрий, …» — тоже)."""
    ini = issue.get("initiator") or {}
    return [n for n in (ini.get("name"), *extra) if n]


def _template(f: dict[str, Any]) -> dict[str, Any]:
    from copilot1c.intent import _GREETING

    # без приветствия и обращения к аналитику в первой строке («Добрый день!», «Дмитрий,»)
    desc = _GREETING.sub("", f["description"], count=1)
    desc = re.sub(r"^\s*[А-ЯЁA-Z][а-яёa-z]+,\s*\n", "", desc).strip()
    symptom = desc[:800] or f["error_text"]  # текст ошибки render добавит отдельной строкой
    if f["actual"]:
        symptom = f"{symptom}\nПолучено: {f['actual']}".strip()
    return {"title": f["title"], "symptom": symptom, "cause": f["root_cause"], "solution": f["resolution"],
            "keywords": list(f["objects"])}


def render(issue: dict[str, Any], f: dict[str, Any], parts: dict[str, Any]) -> str:
    """Markdown разбора. Абзацы через пустую строку — так текст делится на фрагменты при индексации."""
    meta = [f"Категория: {f['category']}" if f["category"] else "",
            "Объекты: " + ", ".join(f["objects"]) if f["objects"] else "",
            f"Конфигурация: {f['config_version']}" if f["config_version"] else "",
            f"Платформа: {f['platform_version']}" if f["platform_version"] else "",
            f"Решено: {str(issue.get('resolved_at'))[:10]}" if issue.get("resolved_at") else ""]
    lines = [f"# Решённое обращение {number(issue['id'])}: {parts['title'] or f['title']}",
             " · ".join(m for m in meta if m),
             "## Симптом", parts["symptom"] or "—"]
    if f["error_text"] and f["error_text"] not in (parts["symptom"] or ""):
        lines += ["Текст ошибки:", f["error_text"]]
    lines += ["## Причина", parts.get("cause") or "Причина не указана.", "## Решение", parts["solution"] or "—"]
    links = []
    if f["test_case_ids"]:
        links.append("тест-кейсы ПиМИ: " + ", ".join(f["test_case_ids"]))
    if f["requirement_ids"]:
        links.append("пункты плана тестирования ТЗ: " + ", ".join(f["requirement_ids"]))
    if links:
        joined = "; ".join(links)
        lines += ["## Связи", joined[0].upper() + joined[1:] + "."]  # capitalize() испортил бы «ПиМИ» и «ТЗ»
    kw = [k for k in parts.get("keywords") or [] if k]
    if kw:
        lines.append("Ключевые слова: " + ", ".join(dict.fromkeys(kw)))
    return "\n\n".join(x for x in lines if x)


def compose(issue: dict[str, Any], settings=None, use_llm: bool = True) -> dict[str, Any]:
    """Черновик разбора: {title, text, method, warnings}. Ничего не сохраняет."""
    if not (issue.get("resolution") or "").strip():
        raise IssueError("Заполните поле «Решение» в блоке «Итог» и сохраните обращение — без него разбирать нечего")
    analysts = tuple(getattr(settings, "analysts", ()) or ())
    names = _names(issue, analysts)
    f = _facts(issue, names, analysts)
    warnings = []
    if not f["root_cause"]:
        warnings.append("не заполнена «Причина» — разбор будет без неё")
    parts, method = _template(f), "template"
    if use_llm and settings is not None and settings.yc_api_key and settings.yc_folder_id:
        from copilot1c.index.yandex import chat_json

        prompt = "Поля обращения (JSON):\n" + str({k: v for k, v in f.items() if v})
        try:
            res = chat_json(prompt, SCHEMA, model=settings.model_business_text, system=SYSTEM, settings=settings)
            if (res.get("symptom") or "").strip() and (res.get("solution") or "").strip():
                parts = {k: _scrub(v, names, analysts) if isinstance(v, str) else v for k, v in res.items()}
                parts["keywords"] = list(dict.fromkeys([*f["objects"], *(res.get("keywords") or [])]))
                method = "llm"
        except Exception as exc:  # noqa: BLE001 — модель недоступна: шаблон из полей
            log.warning("разбор обращения моделью не удался: %s: %s", type(exc).__name__, exc)
            warnings.append(f"модель недоступна ({type(exc).__name__}) — разбор собран из полей")
    title = (parts.get("title") or f["title"]).strip()[:150]
    return {"title": title, "text": render(issue, f, {**parts, "title": title}), "method": method,
            "warnings": warnings}


def publish(conn, project: str, issue: dict[str, Any], title: str, text: str, materials_dir: Path,
            base: Path | None = None, analysts: tuple[str, ...] = (),
            contours: list[int] | None = None) -> tuple[dict[str, Any], bool]:
    """Разбор → материал в реестре (дальше его обрабатывает фоновый поток демона). (материал, уже_был).
    contours — система и подсистема обращения: разбор находится в поиске по тем же контурам."""
    from copilot1c.materials import MaterialRegistry, register_upload

    text = _scrub(text or "", _names(issue, analysts), analysts).strip()
    if len(text) < 40:
        raise IssueError("Текст разбора слишком короткий")
    title = re.sub(r"\s+", " ", title or issue["title"]).strip()[:120]
    filename = f"Решение {number(issue['id'])} — {title}.md"
    return register_upload(MaterialRegistry(conn, project), materials_dir, filename, text.encode("utf-8"), base=base,
                           decision={"action": "add", "contours": list(contours or []), "kind": "protocol"})
