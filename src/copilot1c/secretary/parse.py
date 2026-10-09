"""Разбор фраз секретаря правилами: место, рабочая сессия и перерыв, стоп, «где я», «сколько осталось».

Правила покрывают обычные короткие фразы и не ходят в модель: «я в Варшаве», «завтра поехал в Лондон»,
«вернулся в Варшаву», «работаем час», «перерыв 15 минут», «до 18:00 работаю», «стоп». Что правила не поняли,
секретарь отдаёт модели (secretary/service.py). Одна фраза может дать несколько команд: «я в Варшаве, работаем
час» — место и сессия.

Город здесь — как написано («Варшаве», «Варшаву»); именительный падеж, страну и часовой пояс находит
secretary/places.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, time, timedelta

# ---------- числа и длительности ----------

UNITS = {"ноль": 0, "один": 1, "одну": 1, "одна": 1, "одного": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
         "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9}
TEENS = {"десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14, "пятнадцать": 15,
         "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19}
TENS = {"двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60, "семьдесят": 70,
        "восемьдесят": 80, "девяносто": 90, "сто": 100}
HALF = {"полтора": 1.5, "полторы": 1.5, "пол": 0.5}
HOUR_UNITS = {"час", "часа", "часов", "часик", "часок", "часика", "ч"}
MINUTE_UNITS = {"минута", "минуты", "минут", "минуту", "минутку", "минутки", "минуток", "мин", "м"}
WHOLE = {"полчаса": 30, "полчасика": 30, "четверть": 15}  # «четверть часа» — 15, слово «часа» дальше не удваивает

MAX_MINUTES = 12 * 60

_TOKEN = re.compile(r"\d+(?:[.,]\d+)?|[a-zа-яё]+|:", re.IGNORECASE)


def _word_number(tokens: list[str], i: int) -> tuple[float | None, int]:
    """Число словами с позиции i: «сорок пять», «двадцать», «полтора». (значение, сколько токенов занято)."""
    t = tokens[i]
    if t in HALF:
        return HALF[t], 1
    if t in UNITS or t in TEENS:
        return float(UNITS.get(t, TEENS.get(t, 0))), 1
    if t in TENS:
        if i + 1 < len(tokens) and tokens[i + 1] in UNITS:
            return float(TENS[t] + UNITS[tokens[i + 1]]), 2
        return float(TENS[t]), 1
    return None, 0


def parse_duration(text: str) -> int | None:
    """Длительность в минутах: «час», «1 час», «полчаса», «полтора часа», «1 ч 20 мин», «45 минут», «1:30», «90м»,
    «сорок пять минут». Голое число без единиц: до 4 — часы, больше — минуты. None — длительности нет."""
    tokens = [t.lower() for t in _TOKEN.findall(text)]
    total = 0.0
    found = False
    pending: float | None = None
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in WHOLE:
            total += WHOLE[t]
            found = True
            pending = None
            if i + 1 < len(tokens) and tokens[i + 1] in HOUR_UNITS:  # «четверть часа»
                i += 1
            i += 1
            continue
        if t[0].isdigit():
            if i + 2 < len(tokens) and tokens[i + 1] == ":" and tokens[i + 2].isdigit():  # 1:30
                total += int(float(t.replace(",", "."))) * 60 + int(tokens[i + 2])
                found = True
                pending = None
                i += 3
                continue
            pending = float(t.replace(",", "."))
            i += 1
            continue
        num, used = _word_number(tokens, i)
        if used:
            pending = num
            i += used
            continue
        if t in HOUR_UNITS:
            total += (pending if pending is not None else 1) * 60
            found = True
            pending = None
        elif t in MINUTE_UNITS:
            if pending is not None or t not in ("м", "мин"):  # одинокое «м» без числа — не минута
                total += pending if pending is not None else 1
                found = True
            pending = None
        elif pending is not None and not found:
            # число, за которым не единица («работаем 45 сегодня») — решим в конце, если единиц не будет
            pass
        i += 1
    if not found and pending is not None:
        total, found = (pending * 60 if pending <= 4 else pending), True
    if not found:
        return None
    minutes = int(round(total))
    return minutes if 0 < minutes <= MAX_MINUTES else None


_UNTIL = re.compile(r"(?<![\wё])до\s+(\d{1,2})(?:[:.](\d{2}))?(?:\s*(?:ч|час\w*))?(?![\d.:])", re.IGNORECASE)


def parse_until(text: str) -> time | None:
    """«до 18:00», «до 18», «до 9.30» — время окончания (местное)."""
    m = _UNTIL.search(text)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    return time(h, mi) if h < 24 and mi < 60 else None


# ---------- даты ----------

WEEKDAYS = {"понедельник": 0, "вторник": 1, "среду": 2, "среда": 2, "четверг": 3, "пятницу": 4, "пятница": 4,
            "субботу": 5, "суббота": 5, "воскресенье": 6}
MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7, "августа": 8,
          "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
_DATE_NUM = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?![\d.])")
_DATE_WORD = re.compile(r"(?<!\d)(\d{1,2})\s+(" + "|".join(MONTHS) + r")(?:\s+(\d{4}))?", re.IGNORECASE)
_IN_DAYS = re.compile(r"через\s+(\d+|[а-яё]+)\s+(?:дн\w*|день)", re.IGNORECASE)


def parse_date(text: str, today: date) -> date | None:
    """Дата из фразы: сегодня, завтра, послезавтра, «в пятницу», «12 октября», «12.10», «через 3 дня». None — нет."""
    low = text.lower()
    if re.search(r"(?<![\wё])послезавтра(?![\wё])", low):
        return today + timedelta(days=2)
    if re.search(r"(?<![\wё])завтра(?![\wё])", low):
        return today + timedelta(days=1)
    if re.search(r"(?<![\wё])сегодня(?![\wё])", low):
        return today
    m = _IN_DAYS.search(low)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else int(_word_number([m.group(1)], 0)[0] or 0)
        if n:
            return today + timedelta(days=n)
    m = _DATE_WORD.search(low)
    if m:
        return _resolve_day(int(m.group(1)), MONTHS[m.group(2)], m.group(3), today)
    m = _DATE_NUM.search(low)
    if m and int(m.group(2)) <= 12:
        return _resolve_day(int(m.group(1)), int(m.group(2)), m.group(3), today)
    for word, wd in WEEKDAYS.items():
        if re.search(rf"(?<![\wё])(?:в|во|с)\s+{word}\w*", low):
            ahead = (wd - today.weekday()) % 7 or 7
            return today + timedelta(days=ahead)
    return None


def _resolve_day(day: int, month: int, year: str | None, today: date) -> date | None:
    try:
        if year:
            y = int(year)
            return date(y + 2000 if y < 100 else y, month, day)
        d = date(today.year, month, day)
        return d if d >= today - timedelta(days=1) else date(today.year + 1, month, day)  # «5 января» в октябре
    except ValueError:
        return None


# ---------- место ----------

# Глаголы и слова, после которых «в <город>» — это место. Прибытие (я, вернулся, прилетел) и отъезд (поехал, лечу)
# различаются только для ответа: момент записи определяет дата во фразе («завтра»), без даты — сейчас.
_ARRIVE = (r"я|мы|сейчас|нахожусь|находимся|живу|остановил\w*|прилетел\w*|прилетаю|приехал\w*|приезжаю|прибыл\w*|"
           r"вернул\w*|возвраща\w*|верн[уё]\w*|переехал\w*|переместил\w*|буду|будем|теперь|снова|опять")
_DEPART = (r"поехал\w*|поеду|поедем|еду|едем|лечу|летим|полечу|полетим|полетел\w*|улета\w*|улечу|уезжа\w*|уеду|"
           r"выезжа\w*|вылета\w*|переезжа\w*|перееду|отправля\w*|отправлюсь|отбываю|отбыл\w*|двинул\w*|направля\w*")
_VERB = re.compile(rf"(?<![\wё-])(?P<arrive>{_ARRIVE})(?![\wё-])|(?<![\wё-])(?P<depart>{_DEPART})(?![\wё-])",
                   re.IGNORECASE)
_IN = re.compile(r"(?<![\wё-])(?:в|во)\s+(?P<place>[a-zа-яё][\wё-]*(?:\s+[a-zа-яё][\wё-]*){0,2})", re.IGNORECASE)
_STOP_WORDS = {
    "завтра", "сегодня", "послезавтра", "утром", "вечером", "днём", "днем", "ночью", "на", "по", "с", "со", "до",
    "и", "а", "но", "к", "ко", "через", "в", "во", "за", "из", "от", "работаем", "работаю", "поработаем", "надолго",
    "неделю", "недели", "дня", "дней", "командировку", "командировке", "был", "была", "буду", "будем", "снова",
    "опять", "обратно", "тоже", "уже", "сейчас", "пока", "ещё", "еще", "отдыхаем", "перерыв", "час", "часа",
    "минут", "числа", "сюда", "туда", "where", "это", "этот", "там", "тут", "теперь", "рейсом", "поездом",
}
# Даты после «в»: «в понедельник», «в 10:00» — не место
_NOT_PLACE_FIRST = set(WEEKDAYS) | set(MONTHS) | {"понедельника", "вторника", "среды", "четверга", "пятницы",
                                                  "субботы", "воскресенья", "час", "часов", "минут", "течение"}


@dataclass
class Command:
    action: str                       # place | work | rest | stop | status | where | history | cancel_trip | help
    place: str | None = None          # как написано во фразе
    when: date | None = None          # дата переезда (None — сейчас)
    minutes: int | None = None
    until: time | None = None
    direction: str | None = None      # arrive | depart
    notes: list[str] = field(default_factory=list)
    # книги (secretary/books.py)
    book_no: int | None = None        # № книги у человека
    book_ref: str | None = None       # книга названием, если номера нет
    page: int | None = None
    author: str | None = None
    title: str | None = None
    total_pages: int | None = None
    toc: list[tuple[str, int]] = field(default_factory=list)  # оглавление: (раздел, страница)


def _place_after(text: str, start: int) -> str | None:
    pos = start
    while (m := _IN.search(text, pos)) is not None:
        pos = m.start("place")  # следующий поиск — внутри этого совпадения: «в понедельник в Лондоне»
        words = m.group("place").split()
        if words[0].lower() in _NOT_PLACE_FIRST:
            continue
        keep = []
        for w in words:
            if w.lower() in _STOP_WORDS:
                break
            keep.append(w.strip(".,;:!?»«\"'"))
        if keep and keep[0]:
            return " ".join(keep)
    return None


def find_place(text: str) -> tuple[str, str] | None:
    """(место как написано, arrive|depart) или None. «Я в Варшаве» → («Варшаве», arrive)."""
    for v in _VERB.finditer(text):
        place = _place_after(text[: v.end() + 80], v.end())
        if place:
            return place, ("depart" if v.group("depart") else "arrive")
    m = re.match(r"\s*(?:в|во)\s+", text, re.IGNORECASE)  # фраза из одного «в Лондоне»
    if m:
        place = _place_after(text, 0)
        if place:
            return place, "arrive"
    return None


# ---------- команды ----------

_WORK = re.compile(r"(?<![\wё])(?:работа\w*|поработа\w*|рабоч\w*|фокус\w*|сосредоточ\w*|помидор\w*|pomodoro)",
                   re.IGNORECASE)
_REST = re.compile(r"(?<![\wё])(?:отдых\w*|отдохн\w*|перерыв\w*|пауз\w*|передышк\w*|перекур\w*)", re.IGNORECASE)
_STOP = re.compile(r"(?<![\wё])(?:стоп|хватит|закончил\w*|заканчива\w*|заверш\w*|останов\w*|прерв\w*|прерыва\w*|"
                   r"сбрось|отмени\s+(?:таймер|сессию|перерыв))(?![\wё])", re.IGNORECASE)
_CANCEL_TRIP = re.compile(r"(?<![\wё])(?:не\s+(?:еду|поеду|лечу|полечу|уезжаю|улетаю)|отмен\w*\s+(?:поездк\w*|"
                          r"переезд\w*|командировк\w*|перелёт\w*|перелет\w*)|поездк\w*\s+отмен\w*)", re.IGNORECASE)
_STATUS = re.compile(r"сколько\s+(?:ещё\s+|еще\s+)?осталось|статус|как\s+таймер|что\s+с\s+таймером|"
                     r"идёт\s+ли|сколько\s+(?:я\s+)?(?:уже\s+)?работа", re.IGNORECASE)
_WHERE = re.compile(r"где\s+я(?!\s+был)|котор\w*\s+час|сколько\s+времени|местное\s+время|погод\w*|"
                    r"(?<![\wё])время(?![\wё])", re.IGNORECASE)
_HISTORY = re.compile(r"где\s+я\s+был\w*|истори\w*\s+(?:поездок|перемещений|мест)|мои\s+поездки|маршрут|"
                      r"куда\s+я\s+(?:еду|собираюсь)|план\w*\s+поездок", re.IGNORECASE)
_HELP = re.compile(r"^\s*(?:помощь|help|что\s+ты\s+умеешь|\?)\s*$", re.IGNORECASE)


_SKY_TARGET = {"stars": re.compile(r"звёзд\w*|звезд\w*", re.IGNORECASE),
               "planets": re.compile(r"планет\w*", re.IGNORECASE),
               "astro": re.compile(r"(?<![\wё])неб\w*|астроном\w*|восход\w*\s+и\s+заход\w*|кульминац\w*", re.IGNORECASE)}
_SKY_OFF = re.compile(r"выключ\w*|отключ\w*|не\s+присыла\w*|не\s+надо|(?<![\wё])без(?![\wё])|хватит|убер\w*|"
                      r"(?<![\wё])выкл", re.IGNORECASE)
_SKY_ON = re.compile(r"(?<![\wё])включ\w*|присылай\w*|верни\w*|(?<![\wё])вкл(?![\wё])", re.IGNORECASE)


def _sky_setting(t: str) -> Command | None:
    """«выключи небо», «без звёзд», «включи планеты» — настройки сообщений о небе."""
    target = next((k for k, rx in _SKY_TARGET.items() if rx.search(t)), None)
    if target is None:
        return None
    if _SKY_OFF.search(t):
        return Command("sky_set", notes=[target, "off"])
    if _SKY_ON.search(t):
        return Command("sky_set", notes=[target, "on"])
    return None


def parse(text: str, today: date) -> list[Command]:
    """Команды из фразы в порядке выполнения; пустой список — правила не поняли (дальше — модель)."""
    t = " ".join((text or "").split())
    if not t:
        return []
    if _HELP.search(t):
        return [Command("help")]
    sky = _sky_setting(t)
    if sky is not None:
        return [sky]
    from copilot1c.secretary.books import parse_book

    # исходный текст: оглавление — по строкам; книги — раньше остальных («закончил книгу» — не «стоп»)
    book = parse_book(text)
    if book is not None:
        return [book]
    if _HISTORY.search(t):
        return [Command("history")]
    if _CANCEL_TRIP.search(t):
        return [Command("cancel_trip")]
    if _STATUS.search(t) and parse_duration(t) is None and parse_until(t) is None:
        return [Command("status")]  # «сколько я уже работаю» — вопрос, а не новая сессия
    out: list[Command] = []
    place = find_place(t)
    if place:
        out.append(Command("place", place=place[0], direction=place[1], when=parse_date(t, today)))
    work, rest = _WORK.search(t), _REST.search(t)
    kind = None
    if work and rest:
        kind = "work" if work.start() < rest.start() else "rest"
    elif work or rest:
        kind = "work" if work else "rest"
    if kind:
        until = parse_until(t)
        minutes = None if until else parse_duration(_without_place(t, place))
        if until or minutes:
            out.append(Command(kind, minutes=minutes, until=until))
        elif _STOP.search(t):
            out.append(Command("stop"))
        elif not out:
            out.append(Command(kind))  # «работаем» без срока — спросим, сколько
    elif _STOP.search(t) and not out:
        out.append(Command("stop"))
    if not out and _STATUS.search(t):
        out.append(Command("status"))
    if not out and _WHERE.search(t):
        out.append(Command("where"))
    return out


def _without_place(text: str, place: tuple[str, str] | None) -> str:
    """Фраза без названия города — «Пятигорск» и т. п. не должны давать цифры длительности."""
    return text.replace(place[0], " ") if place else text
