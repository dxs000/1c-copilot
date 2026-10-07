"""Инструменты агента-оркестратора и цикл вызова инструментов.

Описания инструментов в формате function calling (OpenAI-совместимый Chat Completions AI Studio).
Эти же функции можно выставить наружу MCP-сервером для IDE.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from copilot1c.code1c.platform import Designer
from copilot1c.config import Settings, get_settings
from copilot1c.graph.store import GraphStore
from copilot1c.index.yandex import client
from copilot1c.retrieval import format_hits, smart_search

SYSTEM_PROMPT = """Ты — 1С Project Copilot, аналитик проекта внедрения 1С. Отвечай по-русски, коротко и по делу.

Источники. Каждое утверждение подкрепляй источником в человекочитаемом виде — так, как он указан в
поле «источник» найденного фрагмента: письмо (тема, дата, автор), документ (название, редакция,
раздел или пункт), № тест-кейса ПиМИ, путь к модулю и строки. Никогда не указывай внутренние
идентификаторы файлов и чанков, не придумывай номера страниц. Утверждение без источника помечай
как предположение.

Порядок работы. В сообщении пользователя уже есть найденные фрагменты. Если их достаточно — сразу
отвечай, без вызова инструментов. Инструменты вызывай, только если нужного факта во фрагментах нет;
не повторяй один и тот же запрос. Аналитические вопросы («что не покрыто», «что затронет
обновление») решай в несколько шагов: поиск → граф → SQL.

Обращения. База зарегистрированных проблем доступна: get_issue — по номеру (ОБР-0012 или 12),
search_issues — по смыслу. Если в сообщении уже есть блок «Похожие зарегистрированные обращения» или
вопрос описывает проблему — проверь, не зарегистрирована ли она: назови номер, статус и что по ней известно
(«это похоже на ОБР-0012, статус «в работе»»), если есть решение — опирайся на него. Новое обращение
предлагает интерфейс; ты не регистрируй и не утверждай, что зарегистрировал.

Интернет (web_search, read_page) — только после базы проекта и только если ответа там нет, а вопрос
про платформу 1С, типовую конфигурацию, текст ошибки, версии, методику — то, что известно вне проекта.
Договорённости, сроки, переписку и ТЗ проекта в интернете не ищи — их там нет. Запрос в интернет
формулируй только из текста ошибки, имён объектов метаданных, номеров версий и общих слов: без имён
людей, e-mail, телефонов, серверов и названия заказчика (система это проверяет и вырежет). Для 1С
предпочитай its.1c.ru, v8.1c.ru, infostart.ru (параметр sites). Если фрагмента мало — read_page по
лучшей ссылке. Найденное в интернете помечай в ответе «по данным интернета» со ссылкой и отделяй от
фактов проекта; противоречие между ними называй явно.

Эксперт. Если ответа нет ни в базе проекта, ни в интернете, или задача требует глубокой работы (разбор и
правка кода BSL, проектирование доработки, длинная цепочка рассуждений, противоречивые источники) — вызови
prepare_escalation: укажи, почему не справился, и сформулируй точный вопрос эксперту. Пакет соберёт
аналитик. Всё равно дай в ответе то, что удалось выяснить, и прямо скажи, чего не хватает.

Приложенные файлы. Если в сообщении есть блок «Приложено к вопросу» — это материалы, которые аналитик
приложил сейчас (письма, документы, скриншоты); их нет в базе проекта. Используй их как главный
контекст вопроса и ссылайся на них как «приложенный файл «имя»»; факты из базы проекта — как обычно.

