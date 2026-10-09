"""Книги в секретаре: несколько книг параллельно, у каждой — номер у человека и журнал страниц.

Фразы (правила; непонятое — модель, см. service.model_parse):
  «Зарегистрировать для чтения книгу Л.Н.Толстой "Война и Мир"» → книга № 1 — Л.Н. Толстой «Война и Мир»;
      «… 1300 страниц» в той же фразе — объём книги;
  «Читаю книгу № 1, текущая страница 70», «книга 2 стр. 154», «Война и мир — страница 85», «прочитал до 120
      страницы в книге 2» → запись: дата и время, страница, город (местное время по поясу места);
      единственная читаемая книга — номер можно не называть («страница 90»);
  «В книге 1 всего 1300 страниц» — объём; «дочитал книгу 1», «отложил книгу 2», «удали книгу 3»;
  «мои книги», «что читаю» — список.
Таблица и история — GET /secretary/books, /secretary/books/{id}; в вебе — страница «Книги».
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from copilot1c.secretary.parse import Command

_BOOK_WORD = r"(?:книг\w*|кн\.)"
_REF = re.compile(rf"{_BOOK_WORD}\s*(?:номер|номером|под\s+номером|№|#|n|no\.?)?\s*(\d{{1,4}})(?!\d)|"
                  r"(?:№|#)\s*(\d{1,4})(?!\d)", re.IGNORECASE)
_PAGE = re.compile(r"(?:страниц\w*|стр\.?|с\.)\s*(?:№\s*)?(\d{1,5})(?!\d)", re.IGNORECASE)
_PAGE_AFTER = re.compile(r"(?<![\d№#])(\d{1,5})\s*(?:-?(?:я|ю|й|ой))?\s*(?:страниц\w*|стр\.?)(?![\wё])", re.IGNORECASE)
_UNTIL_PAGE = re.compile(r"(?:прочит\w*|дочит\w*|читаю|остановил\w*|закончил\w*)\s+(?:\w+\s+){0,2}?до\s+(\d{1,5})",
                         re.IGNORECASE)
_TOTAL = re.compile(r"(?:всего|объ[её]м\w*|в\s+ней|в\s+книге\s+(?:№\s*)?\d{1,4})\s*[:\-—]?\s*(\d{1,5})\s*(?:стр\w*|с\.)",
                    re.IGNORECASE)
_PAGES_IN_ADD = re.compile(r"(?<![\d№#])(\d{1,5})\s*(?:страниц\w*|стр\.?)(?![\wё])", re.IGNORECASE)

_ADD = re.compile(rf"(?:зарегистр\w*|регистр\w*|добав\w*|запиш\w*|заведи|заведём|заведем|нов\w*|начинаю\s+читать|"
                  rf"начал\w*\s+читать|буду\s+читать)\s+(?:для\s+чтения\s+)?(?:новую\s+)?{_BOOK_WORD}\s*"
                  rf"(?:для\s+чтения)?\s*[:\-—]?\s*(?P<rest>.+)$", re.IGNORECASE | re.DOTALL)
_DONE = re.compile(rf"(?:дочитал\w*|прочитал\w*\s+(?:всю\s+|целиком\s+)?{_BOOK_WORD}|закончил\w*\s+(?:читать\s+)?"
                   rf"{_BOOK_WORD}|{_BOOK_WORD}[^.!?]*\s(?:прочитан\w*|дочитан\w*|законч\w*))", re.IGNORECASE)
_PAUSE = re.compile(rf"(?:отлож\w*|бросил\w*|приостанов\w*)\s+(?:\w+\s+)?{_BOOK_WORD}|пауза\s+(?:в|с)\s+{_BOOK_WORD}|"
                    rf"{_BOOK_WORD}[^.!?]*\s(?:отложен\w*|на\s+паузе)", re.IGNORECASE)
_DELETE = re.compile(rf"(?:удал\w*|убер\w*|вычеркн\w*)\s+{_BOOK_WORD}", re.IGNORECASE)
_LIST = re.compile(r"(?:мои\s+книги|что\s+(?:я\s+)?(?:сейчас\s+)?читаю|список\s+книг|какие\s+книги|^\s*книги\s*\??\s*$|"
                   r"книги\s+(?:в\s+работе|читаю))", re.IGNORECASE)
_READ_WORD = re.compile(rf"чита\w*|прочит\w*|дочит\w*|{_BOOK_WORD}|остановил\w*", re.IGNORECASE)

_QUOTED = re.compile(r"[«\"“„']\s*(?P<t>[^«»\"“”„']+?)\s*[»\"”“']")
_INITIALS = re.compile(r"(\b[А-ЯЁA-Z]\.)(?=[А-ЯЁA-Z][а-яёa-z])")


def tidy_author(s: str) -> str:
    """«Л.Н.Толстой» → «Л.Н. Толстой»; «автор:» и лишние знаки — прочь."""
    s = re.sub(r"^\s*(?:автор\w*|писател\w*)\s*[:\-—]?\s*", "", s.strip(), flags=re.IGNORECASE)
    s = _INITIALS.sub(r"\1 ", s)
    return " ".join(s.strip(" ,.;:—-–").split())


def split_author_title(rest: str) -> tuple[str, str, int | None]:
    """(автор, название, страниц). Название — в кавычках; без кавычек — после «—», «-» или запятой."""
    rest = " ".join(rest.split()).strip(" .")
    pages = None
    m = _QUOTED.search(rest)
    if m:
        title = m.group("t").strip()
        author = rest[: m.start()]
        tail = rest[m.end():]
        pm = _PAGES_IN_ADD.search(tail)
        if pm:
            pages = int(pm.group(1))
        if not author.strip(" ,.;:—-–") and tail:  # «"Война и мир" Толстого»
            author = re.sub(_PAGES_IN_ADD, "", tail)
        return tidy_author(author), title, pages
    pm = _PAGES_IN_ADD.search(rest)
    if pm:
        pages = int(pm.group(1))
        rest = (rest[: pm.start()] + rest[pm.end():]).strip(" ,.;:—-–")
    for sep in (" — ", " – ", " - ", ", ", ": "):
        if sep in rest:
            a, t = rest.split(sep, 1)
            return tidy_author(a), t.strip(" ,.;:—-–"), pages
    return "", rest.strip(" ,.;:—-–"), pages


def _ref(text: str) -> int | None:
    m = _REF.search(text)
    return int(m.group(1) or m.group(2)) if m else None


def _page(text: str) -> int | None:
    m = _PAGE.search(text) or _UNTIL_PAGE.search(text)
    if m:
        return int(m.group(1))
    m = _PAGE_AFTER.search(text)
    return int(m.group(1)) if m else None


def _title_ref(text: str) -> str | None:
    """Книга названием без номера: «Война и мир — страница 85», «в "Войне и мире" стр. 90»."""
    m = _QUOTED.search(text)
    if m:
        return m.group("t").strip()
    m = re.match(r"\s*(?P<t>[^,—–\-:]{3,80}?)\s*[,—–\-:]\s*(?:страниц\w*|стр\.?|с\.)\s*\d", text, re.IGNORECASE)
    return m.group("t").strip() if m else None


# ---------- оглавление ----------
# «Книга 1, стр. 5, Предисловие» — строка оглавления (после страницы — название раздела), а не отметка чтения.
# Отметка — когда вместо названия слова чтения: «Книга 1, остановился на стр. 15», «читаю книгу 1, стр. 70».
# Оглавление можно вставить списком: первая строка «Книга 1» (или «Книга 1, оглавление:», или сама строка
# оглавления), дальше «Предисловие<Tab>стр. 5», «1. Начало стр. 10», «Установка ……… 39».

_PROGRESS_WORDS = re.compile(r"остановил\w*|чита\w*|прочит\w*|дочит\w*|текущ\w*|сейчас|закончил\w*|дошёл|дошел|"
                             r"дошла|я\s+на|на\s*$|^(?:сегодня|вчера|утром|днём|днем|вечером|ночью)$", re.IGNORECASE)
# Перед «Книга N» в строке оглавления допускается только пояснение: «Запиши оглавление:», «Раздел:», «Добавь главу —»
_TOC_PREFIX = re.compile(r"^(?:\w+\s+){0,2}(?:оглавлени\w*|содержани\w*|раздел\w*|глав\w*)\s*[:\-—–]?\s*$", re.IGNORECASE)
_TOC_WORD = re.compile(r"оглавлени\w*|содержани\w*|разделы|главы", re.IGNORECASE)
_PAGE_WORD = r"(?:стр\.?|страниц\w*|с\.)"
_TOC_LINE = re.compile(rf"^\s*(?P<title>\S.*?)[\s\t.…·_—–\-]*{_PAGE_WORD}?\s*(?P<page>\d{{1,5}})\s*$", re.IGNORECASE)
_TOC_LINE_PAGE_FIRST = re.compile(rf"^\s*{_PAGE_WORD}\s*(?P<page>\d{{1,5}})\s*[,.:;—–\-\t]+\s*(?P<title>\S.*?)\s*$",
                                  re.IGNORECASE)
_REF_ONLY = re.compile(rf"^\s*{_BOOK_WORD}\s*(?:номер|№|#)?\s*\d{{1,4}}\s*[,.:;—–\-]*\s*(?:оглавлени\w*|содержани\w*)?"
                       r"\s*[:.]?\s*$", re.IGNORECASE)


def _clean_title(s: str) -> str:
    return " ".join(s.strip(" \t,.;:—–-…·_").split())


def toc_entry(line: str) -> tuple[str, int] | None:
    """«Книга 1, стр. 5, Предисловие» → («Предисловие», 5); строка-отметка («остановился на стр. 15») → None."""
    t = " ".join(line.split())
    ref = _REF.search(t)
    if not ref:
        return None
    before = t[: ref.start()].strip()
    if before and not _TOC_PREFIX.match(before):  # «вернулся к книге 2, страница 160» — отметка, не оглавление
        return None
    rest = t[ref.end():].strip(" ,.;:—–-")
    rest = re.sub(r"^(?:оглавлени\w*|содержани\w*)\s*[:\-—–]?\s*", "", rest, flags=re.IGNORECASE)  # «Книга 1, оглавление: …»
    # «стр. 5, Предисловие», «стр.5 Предисловие» (без запятой — название начинается с буквы или «1.»)
    m = _TOC_LINE_PAGE_FIRST.match(rest) or re.match(
        rf"^{_PAGE_WORD}\s*(?P<page>\d{{1,5}})\s+(?P<title>(?:\d{{1,3}}[.)]\s*)?[A-Za-zА-Яа-яЁё«\"„].*?)\s*$", rest,
        re.IGNORECASE)
    if m:
        title, page = m.group("title"), int(m.group("page"))
    else:
        m = re.match(rf"^(?P<title>.+?)\s*[,.:;—–\-]\s*{_PAGE_WORD}\s*(?P<page>\d{{1,5}})\s*$", rest, re.IGNORECASE)
        if not m:
            return None
        title, page = m.group("title"), int(m.group("page"))
    title = _clean_title(title)
    if not title or _PROGRESS_WORDS.search(title) or not re.search(r"[A-Za-zА-Яа-яЁё]", title):
        return None
    return title, page


def parse_toc_lines(lines: list[str]) -> list[tuple[str, int]]:
    out = []
    for ln in lines:
        if not ln.strip():
            continue
        m = _TOC_LINE_PAGE_FIRST.match(ln) or _TOC_LINE.match(ln.replace("\t", " \t "))
        if not m:
            return []  # не похоже на оглавление целиком — не гадаем
        title = _clean_title(m.group("title"))
        if not title or not re.search(r"[A-Za-zА-Яа-яЁё]", title):
            return []
        out.append((title, int(m.group("page"))))
    return out


def _parse_toc_block(text: str) -> Command | None:
    lines = [ln for ln in text.replace("\r", "").split("\n") if ln.strip()]
    if len(lines) < 2:
        return None
    first = lines[0]
    no = _ref(first)
    if no is None:
        return None
    entries: list[tuple[str, int]] = []
    head = toc_entry(first)
    if head:
        entries.append(head)
    elif not _REF_ONLY.match(first):
        return None
    rest = parse_toc_lines(lines[1:])
    if not rest:
        return None
    return Command("book_toc", book_no=no, toc=entries + rest)


def parse_book(text: str) -> Command | None:
    """Команда про книги или None — фраза не о книгах."""
    block = _parse_toc_block(text or "")
    if block is not None:
        return block
    t = " ".join((text or "").split())
    m = _ADD.search(t)
    if m and not _ref(m.group("rest")[:12]):  # «добавь книге 1 страницу…» — не регистрация
        author, title, pages = split_author_title(m.group("rest"))
        if title:
            return Command("book_add", author=author, title=title, total_pages=pages)
    if _LIST.search(t):
        return Command("books")
    if _DELETE.search(t):
        return Command("book_delete", book_no=_ref(t), book_ref=None if _ref(t) else _title_ref(t))
    if _DONE.search(t):
        return Command("book_done", book_no=_ref(t), book_ref=None if _ref(t) else _title_ref(t))
    if _PAUSE.search(t):
        return Command("book_pause", book_no=_ref(t), book_ref=None if _ref(t) else _title_ref(t))
    # «удали последнюю отметку книги 1», «удали (все) отметки стр. 5 в книге 1»
    if re.search(r"(?:удал\w*|убер\w*|отмен\w*|сотри)\s+(?:вс[еёи]\s+)?(?:последн\w*\s+|ошибочн\w*\s+)?"
                 r"(?:отметк\w*|запис\w*)", t, re.IGNORECASE):
        return Command("book_mark_delete", book_no=_ref(t), page=_page(t),
                       notes=["all"] if re.search(r"(?<![\wё])вс[еёи]\w*", t, re.IGNORECASE) else [])
    if _TOC_WORD.search(t) and _ref(t) is not None and _page(t) is None:  # «оглавление книги 1»
        return Command("book_toc_show", book_no=_ref(t))
    m = re.search(rf"(?:удал\w*|убер\w*)\s+(?:из\s+оглавления\s+)?(?:раздел\w*|глав\w*)\s+[«\"„]?(?P<t>[^»\"“]+?)[»\"“]?\s*"
                  rf"(?:(?:из|в|у)\s+{_BOOK_WORD}\s*(?:№\s*)?\d+)?\s*$", t, re.IGNORECASE)
    if m and _ref(t) is not None:
        return Command("book_toc_delete", book_no=_ref(t), title=_clean_title(re.sub(rf"(?:из|в|у)\s+{_BOOK_WORD}.*$", "",
                                                                                      m.group("t"))))
    entry = toc_entry(t)
    if entry is not None:
        return Command("book_toc", book_no=_ref(t), toc=[entry])
    total = _TOTAL.search(t)
    if total:  # «в книге 1 всего 1300 страниц», «объём 1300 стр.», «в книге 2 640 страниц»
        return Command("book_total", book_no=_ref(t), total_pages=int(total.group(1)))
    page = _page(t)
    bare = re.match(r"\s*(?:страниц\w*|стр\.?|с\.)\s*\d+\s*[.!]?\s*$", t, re.IGNORECASE)  # «страница 90»
    if page is not None and (_READ_WORD.search(t + " ") or _ref(t) or bare):
        no = _ref(t)
        return Command("book_page", book_no=no, page=page, book_ref=None if no else _title_ref(t))
    if page is not None and _title_ref(t):
        return Command("book_page", page=page, book_ref=_title_ref(t))
    return None


# ---------- статистика для таблицы ----------

def section_at(toc: list[dict], page: int | None) -> dict | None:
    """Раздел, в котором страница: последний раздел оглавления, начавшийся не позже неё."""
    if page is None:
        return None
    cur = None
    for e in sorted(toc, key=lambda e: (e["page"], e["id"])):
        if e["page"] <= page:
            cur = e
    return cur


def book_label(b: dict) -> str:
    who = f"{b['author']} " if b.get("author") else ""
    return f"№ {b['num']} — {who}«{b['title']}»"


def stats(book: dict, log: list[dict], now: datetime) -> dict:
    """Текущая страница, прогресс, за 7 дней, страниц в день. log — записи книги по времени (старые первыми)."""
    last = log[-1] if log else None
    week_ago = now - timedelta(days=7)
    before = [r for r in log if r["at"] <= week_ago]
    base = before[-1]["page"] if before else 0
    week = max(0, (last["page"] if last else 0) - base) if any(r["at"] > week_ago for r in log) else 0
    days = max(1.0, (now - (log[0]["at"] if log else book["created_at"])).total_seconds() / 86400) if log else None
    total = book.get("total_pages")
    page = last["page"] if last else None
    return {
        "page": page,
        "percent": round(min(page / total, 1) * 100) if page and total else None,
        "left": max(total - page, 0) if page is not None and total else None,
        "week_pages": week,
        "per_day": round(page / days, 1) if page and days else None,
        "entries": len(log),
        "started_at": log[0]["at"] if log else None,
        "last_at": last["at"] if last else None,
        "last_city": last.get("city") if last else None,
        "last_tz": last.get("tz") if last else None,
    }
