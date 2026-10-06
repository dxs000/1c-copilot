"""Пакет для эксперта (Claude) через оператора: промт + все нужные материалы в одном архиве .zip.

Когда: локальный агент не нашёл ответа ни в базе проекта, ни в интернете, или задача требует глубокой
работы (разбор кода, проектирование доработки, длинная цепочка рассуждений). Агент сам рекомендует
эскалацию (инструмент prepare_escalation), аналитик может собрать пакет и вручную — кнопкой в чате или в
карточке обращения. Оператор передаёт архив в Claude, ответ возвращается в систему.

Состав архива:
  PROMPT.md                      — роль, контекст проекта, задача, что уже сделано, почему эскалация,
                                   вопрос эксперту, требования к ответу; ссылки на файлы пакета;
  materials/01_project_fragments.md — найденные фрагменты базы проекта с источниками;
  materials/02_internet.md       — что агент нашёл в интернете (ссылки);
  materials/03_issue.md          — карточка обращения, связанные тест-кейсы ПиМИ и пункты ТЗ, история;
  materials/04_attachments_text.md — текст приложенных файлов (письма, документы, логи);
  materials/05_local_agent.md    — ответ локального агента и его шаги;
  attachments/…                  — исходные файлы, только если аналитик это явно разрешил;
  MANIFEST.json                  — состав, источник, дата, что вырезано.

Персональные данные: во всех текстах пакета имя инициатора → «пользователь», имена аналитиков →
«аналитик», e-mail и телефоны маскируются (как в разборе для базы знаний). Исходные файлы писем
маскировать нельзя — поэтому по умолчанию в пакет идёт только их очищенный текст.
"""

from __future__ import annotations

import io
import json
import re
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from copilot1c.kb import _scrub
from copilot1c.materials import safe_filename

MAX_PACKAGE_BYTES = 50 * 1024 * 1024

ROLE = """Ты — эксперт по платформе «1С:Предприятие 8.3» и конфигурации «1С:Управление торговлей 11» (УТ 11):
обновление типовых конфигураций, расширения (.cfe), перенос доработок, язык BSL, запросы, права, производительность.
Тебе передан вопрос из проекта, на который локальный помощник не смог ответить уверенно."""

REQUIREMENTS = """## Требования к ответу

1. **Вывод** — 2–4 предложения: что происходит и что делать.
2. **Обоснование** — со ссылками на файлы этого пакета (например, `materials/01_project_fragments.md`, фрагмент [3])
   и на общеизвестные источники 1С (ИТС, документация платформы). Предположения помечай как предположения.
3. **Шаги решения** — по порядку, проверяемые; что проверить до и после.
4. **Риски** — что может сломаться при обновлении или в рабочей базе.
5. **Код** (если нужен) — расширение, а не правка типовой конфигурации; префикс объектов `КС_`; аннотации
   `&Перед` / `&После` / `&ИзменениеИКонтроль` (`&Вместо` — только с обоснованием); комментарии на русском.
6. **Чего не хватает** — какие данные или файлы нужны, если для точного ответа их недостаточно.

Отвечай по-русски. Не придумывай факты о проекте: всё, что о проекте известно, есть в материалах пакета."""


@dataclass
class EscalationInput:
    question: str
    expert_question: str = ""                 # что именно спросить эксперта; по умолчанию — вопрос аналитика
    reason: str = ""                          # почему эскалация (агент или аналитик)
    answer: str = ""                          # ответ локального агента
    sources: list[dict] = field(default_factory=list)      # [{label, text}] — фрагменты базы
    web_sources: list[dict] = field(default_factory=list)  # [{title, url, read}]
    tools: list[str] = field(default_factory=list)          # шаги агента
    attachments: list[tuple[str, bytes]] = field(default_factory=list)  # приложенные файлы
    include_raw: bool = False                 # класть исходные файлы (в письмах — имена и адреса)
    issue: dict[str, Any] | None = None       # карточка обращения (issues.get)
    related: dict[str, list] | None = None    # related.find_related


def _email_people(attachments: list[tuple[str, bytes]]) -> list[str]:
    """Имена всех авторов цепочек приложенных писем — их нужно убрать из текстов пакета."""
    from copilot1c.email_intake import is_email_file, read_chain

    out: list[str] = []
    for name, data in attachments:
        if not is_email_file(name):
            continue
        try:
            out += [m.name for m in read_chain(name, data)[0] if m.name and "@" not in m.name]
        except Exception:  # noqa: BLE001 — битое письмо: его текст и так не попадёт в пакет
            continue
    return list(dict.fromkeys(out))


def _md_escape_fence(text: str) -> str:
    return (text or "").replace("```", "ʼʼʼ")


