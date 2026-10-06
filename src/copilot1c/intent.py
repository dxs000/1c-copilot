"""Тип сообщения аналитика в чате и черновик обращения, если это сообщение о проблеме.

Типы (сообщение может нести несколько):
  question  — найти ответ в материалах проекта («почему 11.5.27.75, а не 11.6?»);
  issue     — проблема у пользователей («после обновления не проводится реализация, ошибка …»);
  summary   — сводка, список, агрегат («какие требования не покрыты ПиМИ?»);
  document  — подготовить текст («подготовь письмо заказчику…»);
  knowledge — новое или исправленное знание для базы («к сведению: договорились…», «это устарело»).

Как определяется:
1. Эвристики — быстро, бесплатно, предсказуемо. Каждое правило добавляет баллы своему типу и попадает
   в список сигналов (видно, почему так решено). Самый сильный признак проблемы — текст ошибки 1С
   ({Документ.X.МодульОбъекта(245)}, «Поле объекта не обнаружено» и т. п.).
2. Модель — только если эвристики не уверены (слабый лидер или два типа почти равны) и в .env есть ключи
   AI Studio. Отвечает по JSON-схеме; при ошибке вызова остаётся результат эвристик.

Для проблемы собирается черновик обращения: тема (первая содержательная фраза), описание (весь текст),
текст ошибки 1С, объекты метаданных, категория и приоритет по ключевым словам. Черновик ничего не
сохраняет — аналитик подтверждает его в чате.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("copilot1c.intent")

INTENTS = ("question", "issue", "summary", "document", "knowledge")
LABELS = {"question": "вопрос", "issue": "проблема", "summary": "сводка", "document": "подготовить документ",
          "knowledge": "новое знание"}
ISSUE_THRESHOLD = 2.5   # баллов, чтобы предложить зарегистрировать обращение
CONFIDENT = 3.0         # лидер с таким счётом и заметным отрывом — модель не нужна

_I = re.IGNORECASE
# Текст ошибки платформы 1С: {Документ.РеализацияТоваровУслуг.МодульОбъекта(245)}: …
_ERROR_LOCATION_RE = re.compile(r"\{[A-Za-zА-ЯЁа-яё]+\.[^{}\n]{2,200}\(\d+(?:,\d+)?\)\}")
_ERROR_PHRASES = ("ошибка при вызове метода контекста", "поле объекта не обнаружено", "нарушение прав доступа",
                  "ошибка выполнения запроса", "значение не является значением объектного типа",
                  "неверные параметры", "деление на 0", "индекс находится за границами", "метод объекта не обнаружен",
                  "переменная не определена", "недостаточно прав", "ошибка блокировки", "превышено максимальное время",
                  "конфликт блокировок", "объект не найден", "ошибка преобразования данных", "сеанс работы завершен")
_ISSUE_WORDS = re.compile(
    r"\bошибк|\bне\s+(?:работа|провод|запис|открыва|загружа|груз|выгружа|формиру|отобража|печата|видн|видит|сохраня|"
    r"считает|рассчитыва|заполня|подтягива|приход|уход|отправля|получа|меняет|удаля|созда)|"
    r"\bне\s+мо(?:жет|жем|гут|гу)\s+\w+|\bпада|вылета|\bзависа|\bзавис\b|сбой|тормоз|медленн|расхожд|не\s+сход|некорректн|неправильн|неверно\s+(?:счита|"
    r"рассчит|заполн)|пуст(?:ой|ая|ые)\s+(?:отч|печат|форм|спис)|исчез|пропал", _I)
_AFTER_UPDATE = re.compile(r"после\s+обновлени", _I)
_REPORTED = re.compile(r"пользовател\w*\s+(?:сообща|жалу|пишут|говорят)|жалоб|обращени\w*\s+от\b|у\s+(?:пользовател|"
                       r"бухгалтер|менеджер|склад|отдел)\w*\s+не\b", _I)
_QUESTION_START = re.compile(r"^\s*(?:как|почему|зачем|где|когда|какой|какая|какое|какие|каких|кто|что|сколько|"
                             r"чем|откуда|куда|можно\s+ли|есть\s+ли|нужно\s+ли|надо\s+ли|подходит\s+ли|верно\s+ли)\b", _I)
_SUMMARY_WORDS = re.compile(r"\bсписок\b|\bперечисли|\bперечень|\bвсе\s+(?:требовани|тест|объект|доработ|письм|обращени|"
                            r"вопрос)|\bсколько\b|сводк|статистик|не\s+покрыт|что\s+затрон|матриц|\bтаблиц\w*\s+(?:всех|по)",
                            _I)
_DOCUMENT_START = re.compile(r"^\s*(?:пожалуйста,?\s+)?(?:подготовь|напиши|составь|сформулируй|оформи|набросай|"
                             r"сгенерируй|сделай\s+(?:черновик|письмо|протокол|резюме|сводку\s+для)|переведи|"
                             r"перепиши)\w*\b", _I)
_KNOWLEDGE_WORDS = re.compile(r"\bк\s+сведению|\bзапомни|добавь\s+в\s+(?:базу|знани)|для\s+информации|\bfyi\b|"
                              r"\bдоговорились|\bрешили\b|\bутвердили|\bсогласовали|\bустарел|\bбольше\s+не\s+(?:актуал|"
                              r"действ)|исправь\s+в\s+базе|теперь\s+(?:версия|сервер|платформа|срок)", _I)
_GREETING = re.compile(r"^\s*(?:добрый\s+(?:день|вечер)|доброе\s+утро|здравствуйте|привет|коллеги|уважаем\w+[^,\n]*)"
                       r"[,!.\s]*", _I)
# Объекты 1С «Вид.Имя» в тексте ошибок и сообщений
_MD_DOTTED = re.compile(r"\b(Справочник|Документ|РегистрСведений|РегистрНакопления|РегистрБухгалтерии|Обработка|Отчет|"
                        r"ОбщийМодуль|Перечисление|ПланВидовХарактеристик|БизнесПроцесс|Задача|ЖурналДокументов)"
                        r"\.([A-Za-zА-ЯЁа-яё0-9_]+)")
_MODULE_NAMES = {"МодульОбъекта", "МодульМенеджера", "МодульФормы", "МодульНабораЗаписей", "Форма", "ФормаОбъекта",
                 "ФормаСписка", "МодульКоманды"}


@dataclass
class Intent:
    scores: dict[str, float]
    signals: list[str] = field(default_factory=list)   # почему так решено (для отладки и интерфейса)
    method: str = "heuristic"                          # heuristic | llm

    @property
    def ranked(self) -> list[tuple[str, float]]:
        return sorted(((k, v) for k, v in self.scores.items() if v > 0), key=lambda x: -x[1])

    @property
    def primary(self) -> str:
        r = self.ranked
        return r[0][0] if r else "question"  # ничего не сработало — считаем вопросом к базе

    @property
    def confident(self) -> bool:
        r = self.ranked
        if not r:
            return False
        top = r[0][1]
        second = r[1][1] if len(r) > 1 else 0.0
        return top >= CONFIDENT and second <= top * 0.6

    @property
    def is_issue(self) -> bool:
        return self.scores.get("issue", 0) >= ISSUE_THRESHOLD

    def to_dict(self) -> dict[str, Any]:
        return {"primary": self.primary, "primary_label": LABELS[self.primary],
                "intents": [{"type": k, "label": LABELS[k], "score": round(v, 1)} for k, v in self.ranked],
                "is_issue": self.is_issue, "confident": self.confident, "method": self.method,
                "signals": self.signals}


def error_lines(text: str) -> list[str]:
    """Строки с текстом ошибки 1С (место ошибки в фигурных скобках или типовая фраза платформы)."""
    out = []
    for ln in text.splitlines():
        low = ln.casefold()
        if _ERROR_LOCATION_RE.search(ln) or any(p in low for p in _ERROR_PHRASES):
            out.append(ln.strip())
    return out


def heuristics(text: str, has_files: bool = False) -> Intent:
    t = text.strip()
    scores = dict.fromkeys(INTENTS, 0.0)
    signals: list[str] = []

    def add(kind: str, pts: float, why: str) -> None:
        scores[kind] += pts
        signals.append(f"{LABELS[kind]} +{pts:g}: {why}")

    low = t.casefold()
    if _ERROR_LOCATION_RE.search(t):
        add("issue", 3, "место ошибки 1С {…(строка)}")
    phrases = [p for p in _ERROR_PHRASES if p in low]
    if phrases:
        add("issue", 2.5, f"текст ошибки платформы: «{phrases[0]}»")
    # слова в кавычках — названия статусов и документов («Не работает» в ПиМИ), а не жалоба
    unquoted = re.sub(r"«[^»]{0,80}»|\"[^\"]{0,80}\"", " ", t)
    words = {m.group(0).casefold() for m in _ISSUE_WORDS.finditer(unquoted)}
    if words:
        add("issue", min(2 * len(words), 3.5), "слова о сбое: " + ", ".join(sorted(words)[:4]))
        if "?" not in unquoted:
            add("issue", 1, "утверждение, а не вопрос")
    if re.search(r"\bсрочн", t, _I):
        add("issue", 1, "«срочно»")
    if _AFTER_UPDATE.search(t):
        add("issue", 1, "«после обновления»")
    if _REPORTED.search(t):
        add("issue", 1.5, "сообщение от пользователей")
    if has_files:
        add("issue", 1, "приложены файлы")

    if "?" in t:
        add("question", 2, "вопросительный знак")
    if _QUESTION_START.search(t):
        add("question", 1.5, "начинается с вопросительного слова")

    if _SUMMARY_WORDS.search(t):
        add("summary", 2, "просьба о списке или сводке")
    if _DOCUMENT_START.search(t):
        add("document", 3, "просьба подготовить текст")
    if _KNOWLEDGE_WORDS.search(t):
        add("knowledge", 2.5, "сообщение-знание («к сведению», «договорились», «устарело»…)")

    # Вопрос о проблеме («почему не проводится реализация?») получает баллы и вопроса, и проблемы — это
    # нормально: агент ответит, а чат предложит зарегистрировать обращение.
    if scores["summary"] and scores["question"]:
        scores["question"] = max(scores["question"] - 2, 0)  # «сколько…?», «какие… не покрыты?» — сводка, а не поиск
    return Intent(scores, signals)


# ---------- модель ----------

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intents": {"type": "array", "items": {"type": "string", "enum": list(INTENTS)},
                    "description": "Все типы, которые есть в сообщении; первым — главный"},
        "reason": {"type": "string", "description": "Одна фраза: почему так"},
    },
    "required": ["intents"],
}
SYSTEM = (
    "Ты определяешь тип сообщения аналитика ИТ-отдела в чате системы по проекту 1С (обновление «Управление "
    "торговлей 11»). Типы: question — найти ответ в переписке, ТЗ, ПиМИ; issue — сообщение о проблеме у "
    "пользователей (ошибка, не работает, неверные данные), которую нужно зарегистрировать; summary — сводка, "
    "список, агрегат по материалам; document — подготовить текст (письмо, протокол, резюме); knowledge — "
    "новая или исправленная информация для базы знаний. Сообщение может нести несколько типов."
)


def refine_with_llm(text: str, intent: Intent, settings) -> Intent:
    """Уточнение моделью для неуверенных случаев. Баллы эвристик сохраняются, типы из ответа модели усиливаются."""
    from copilot1c.index.yandex import chat_json

    try:
        res = chat_json(f"Сообщение:\n{text[:4000]}", SCHEMA, model=settings.model_batch, system=SYSTEM,
                        settings=settings)
    except Exception as exc:  # noqa: BLE001 — модель недоступна: остаёмся на эвристиках
        log.warning("уточнение типа сообщения моделью не удалось: %s: %s", type(exc).__name__, exc)
        intent.signals.append(f"модель недоступна ({type(exc).__name__}) — по эвристикам")
        return intent
    kinds = [k for k in res.get("intents", []) if k in INTENTS]
    if not kinds:
        return intent
    scores = dict(intent.scores)
    for i, k in enumerate(kinds):
        scores[k] = max(scores[k], CONFIDENT + 1 - i * 0.5)  # главный тип модели — лидер
    for k in INTENTS:
        if k not in kinds:
            scores[k] = min(scores[k], ISSUE_THRESHOLD - 0.5) if k == "issue" else scores[k] * 0.5
    reason = (res.get("reason") or "").strip()
    return Intent(scores, intent.signals + [f"модель: {', '.join(LABELS[k] for k in kinds)}"
                                            + (f" — {reason}" if reason else "")], "llm")


def classify(text: str, has_files: bool = False, settings=None, use_llm: bool = True) -> Intent:
    intent = heuristics(text, has_files)
    if use_llm and not intent.confident and settings is not None and settings.intent_llm \
            and settings.yc_api_key and settings.yc_folder_id:
        intent = refine_with_llm(text, intent, settings)
    return intent


# ---------- черновик обращения ----------

def md_objects(text: str) -> list[str]:
    """Объекты метаданных: «Вид.Имя» из текста ошибки и идентификаторы вроде КС_Гамма."""
    from copilot1c.ingest.entities import find_md_objects

    found = [f"{m.group(1)}.{m.group(2)}" for m in _MD_DOTTED.finditer(text)]
    named = {f.split(".", 1)[1] for f in found}
    for name in find_md_objects(text):
        if name not in _MODULE_NAMES and name not in named:
            found.append(name)
    return list(dict.fromkeys(found))


def _title(text: str) -> str:
    body = _GREETING.sub("", text.strip(), count=1)
    for ln in body.splitlines():
        ln = ln.strip()
        if not ln or _ERROR_LOCATION_RE.search(ln):
            continue
        sentence = re.split(r"(?<=[.!?])\s", ln, maxsplit=1)[0].strip(" .:;,-")
        if len(sentence) >= 8:
            return sentence if len(sentence) <= 120 else sentence[:117].rsplit(" ", 1)[0] + "…"
    first = body.strip().splitlines()[0] if body.strip() else "Обращение из чата"
    return first[:117] + ("…" if len(first) > 117 else "")


def _category(low: str) -> str:
    if re.search(r"прав\w*\s+доступ|нет\s+прав|недостаточно\s+прав|не\s+(?:видит|видн)\w*\s+(?:раздел|документ|отч)", low):
        return "access"
    if re.search(r"тормоз|медленн|долго\s+(?:провод|формир|открыва|загруж)|зависа|превышено\s+максимальное\s+время", low):
        return "performance"
    if re.search(r"доработ|добавить\s+(?:поле|реквизит|колонк|отч)|нужно,?\s+чтобы|хотим,?\s+чтобы|просят\s+добав", low):
        return "change"
    if re.search(r"\bнси\b|дубл|расхожд|не\s+сход|неверн\w*\s+(?:остат|цен|данн|сумм)|остатк", low):
        return "data"
    if re.search(r"^\s*(?:как|подскажите|можно\s+ли)\b", low) and not _ERROR_LOCATION_RE.search(low):
        return "consult"
    return "bug"


def _priority(low: str) -> str:
    if re.search(r"не\s+можем\s+(?:отгруж|продава|работа|закрыть)|остановлен|встал\w*\s+(?:работа|склад|отгрузк)|"
                 r"никто\s+не\s+может|у\s+всех\s+не|блокир\w*\s+работ|авари", low):
        return "critical"
    if re.search(r"срочно|\bсрочн|все\s+пользовател|массово|у\s+всех|не\s+провод|закрыти\w*\s+(?:месяц|период)", low):
        return "high"
    return "medium"


def issue_draft(text: str) -> dict[str, Any]:
    """Поля обращения по тексту сообщения (без сохранения)."""
    low = text.casefold()
    errors = error_lines(text)
    return {"title": _title(text), "description": text.strip(), "error_text": "\n".join(errors) or None,
            "objects": md_objects(text), "category": _category(low), "priority": _priority(low),
            "source": "chat"}
