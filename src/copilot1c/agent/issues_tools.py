"""Обращения для агента чата: по номеру, поиск по смыслу и «возможный дубль» до вызова модели.

Без этого агент видел только базу проекта (переписку, ТЗ, ПиМИ) и не знал о зарегистрированных проблемах:
не находил «ОБР-0012» и не узнавал повтор уже заведённой проблемы.

- get_issue(number) — карточка обращения: суть, текст ошибки, статус, причина и решение, связи, последние
  комментарии;
- search_issues(query, open_only) — поиск по смыслу (related.find_related: текст, объекты 1С, место ошибки)
  и по подстроке (тема, описание, текст ошибки, объекты, инициатор);
- issue_context(question) — то, что подставляется в первое сообщение агенту: карточки упомянутых в вопросе
  номеров и похожие обращения («возможный дубль»), если сходство заметное.

Контакты инициатора: имя и организация — да (это внутренний контур и рабочие данные), e-mail и телефон — нет.
"""

from __future__ import annotations

import re
from typing import Any

from copilot1c.issues import IssueRegistry, number, parse_number

_MENTION = re.compile(r"(?:ОБР|OBR)\s*-?\s*0*(\d{1,6})\b|обращени\w*\s*(?:№|N|номер)?\s*0*(\d{1,6})\b", re.IGNORECASE)
DUPLICATE_MIN = 0.45   # сходство, с которого похожее обращение показывается агенту как возможный дубль
MAX_MENTIONS = 3


def mentioned_numbers(text: str) -> list[int]:
    out = []
    for m in _MENTION.finditer(text or ""):
        n = int(m.group(1) or m.group(2))
        if n not in out:
            out.append(n)
    return out[:MAX_MENTIONS]


def _short(text: str | None, n: int) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def card(issue: dict[str, Any]) -> dict[str, Any]:
    """Карточка для модели: только то, что нужно для ответа."""
    ini = issue.get("initiator") or {}
    comments = [e for e in issue.get("events") or [] if e.get("type") == "comment" and e.get("comment")]
    out = {
        "номер": issue["number"], "тема": issue["title"], "статус": issue.get("status_label"),
        "категория": issue.get("category_label"), "приоритет": issue.get("priority_label"),
        "ответственный": issue.get("assignee"), "инициатор": " · ".join(x for x in (ini.get("name"), ini.get("organization"))
                                                                          if x) or None,
        "сообщили": (issue.get("reported_at") or issue.get("created_at") or "")[:10],
        "объекты": issue.get("objects") or [], "описание": _short(issue.get("description"), 1500),
        "текст_ошибки": issue.get("error_text"), "причина": issue.get("root_cause"), "решение": issue.get("resolution"),
        "дубль_обращения": number(issue["duplicate_of"]) if issue.get("duplicate_of") else None,
        "тест_кейсы_ПиМИ": issue.get("test_case_ids") or [], "пункты_ТЗ": issue.get("requirement_ids") or [],
        "в_базе_знаний": bool(issue.get("kb_material_id")),
        "комментарии": [f"{(e.get('at') or '')[:10]} {e.get('actor') or ''}: {_short(e['comment'], 300)}"
                        for e in comments[-5:]],
        "источник": f"обращение {issue['number']} «{issue['title']}»",
    }
    return {k: v for k, v in out.items() if v not in (None, "", [])}


def get_issue(conn, project: str, ref: str | int) -> dict[str, Any]:
    n = ref if isinstance(ref, int) else parse_number(str(ref))
    if n is None:
        return {"error": f"не понял номер обращения «{ref}» — нужен вида ОБР-0012 или 12"}
    issue = IssueRegistry(conn, project).get(n)
    if issue is None:
        return {"error": f"обращения {number(n)} нет"}
    return card(issue)


def search_issues(conn, project: str, query: str, open_only: bool = False, k: int = 5) -> list[dict[str, Any]]:
    """По смыслу (как «Похожие» в карточке) + по подстроке; без повторов, лучшие сверху."""
    from copilot1c.issues import OPEN_STATUSES
    from copilot1c.related import find_related

    found: dict[int, dict[str, Any]] = {}
    for x in find_related(conn, project, {"title": query, "description": query, "error_text": query})["issues"]:
        found[x["id"]] = {"id": x["id"], "score": x["score"], "почему": x["why"]}
    for row in IssueRegistry(conn, project).list(q=query, limit=k * 2):
        found.setdefault(row["id"], {"id": row["id"], "score": 0.3, "почему": ["совпадение по тексту"]})
    out = []
    reg = IssueRegistry(conn, project)
    for item in sorted(found.values(), key=lambda x: -x["score"]):
        issue = reg.get(item["id"])
        if issue is None or (open_only and issue["status"] not in OPEN_STATUSES):
            continue
        out.append({"номер": issue["number"], "тема": issue["title"], "статус": issue.get("status_label"),
                    "сообщили": (issue.get("reported_at") or issue.get("created_at") or "")[:10],
                    "есть_решение": bool(issue.get("resolution")), "сходство": item["score"], "почему": item["почему"],
                    "источник": f"обращение {issue['number']} «{issue['title']}»"})
        if len(out) >= k:
            break
    return out


def issue_context(conn, project: str, question: str) -> tuple[str, list[dict[str, Any]]]:
    """Блок для первого сообщения агенту и список обращений (для ответа API). Пусто — если нечего сказать."""
    from copilot1c.related import find_related

    parts, listed = [], []
    for n in mentioned_numbers(question):
        c = get_issue(conn, project, n)
        if "error" in c:
            parts.append(f"Обращение {number(n)} упомянуто в вопросе, но его нет в базе обращений.")
            continue
        parts.append(f"Обращение {c['номер']}, упомянутое в вопросе:\n{_fmt(c)}")
        listed.append({"number": c["номер"], "id": n, "title": c["тема"], "status": c.get("статус"), "kind": "mentioned"})
    seen = {x["id"] for x in listed}
    similar = [x for x in find_related(conn, project, {"title": question, "description": question,
                                                        "error_text": question})["issues"]
               if x["score"] >= DUPLICATE_MIN and x["id"] not in seen][:3]
    if similar:
        reg = IssueRegistry(conn, project)
        rows = []
        for x in similar:
            issue = reg.get(x["id"])
            if issue is None:
                continue
            c = card(issue)
            rows.append(f"- {c['номер']} «{c['тема']}» — {c.get('статус')}; почему похоже: {', '.join(x['why'])}"
                        + (f"; решение: {_short(c['решение'], 400)}" if c.get("решение") else ""))
            listed.append({"number": c["номер"], "id": x["id"], "title": c["тема"], "status": c.get("статус"),
                           "kind": "similar", "why": x["why"]})
        if rows:
            parts.append("Похожие зарегистрированные обращения — возможные дубли:\n" + "\n".join(rows))
    return "\n\n".join(parts), listed


def _fmt(c: dict[str, Any]) -> str:
    keys = ("тема", "статус", "приоритет", "ответственный", "инициатор", "сообщили", "объекты", "описание", "текст_ошибки",
            "причина", "решение", "дубль_обращения", "тест_кейсы_ПиМИ", "пункты_ТЗ", "комментарии")
    lines = []
    for k in keys:
        v = c.get(k)
        if v in (None, "", []):
            continue
        lines.append(f"  {k}: {'; '.join(v) if isinstance(v, list) else v}")
    return "\n".join(lines)