def build(inp: EscalationInput, settings) -> tuple[bytes, dict[str, Any]]:
    """Собирает архив в памяти. Возвращает (zip, manifest)."""
    from copilot1c.chat_files import extract

    analysts = tuple(getattr(settings, "analysts", ()) or ())
    names = [*analysts]
    if inp.issue and (inp.issue.get("initiator") or {}).get("name"):
        names.insert(0, inp.issue["initiator"]["name"])
    names += _email_people(inp.attachments)  # авторы писем — после аналитиков: «Иванов» остаётся «аналитиком»

    def clean(t: str) -> str:
        return _scrub(t or "", names, analysts)

    files: dict[str, str | bytes] = {}
    listing: list[tuple[str, str]] = []

    # 01 — фрагменты базы проекта
    if inp.sources:
        parts = [f"### [{s.get('n', i)}] {clean(s.get('label', ''))}\n\n{clean(s.get('text', ''))}"
                 for i, s in enumerate(inp.sources, 1)]
        files["materials/01_project_fragments.md"] = "# Фрагменты базы проекта (найдены поиском)\n\n" + "\n\n".join(parts)
        listing.append(("materials/01_project_fragments.md", f"фрагменты базы проекта: {len(inp.sources)}"))
    # 02 — интернет
    if inp.web_sources:
        rows = [f"- [{w.get('title') or w.get('url')}]({w.get('url')})" + (" — прочитана локальным агентом"
                                                                         if w.get("read") else "")
                for w in inp.web_sources]
        files["materials/02_internet.md"] = "# Найдено в интернете локальным агентом\n\n" + "\n".join(rows)
        listing.append(("materials/02_internet.md", f"ссылки из интернета: {len(inp.web_sources)}"))
    # 03 — обращение
    if inp.issue:
        files["materials/03_issue.md"] = clean(_issue_md(inp.issue, inp.related))
        listing.append(("materials/03_issue.md", f"обращение {inp.issue.get('number', '')}: карточка, связи, история"))
    # 04 — текст приложенных файлов (очищенный), 05 — исходные файлы по разрешению
    if inp.attachments:
        items = extract(inp.attachments, settings)
        text_parts = [f"## Файл «{it.filename}»\n\n{clean(it.text)}" + (f"\n\n_({it.note})_" if it.note else "")
                      for it in items if it.text or it.note]
        if text_parts:
            files["materials/04_attachments_text.md"] = "# Текст приложенных файлов (очищен от персональных данных)\n\n" \
                                                        + "\n\n".join(text_parts)
            listing.append(("materials/04_attachments_text.md", f"текст приложенных файлов: {len(items)}"))
        if inp.include_raw:
            used: set[str] = set()
            for name, data in inp.attachments:
                fn = safe_filename(name)
                while fn in used:
                    fn = "_" + fn
                used.add(fn)
                files[f"attachments/{fn}"] = data
                listing.append((f"attachments/{fn}", "исходный файл (не очищен)"))
    # 05 — работа локального агента
    if inp.answer or inp.tools:
        body = "# Ответ локального агента\n\n" + clean(inp.answer or "—")
        if inp.tools:
            body += "\n\n## Шаги\n\n" + "\n".join(f"- {t}" for t in inp.tools)
        files["materials/05_local_agent.md"] = body
        listing.append(("materials/05_local_agent.md", "ответ и шаги локального агента"))

    files["PROMPT.md"] = clean(_prompt(inp, settings, listing))
    manifest = {"id": uuid.uuid4().hex[:12], "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "project": getattr(settings, "project", ""), "question": clean(inp.question),
                "issue": inp.issue.get("number") if inp.issue else None, "reason": clean(inp.reason),
                "raw_attachments": inp.include_raw,
                "files": [{"path": p, "what": w} for p, w in [("PROMPT.md", "задача для эксперта"), *listing]]}
    files["MANIFEST.json"] = json.dumps(manifest, ensure_ascii=False, indent=2)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, content in files.items():
            z.writestr(path, content.encode("utf-8") if isinstance(content, str) else content)
    data = buf.getvalue()
    if len(data) > MAX_PACKAGE_BYTES:
        raise ValueError(f"архив больше {MAX_PACKAGE_BYTES // 2**20} МБ — уберите исходные файлы")
    manifest["size"] = len(data)
    return data, manifest


