"""Поиск для агента и оценки: семантический поиск + точные ссылки по номерам.

Векторный поиск плохо различает числа: «тест-кейс 34» и «тест-кейс 43» для эмбеддинга почти одно и
то же. Поэтому номера из вопроса («тест-кейс № 34», «пункт 64 плана») дополнительно ищутся точным
фильтром по атрибутам чанка (test_case, plan_item), и такие попадания ставятся первыми.
"""

from __future__ import annotations

import re
from collections.abc import Callable

SearchFn = Callable[[str, dict[str, str], int], list[dict]]

_REFS = [
    (re.compile(r"тест[-\s]?кейс\w*\s*(?:№\s*)?(\d{1,4})", re.IGNORECASE), "test_case"),
    (re.compile(r"(?:пункт\w*|п\.)\s*(?:№\s*)?(\d{1,4})\s*(?:плана|пл\.)", re.IGNORECASE), "plan_item"),
    (re.compile(r"(?:пункт\w*|п\.)\s*(?:плана\s*(?:тестирования)?\s*)?(?:№\s*)?(\d{1,4})\b", re.IGNORECASE),
     "plan_item"),
]


def number_refs(question: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for pattern, attr in _REFS:
        for m in pattern.finditer(question):
            ref = (attr, m.group(1))
            if ref not in found:
                found.append(ref)
    return found


def smart_search(search: SearchFn, query: str, filters: dict[str, str], k: int) -> list[dict]:
    exact: list[dict] = []
    for attr, value in number_refs(query):
        exact += search(query, {**filters, attr: value}, k)
    semantic = search(query, filters, k)
    out, seen = [], set()
    for h in exact + semantic:
        key = h.get("file_id") or h.get("text")
        if key not in seen:
            seen.add(key)
            out.append(h)
    return out[:k]


def source_label(attrs: dict, text: str = "") -> str:
    """Человекочитаемая ссылка на источник — вместо внутренних id файлов."""
    a = attrs or {}
    title = a.get("title") or (text.split("\n", 1)[0][:120] if text else "")
    if a.get("doc_type") == "email":
        subject = title.removeprefix("Тема: ")
        parts = [f"письмо «{subject}»", a.get("date", ""), a.get("author", "")]
        return ", ".join(p for p in parts if p)
    doc = a.get("doc_title") or title
    label = doc + (f" (ред. {a['doc_version']})" if a.get("doc_version") else "")
    for key, prefix in (("test_case", "тест-кейс № "), ("plan_item", "пункт плана № "), ("section", "раздел ")):
        if a.get(key):
            label += f", {prefix}{a[key]}"
            break
    if a.get("comment_author"):
        label += f", комментарий {a['comment_author']}"
    if a.get("received"):
        label += f" [{a['received']}]"
    return label


def format_hits(hits: list[dict], max_chars: int = 1500) -> list[dict]:
    return [{"источник": source_label(h.get("attributes") or {}, h.get("text", "")),
             "текст": h.get("text", "")[:max_chars]} for h in hits]