Предметная область. Объекты с префиксом КС_ или суффиксом (КС) — доработки интегратора.
Сгенерированный код перед выдачей проверяй инструментом build_and_check."""


def _fn(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": required}}}


_STR = {"type": "string"}
TOOLS = [
    _fn("search_docs", "Гибридный поиск по переписке, ТЗ, ДС, ПиМИ с фильтрами метаданных.",
        {"query": _STR, "doc_type": {"type": "string", "enum": ["email", "tz", "ds", "pimi", "doc"]},
         "k": {"type": "integer"}}, ["query"]),
    _fn("search_code", "Поиск методов BSL и карточек объектов по описанию или имени.",
        {"query": _STR, "config": {"type": "string", "description": "Метка выгрузки, напр. «УТ 11.5.27.75»"},
         "custom_only": {"type": "boolean"}, "k": {"type": "integer"}}, ["query"]),
    _fn("graph_query", "Связи сущности в графе: кто упоминает, какие тест-кейсы проверяют, кто вызывает.",
        {"entity": {"type": "string", "description": "Ключ вида kind:name или имя объекта/метода"},
         "rel": _STR}, ["entity"]),
    _fn("get_module", "Полный текст модуля BSL из выгрузки.",
        {"config": _STR, "module": {"type": "string", "description": "Путь вида Catalogs/X/Ext/ObjectModule.bsl"}},
        ["config", "module"]),
    _fn("diff_versions", "Дифф двух выгрузок конфигурации в Git (теги/коммиты) по пути.",
        {"base": _STR, "target": _STR, "path": _STR}, ["base", "target"]),
    _fn("sql", "SELECT-запрос только на чтение к реестрам: test_cases, requirements, md_objects, "
        "bsl_methods, bsl_calls, uncovered_requirements, custom_objects.", {"query": _STR}, ["query"]),
    _fn("get_issue", "Обращение (зарегистрированная проблема) по номеру: суть, текст ошибки, статус, причина, решение, "
        "связи, комментарии.", {"number": {"type": "string", "description": "ОБР-0012 или 12"}}, ["number"]),
    _fn("search_issues", "Поиск среди зарегистрированных обращений: по смыслу, тексту ошибки 1С, объектам, теме, "
        "инициатору. Для проверки дублей и поиска решённых похожих проблем.",
        {"query": _STR, "open_only": {"type": "boolean", "description": "Только незакрытые"}}, ["query"]),
    _fn("web_search", "Поиск в интернете (Yandex). Только когда в базе проекта ответа нет и вопрос про платформу, "
        "типовую конфигурацию, ошибку, версии. Запрос — без имён, контактов, серверов и названия заказчика.",
        {"query": {"type": "string", "description": "Текст ошибки, объекты 1С, версии, общие слова"},
         "sites": {"type": "array", "items": {"type": "string"},
                   "description": "Ограничить сайтами, напр. its.1c.ru, v8.1c.ru, infostart.ru"}}, ["query"]),
    _fn("read_page", "Текст страницы из результатов web_search, если фрагмента недостаточно.",
        {"url": _STR}, ["url"]),
    _fn("prepare_escalation", "Рекомендовать передать вопрос эксперту (Claude) через оператора: аналитику будет "
        "предложено собрать пакет с промтом и материалами. Только если ответа нет в базе и в интернете или задача "
        "требует глубокой работы.",
        {"reason": {"type": "string", "description": "Почему не удалось ответить уверенно"},
         "expert_question": {"type": "string", "description": "Точный вопрос эксперту, с нужными деталями"}},
        ["reason", "expert_question"]),
    _fn("build_and_check", "Собрать расширение из XML/BSL в песочнице 1С и проверить /CheckModules.",
        {"src_dir": _STR, "extension": _STR}, ["src_dir", "extension"]),
]


@dataclass
class ToolContext:
    settings: Settings
    dumps_root: Path  # Git-репозиторий выгрузок: <dumps_root>/<config>/...
    store: GraphStore | None = None  # PostgreSQL: база поиска, граф, реестры, обращения
    contours: list[int] | None = None  # область поиска (контуры); None — вся база
    web_sources: list[dict] = field(default_factory=list)  # что агент нашёл и прочитал в интернете
    web_searches: int = 0
    web_pages: int = 0
    escalation: dict | None = None  # рекомендация агента передать вопрос эксперту
    issues: list[dict] = field(default_factory=list)  # обращения, показанные агенту (упомянутые и похожие)


def _raw_search(ctx: ToolContext) -> Callable[[str, dict, int], list[dict]]:
    from copilot1c.search import PgIndex, search_fn

    if ctx.store is None:
        raise RuntimeError("PostgreSQL недоступен — поиск по базе проекта невозможен")
    return search_fn(PgIndex(ctx.store.conn, ctx.settings), ctx.contours)


def _search(ctx: ToolContext, query: str, filters: dict[str, str], k: int) -> list[dict]:
    return format_hits(smart_search(_raw_search(ctx), query, filters, k))


def make_handlers(ctx: ToolContext) -> dict[str, Callable[..., Any]]:
    project = ctx.settings.project

    def search_docs(query: str, doc_type: str | None = None, k: int = 8):
        f = {"project": project}
        if doc_type:
            f["doc_type"] = doc_type
        return _search(ctx, query, f, k)

    def search_code(query: str, config: str | None = None, custom_only: bool = False, k: int = 8):
        f = {"project": project, "doc_type": "bsl_method"}
        if config:
            f["doc_version"] = config
        if custom_only:
            f["custom"] = "true"
        return _search(ctx, query, f, k)

    def graph_query(entity: str, rel: str | None = None):
        assert ctx.store, "graph store not configured"
        key = entity if ":" in entity else None
        sql = """SELECT r.src, r.rel, r.dst, c.source, c.title FROM relations r
                 LEFT JOIN chunks c ON c.chunk_id = r.source_chunk
                 WHERE (r.src = %(k)s OR r.dst = %(k)s OR r.src ILIKE %(n)s OR r.dst ILIKE %(n)s)
                   AND (%(rel)s::text IS NULL OR r.rel = %(rel)s) LIMIT 100"""
        return ctx.store.query(sql, {"k": key, "n": f"%:{entity.casefold()}", "rel": rel})

    def get_module(config: str, module: str):
        path = (ctx.dumps_root / config / module).resolve()
        if not path.is_relative_to(ctx.dumps_root.resolve()):
            return {"error": "путь вне каталога выгрузок"}
        return path.read_text(encoding="utf-8-sig", errors="replace")

    def diff_versions(base: str, target: str, path: str = "."):
        out = subprocess.run(["git", "-C", str(ctx.dumps_root), "diff", "--stat", base, target, "--", path],
                             capture_output=True, text=True, timeout=120)
        return out.stdout[-20000:] or out.stderr

    def sql(query: str):
        assert ctx.store, "graph store not configured"
        if not query.lstrip().lower().startswith(("select", "with")):
            return {"error": "разрешены только SELECT-запросы"}
        return ctx.store.query(query)

    def web_search(query: str, sites: list[str] | None = None):
        from copilot1c.web import WebError
        from copilot1c.web import web_search as search

        if ctx.web_searches >= ctx.settings.web_max_searches:
            return {"error": f"лимит поисков в интернете на вопрос ({ctx.settings.web_max_searches}) исчерпан — "
                             "отвечай по найденному"}
        ctx.web_searches += 1
        try:
            found = search(query, ctx.settings, sites=sites)
        except WebError as exc:
            return {"error": str(exc)}
        for r in found["results"]:
            if not any(w["url"] == r["url"] for w in ctx.web_sources):
                ctx.web_sources.append({"title": r["title"], "url": r["url"], "domain": r["domain"], "read": False})
        return {"запрос_отправлен": found["query_sent"], "вырезано": found["removed"],
                "results": [{**r, "источник": f"интернет: {r['title']} — {r['url']}"} for r in found["results"]]}

    def read_page(url: str):
        from copilot1c.web import WebError
        from copilot1c.web import read_page as fetch

        if ctx.web_pages >= ctx.settings.web_max_pages:
            return {"error": f"лимит чтения страниц на вопрос ({ctx.settings.web_max_pages}) исчерпан"}
        ctx.web_pages += 1
        try:
            page = fetch(url, ctx.settings)
        except WebError as exc:
            return {"error": str(exc)}
        known = next((w for w in ctx.web_sources if w["url"] in (url, page["url"])), None)
        if known:
            known["read"] = True
        else:
            ctx.web_sources.append({"title": page["title"] or page["url"], "url": page["url"],
                                    "domain": page["url"].split("/")[2], "read": True})
        return {**page, "источник": f"интернет: {page['title'] or page['url']} — {page['url']}"}

    def get_issue(number: str):
        from copilot1c.agent.issues_tools import get_issue as fetch

        assert ctx.store, "PostgreSQL недоступен"
        return fetch(ctx.store.conn, project, number)

    def search_issues(query: str, open_only: bool = False):
        from copilot1c.agent.issues_tools import search_issues as find

        assert ctx.store, "PostgreSQL недоступен"
        return find(ctx.store.conn, project, query, open_only) or {"result": "похожих обращений нет"}

    def prepare_escalation(reason: str, expert_question: str):
        ctx.escalation = {"reason": (reason or "").strip()[:2000], "expert_question": (expert_question or "").strip()[:4000]}
        return {"ok": True, "note": "Аналитику будет предложено собрать пакет для эксперта. Дай в ответе то, что "
                                    "удалось выяснить, и скажи, чего не хватает."}

    def build_and_check(src_dir: str, extension: str):
        out = Path(src_dir).with_suffix(".cfe")
        results = Designer(ctx.settings).build_extension(src_dir, out, extension)
        return [{"step": r.argv[r.argv.index("/Out") + 2], "ok": r.ok, "log": r.log[-4000:]} for r in results]

    return {f.__name__: f for f in (search_docs, search_code, graph_query, get_module, diff_versions, sql,
                                     get_issue, search_issues, web_search, read_page, prepare_escalation,
                                     build_and_check)}


@dataclass
class AgentResult:
    answer: str
    trace: list[dict]  # вызовы инструментов: {"tool", "args", "result_chars" | "repeat" | "error"}
    steps: int
    web_sources: list[dict] = field(default_factory=list)  # найденное и прочитанное в интернете
    escalation: dict | None = None  # {reason, expert_question}, если агент рекомендовал эксперта
    issues: list[dict] = field(default_factory=list)  # обращения, показанные агенту: упомянутые и возможные дубли


def available_tools(ctx: ToolContext) -> list[dict]:
    unavailable: set[str] = set()
    if ctx.store is None:  # без PostgreSQL инструменты графа, реестров и обращений не предлагаются вовсе
        unavailable |= {"graph_query", "sql", "get_issue", "search_issues"}
    s = ctx.settings
    if not (getattr(s, "web_search", False) and s.yc_api_key and s.yc_folder_id):  # интернет выключен или нет ключа
        unavailable |= {"web_search", "read_page"}
    if not ctx.dumps_root.exists():  # код 1С ещё не выгружен и не проиндексирован
        unavailable |= {"get_module", "diff_versions", "search_code"}
    if not Path(ctx.settings.onec_bin).exists():
        unavailable.add("build_and_check")
    return [t for t in TOOLS if t["function"]["name"] not in unavailable]


def _prefetch_message(question: str, ctx: ToolContext, k: int, attached: str = "",
                      search_query: str | None = None) -> str:
    """Поиск до первого вызова модели: на простые вопросы она отвечает сразу, без цикла инструментов.
    attached — текст файлов, приложенных к вопросу в чате; search_query — запрос для поиска, если он
    должен отличаться от вопроса (например, вопрос «что тут?» + тема приложенного письма)."""
    head = f"Вопрос: {question}"
    if attached:
        head += f"\n\nПриложено к вопросу (файлы аналитика, в базе проекта их нет):\n\n{attached}"
    if ctx.store is not None:  # упомянутые номера и возможные дубли — до первого вызова модели
        from copilot1c.agent.issues_tools import issue_context

        try:
            block, ctx.issues = issue_context(ctx.store.conn, ctx.settings.project,
                                              " ".join(x for x in (question, search_query) if x))
        except Exception as exc:  # noqa: BLE001 — база обращений недоступна: ответ по документам всё равно будет
            ctx.store.conn.rollback()
            block = f"(Обращения не проверены: {type(exc).__name__}. Можно вызвать search_issues.)"
        if block:
            head += f"\n\nОбращения:\n\n{block}"
    try:
        hits = _search(ctx, search_query or question, {"project": ctx.settings.project}, k)
    except Exception as exc:  # noqa: BLE001 — без предварительного поиска агент всё равно может искать сам
        return f"{head}\n\n(Предварительный поиск не удался: {type(exc).__name__}. Используй инструменты.)"
    found = "\n\n".join(f"[{i}] Источник: {h['источник']}\n{h['текст']}" for i, h in enumerate(hits, 1))
    return f"{head}\n\nНайденные фрагменты:\n\n{found or '(ничего не найдено)'}"


def run_agent(question: str, ctx: ToolContext, max_steps: int = 6, prefetch_k: int = 8, attached: str = "",
              search_query: str | None = None) -> AgentResult:
    """Цикл агента: предварительный поиск → модель с инструментами → обязательный финальный ответ."""
    s = ctx.settings or get_settings()
    handlers = make_handlers(ctx)
    tools = available_tools(ctx)
    first = _prefetch_message(question, ctx, prefetch_k, attached, search_query)
    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT},
                                      {"role": "user", "content": first}]
    trace: list[dict] = []
    seen_calls: dict[str, str] = {}
    model = s.model_uri(s.model_orchestrator)

    for step in range(1, max_steps + 1):
        resp = client(s).chat.completions.create(model=model, messages=messages, tools=tools, temperature=0.1)
        msg = resp.choices[0].message
        if not msg.tool_calls:
            if msg.content:
                return AgentResult(msg.content, trace, step, ctx.web_sources, ctx.escalation, ctx.issues)
            break
        messages.append(msg.model_dump(exclude_none=True))
        for call in msg.tool_calls:
            key = f"{call.function.name}:{call.function.arguments}"
            entry: dict[str, Any] = {"tool": call.function.name, "args": call.function.arguments}
            if key in seen_calls:  # модель повторяет запрос — не тратим время, напоминаем о результате
                content = "Этот вызов уже выполнялся, результат выше. Отвечай по имеющимся данным."
                entry["repeat"] = True
            else:
                try:
                    args = json.loads(call.function.arguments or "{}")
                    result = handlers[call.function.name](**args)
                except Exception as exc:  # ошибка инструмента возвращается модели, а не роняет цикл
                    result = {"error": f"{type(exc).__name__}: {exc}"}
                    entry["error"] = result["error"]
                content = json.dumps(result, ensure_ascii=False, default=str)[:30000]
                seen_calls[key] = content
                entry["result_chars"] = len(content)
            trace.append(entry)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})

    # Лимит шагов исчерпан: просим ответ по уже собранному, без новых вызовов
    messages.append({"role": "user", "content": "Инструменты больше недоступны. Дай окончательный ответ по уже "
                                                "найденным данным, с источниками; чего не нашлось — так и скажи."})
    try:
        resp = client(s).chat.completions.create(model=model, messages=messages, tools=tools, tool_choice="none",
                                                 temperature=0.1)
    except Exception:  # noqa: BLE001 — если tool_choice не поддержан, повторяем без инструментов
        resp = client(s).chat.completions.create(model=model, messages=messages, temperature=0.1)
    return AgentResult(resp.choices[0].message.content or "Ответ не получен.", trace, max_steps + 1, ctx.web_sources,
                       ctx.escalation, ctx.issues)


def ask(question: str, ctx: ToolContext, max_steps: int = 6) -> str:
    return run_agent(question, ctx, max_steps).answer