def _prompt(inp: EscalationInput, settings, listing: list[tuple[str, str]]) -> str:
    expert_q = (inp.expert_question or inp.question).strip()
    lines = [f"# Вопрос эксперту по 1С\n\n{ROLE}",
             "## Контекст проекта\n\nПроект обновления конфигурации «1С:Управление торговлей 11» (код проекта: "
             f"`{getattr(settings, 'project', '')}`). Доработки интегратора — объекты с префиксом `КС_` или суффиксом "
             "`(КС)`; они рискуют при обновлении. Материалы проекта: переписка, ТЗ, допсоглашения, ПиМИ "
             "(программа и методика испытаний).",
             f"## Исходный вопрос аналитика\n\n{inp.question.strip()}"]
    if inp.issue:
        lines.append(f"Связанное обращение: **{inp.issue.get('number')}** «{inp.issue.get('title')}» — "
                     "см. `materials/03_issue.md`.")
    if inp.reason:
        lines.append(f"## Почему вопрос передан эксперту\n\n{inp.reason.strip()}")
    done = []
    if inp.sources:
        done.append(f"поиск по базе проекта — найдено фрагментов: {len(inp.sources)}")
    if inp.web_sources:
        done.append(f"поиск в интернете — ссылок: {len(inp.web_sources)}")
    if inp.answer:
        done.append("локальный агент дал предварительный ответ (`materials/05_local_agent.md`) — проверь его, "
                    "он может быть неполным или ошибочным")
    if done:
        lines.append("## Что уже сделано\n\n" + "\n".join(f"- {d}" for d in done))
    lines.append(f"## Вопрос эксперту\n\n{expert_q}")
    if listing:
        lines.append("## Материалы в пакете\n\n" + "\n".join(f"- `{p}` — {w}" for p, w in listing))
    lines.append(REQUIREMENTS)
    return "\n\n".join(lines) + "\n"


def _issue_md(issue: dict[str, Any], related: dict[str, list] | None) -> str:
    f = issue
    rows = [("Тема", f.get("title")), ("Статус", f.get("status_label")), ("Категория", f.get("category_label")),
            ("Приоритет", f.get("priority_label")), ("Объекты", ", ".join(f.get("objects") or [])),
            ("База", f.get("infobase")), ("Версия конфигурации", f.get("config_version")),
            ("Версия платформы", f.get("platform_version")), ("Когда сообщили", (f.get("reported_at") or "")[:16])]
    out = [f"# Обращение {f.get('number', '')}", "\n".join(f"- **{k}:** {v}" for k, v in rows if v)]
    for title, key in (("Описание", "description"), ("Текст ошибки 1С", "error_text"), ("Шаги воспроизведения", "steps"),
                       ("Ожидалось", "expected"), ("Получено", "actual"), ("Причина (если известна)", "root_cause"),
                       ("Решение (если есть)", "resolution")):
        if f.get(key):
            body = f"```\n{_md_escape_fence(f[key])}\n```" if key == "error_text" else f[key]
            out.append(f"## {title}\n\n{body}")
    if related:
        if related.get("test_cases"):
            out.append("## Связанные тест-кейсы ПиМИ\n\n" + "\n".join(
                f"- № {t['num']} — {t.get('function') or ''} ({t.get('section') or ''}); результат: {t.get('result') or '—'}"
                for t in related["test_cases"]))
        if related.get("requirements"):
            out.append("## Связанные пункты плана тестирования ТЗ\n\n" + "\n".join(
                f"- п. {r['num']} — {r.get('object') or ''}: {(r.get('text') or '')[:200]}"
                for r in related["requirements"]))
        if related.get("issues"):
            out.append("## Похожие обращения\n\n" + "\n".join(
                f"- {x.get('number')} «{x.get('title')}» — {x.get('status_label')}" for x in related["issues"]))
    comments = [e for e in f.get("events") or [] if e.get("type") == "comment" and e.get("comment")]
    if comments:
        out.append("## Комментарии аналитиков\n\n" + "\n".join(f"- {(e.get('at') or '')[:10]}: {e['comment']}"
                                                                for e in comments))
    return "\n\n".join(out)


def package_name(manifest: dict[str, Any]) -> str:
    base = f"Claude — {manifest['issue']}" if manifest.get("issue") else "Claude — вопрос"
    stamp = manifest["created_at"][:16].replace("T", " ").replace(":", "-")
    return safe_filename(f"{base} {stamp}.zip")


def store(root: Path, data: bytes, manifest: dict[str, Any]) -> Path:
    """Архив на диск (папка внутри .cache — она уже доступна службе на запись)."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{manifest['id']}.zip"
    path.write_bytes(data)
    (root / f"{manifest['id']}.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return path


def load(root: Path, package_id: str) -> tuple[Path, dict[str, Any]] | None:
    if not re.fullmatch(r"[0-9a-f]{12}", package_id or ""):
        return None
    path, meta = root / f"{package_id}.zip", root / f"{package_id}.json"
    if not path.is_file() or not meta.is_file():
        return None
    return path, json.loads(meta.read_text(encoding="utf-8"))
