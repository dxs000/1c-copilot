"""Секретарь: фраза → действие → ответ; состояние для панели «Сейчас»; сообщение об окончании сессии.

Фразы сначала разбирают правила (parse.py), непонятое — модель COPILOT_MODEL_BATCH (если COPILOT_SECRETARY_LLM).
Город, пояс — places.py; погода — weather.py (Yandex Search API + модель). Все записи — store.py.
Время — всегда по часовому поясу текущего места; место не указано — по поясу сервера.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from copilot1c.secretary.books import book_label, section_at, stats
from copilot1c.secretary.parse import Command, parse
from copilot1c.secretary.places import NotAPlace, resolve
from copilot1c.secretary.store import SecretaryStore

WEEKDAY_NAMES = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTH_NAMES = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
               "ноября", "декабря"]
LATE_SECONDS = 90  # сообщение пришло позже окончания больше чем на столько — сказать об этом

HELP = ("Я веду ваш распорядок.\n"
        "• Место: «я в Варшаве», «завтра еду в Лондон», «вернулся в Варшаву», «не еду» (отменить поездку), "
        "«где я был».\n"
        "• Сессии: «работаем час», «работаю до 18:00», «перерыв 15 минут», «сколько осталось», «стоп».\n"
        "• «Где я» — место, местное время и погода сейчас.\n"
        "• Книги: «зарегистрируй книгу Л.Н. Толстой „Война и мир“», «читаю книгу № 1, страница 70», «в книге 1 "
        "всего 1300 страниц», «дочитал книгу 1», «отложил книгу 2», «мои книги».\n"
        "• Оглавление: «Книга 1, стр. 5, Предисловие» или списком; "
        "отметка, где остановились: «Книга 1, остановился на стр. 15»; ошибочную — «удали последнюю отметку книги 1». "
        "Таблица — на странице «Книги».\n"
        "По окончании сессии напишу точное местное время и погоду там, где вы находитесь.")


def fmt_minutes(m: int) -> str:
    h, mi = divmod(max(int(m), 0), 60)
    if h and mi:
        return f"{h} ч {mi} мин"
    return f"{h} ч" if h else f"{mi} мин"


def utc_offset(dt: datetime) -> str:
    z = dt.strftime("%z") or "+0000"
    return f"UTC{z[0]}{z[1:3]}:{z[3:5]}"


def fmt_day(d: date) -> str:
    return f"{WEEKDAY_NAMES[d.weekday()]}, {d.day} {MONTH_NAMES[d.month - 1]}"


def fmt_from(d: date) -> str:
    """«10 октября (суббота)» — для «с …»."""
    return f"{d.day} {MONTH_NAMES[d.month - 1]} ({WEEKDAY_NAMES[d.weekday()]})"


def move_day(p: dict) -> date:
    """День переезда: как его назвали; иначе — дата начала по поясу города."""
    return p.get("on_date") or p["effective_at"].astimezone(ZoneInfo(p["tz"])).date()


def fmt_local(dt: datetime, seconds: bool = False) -> str:
    """«15:32, пятница, 9 октября (UTC+02:00)»."""
    return f"{dt.strftime('%H:%M:%S' if seconds else '%H:%M')}, {fmt_day(dt.date())} ({utc_offset(dt)})"


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime | date) else v


class Secretary:
    def __init__(self, conn, settings, now: Callable[[], datetime] | None = None,
                 weather: Callable[[str, str], dict] | None = None, llm_parse: Callable[[str, date], list] | None = None):
        self.store = SecretaryStore(conn)
        self.s = settings
        self._now = now or (lambda: datetime.now(UTC))
        self._weather = weather
        self._llm_parse = llm_parse

    # ---------- время и место ----------

    def now(self) -> datetime:
        return self._now().astimezone(UTC)

    def host_tz(self) -> tzinfo:
        name = getattr(self.s, "secretary_default_tz", "") or ""
        return ZoneInfo(name) if name else datetime.now().astimezone().tzinfo

    def place(self, person: str, at: datetime | None = None) -> dict | None:
        return self.store.place_at(person, at or self.now())

    def tz_of(self, place: dict | None) -> tzinfo:
        return ZoneInfo(place["tz"]) if place else self.host_tz()

    def local(self, person: str, at: datetime | None = None) -> tuple[datetime, dict | None]:
        at = at or self.now()
        p = self.place(person, at)
        return at.astimezone(self.tz_of(p)), p

    def weather(self, place: dict | None) -> dict:
        if place is None:
            return {"ok": False, "reason": "место не указано"}
        if self._weather is not None:
            return self._weather(place["city"], place["country"])
        from copilot1c.secretary.weather import current_weather

        return current_weather(place["city"], place["country"], self.s)

    # ---------- фраза ----------

    def say(self, person: str, text: str) -> dict[str, Any]:
        person = (person or "").strip()
        local_now, _ = self.local(person)
        commands = parse(text, local_now.date())
        via = "rules"
        if not commands and getattr(self.s, "secretary_llm", True):
            commands = self._model_commands(text, local_now.date())
            via = "model" if commands else via
        if not commands:
            return {"reply": "Не понял. " + HELP, "actions": [], "via": via, "state": self.state(person)}
        replies, actions = [], []
        for cmd in commands:
            reply, action = self._run(person, cmd, text)
            replies.append(reply)
            actions.append(action)
        return {"reply": "\n\n".join(replies), "actions": actions, "via": via, "state": self.state(person)}

    def _model_commands(self, text: str, today: date) -> list[Command]:
        if self._llm_parse is not None:
            return self._llm_parse(text, today)
        if not (self.s.yc_api_key and self.s.yc_folder_id):
            return []
        try:
            return model_parse(text, today, self.s)
        except Exception:  # noqa: BLE001 — модель недоступна: ответим подсказкой
            import logging

            logging.getLogger("copilot1c.secretary").exception("разбор фразы моделью")
            return []

    def _run(self, person: str, cmd: Command, said: str) -> tuple[str, dict]:
        fn = getattr(self, f"_do_{cmd.action}", None)
        if fn is None:
            return HELP, {"action": "help"}
        return fn(person, cmd, said)

    def _do_help(self, person, cmd, said):
        return HELP, {"action": "help"}

    def _do_place(self, person: str, cmd: Command, said: str) -> tuple[str, dict]:
        try:
            place = resolve(cmd.place or "", self.s, cache=self.store)
        except NotAPlace as exc:
            return f"Не записал место: {exc}. Назовите город — например, «я в Варшаве».", \
                {"action": "place", "ok": False, "said": cmd.place}
        now = self.now()
        current = self.place(person, now)
        tz = ZoneInfo(place.tz)
        local_today = now.astimezone(self.tz_of(current)).date()
        if cmd.when and cmd.when > local_today:
            # переезд в будущем: с начала того дня по часовому поясу, где человек будет до отъезда
            start = datetime.combine(cmd.when, time(0, 0), tzinfo=self.tz_of(current))
            row = self.store.add_place(person, place, start.astimezone(UTC), cmd.direction or "depart", said,
                                       on_date=cmd.when)
            here = f"Сейчас вы в городе {current['city']}." if current else "Где вы сейчас, не записано."
            return (f"Записал: с {fmt_from(cmd.when)} — {place.city} ({place.country}, "
                    f"{utc_offset(datetime.combine(cmd.when, time(12), tzinfo=tz))}). {here}",
                    {"action": "place", "ok": True, "planned": True, "place": _place_out(row)})
        # сейчас: та же запланированная поездка состоялась раньше — убрать план
        for up in self.store.upcoming(person, now):
            if up["city"] == place.city:
                self.store._rows("UPDATE sec_places SET cancelled = true WHERE id = %s", (up["id"],))
        if current and current["city"] == place.city:
            local = now.astimezone(tz)
            return (f"Уже записано: вы в городе {place.city} с {_since(current, tz)}. Местное время {fmt_local(local)}."
                    + self._plans_hint(person, now), {"action": "place", "ok": True, "unchanged": True,
                                                       "place": _place_out(current)})
        row = self.store.add_place(person, place, now, cmd.direction or "arrive", said)
        local = now.astimezone(tz)
        was = f" До этого: {current['city']}." if current else ""
        return (f"Записал: вы в городе {place.city} ({place.country}). Местное время {fmt_local(local)}.{was}"
                + self._plans_hint(person, now), {"action": "place", "ok": True, "place": _place_out(row)})

    def _plans_hint(self, person: str, now: datetime) -> str:
        ups = self.store.upcoming(person, now)
        if not ups:
            return ""
        items = "; ".join(f"с {fmt_from(move_day(u))} — {u['city']}"
                          for u in ups[:3])
        return f"\nЗапланировано: {items}. Отменить — «не еду»."

    def _do_cancel_trip(self, person, cmd, said):
        rows = self.store.cancel_upcoming(person, self.now())
        if not rows:
            return "Запланированных поездок нет.", {"action": "cancel_trip", "cancelled": 0}
        return "Отменил: " + "; ".join(r["city"] for r in rows) + ".", {"action": "cancel_trip", "cancelled": len(rows)}

    def _do_history(self, person, cmd, said):
        now = self.now()
        rows = self.store.history(person, 30)
        if not rows:
            return "Мест пока нет. Скажите, где вы: «я в Варшаве».", {"action": "history"}
        lines = []
        for r in rows:
            mark = " (план)" if r["effective_at"] > now else ""
            when = (r["on_date"].strftime("%d.%m.%Y") if r.get("on_date")  # переезд «на день» — без часов
                    else r["effective_at"].astimezone(ZoneInfo(r["tz"])).strftime("%d.%m.%Y %H:%M"))
            lines.append(f"• {when} — {r['city']}{mark}")
        return "Места (новые сверху):\n" + "\n".join(lines[:15]), {"action": "history"}

    def _do_work(self, person, cmd, said):
        return self._start(person, "work", cmd, said)

    def _do_rest(self, person, cmd, said):
        return self._start(person, "rest", cmd, said)

    def _start(self, person: str, kind: str, cmd: Command, said: str) -> tuple[str, dict]:
        now = self.now()
        local, place = self.local(person, now)
        what = "работаем" if kind == "work" else "отдыхаем"
        minutes = cmd.minutes
        if cmd.until is not None:
            end_local = datetime.combine(local.date(), cmd.until, tzinfo=local.tzinfo)
            if end_local <= local:
                return (f"{cmd.until.strftime('%H:%M')} по местному времени уже прошло (сейчас {local.strftime('%H:%M')}).",
                        {"action": kind, "ok": False})
            minutes = max(1, int(round((end_local - local).total_seconds() / 60)))
        if not minutes:
            return (f"Сколько {what}? Например: «{'работаем час' if kind == 'work' else 'перерыв 15 минут'}» или "
                    f"«{'работаю' if kind == 'work' else 'отдыхаю'} до 18:00».", {"action": kind, "ok": False})
        ends = now + timedelta(minutes=minutes)
        new, replaced = self.store.start(person, kind, minutes, now, ends, said)
        title = "Работаем" if kind == "work" else "Перерыв"
        text = f"{title} {fmt_minutes(minutes)} — до {ends.astimezone(local.tzinfo).strftime('%H:%M')}"
        text += f" ({place['city']}, {utc_offset(local)})." if place else f" (время сервера, {utc_offset(local)})."
        text += " Напишу, когда закончится."
        if replaced:
            done = int((now - replaced["started_at"]).total_seconds() // 60)
            text += (f"\nПредыдущую сессию ({'работа' if replaced['kind'] == 'work' else 'перерыв'}, "
                     f"{fmt_minutes(replaced['minutes'])}) остановил через {fmt_minutes(done)}.")
        if not place:
            text += "\nГде вы? Скажите «я в …» — время и погода будут по вашему городу."
        return text, {"action": kind, "ok": True, "session": _session_out(new, now)}

    def _do_stop(self, person, cmd, said):
        now = self.now()
        row = self.store.stop(person, now)
        if row is None:
            return "Сейчас таймер не идёт.", {"action": "stop", "ok": False}
        done = int((now - row["started_at"]).total_seconds() // 60)
        what = "работу" if row["kind"] == "work" else "перерыв"
        local, _ = self.local(person, now)
        return (f"Остановил {what}: прошло {fmt_minutes(done)} из {fmt_minutes(row['minutes'])}. "
                f"Местное время {local.strftime('%H:%M')}.", {"action": "stop", "ok": True})

    def _do_status(self, person, cmd, said):
        now = self.now()
        local, place = self.local(person, now)
        run = self.store.running(person)
        totals = self._today(person, local)
        if run is None:
            return f"Таймер не идёт. {totals}", {"action": "status"}
        left = max(0, int((run["ends_at"] - now).total_seconds()))
        what = "Работа" if run["kind"] == "work" else "Перерыв"
        passed = int((now - run["started_at"]).total_seconds() // 60)
        return (f"{what}: осталось {fmt_minutes((left + 59) // 60)} (до "
                f"{run['ends_at'].astimezone(local.tzinfo).strftime('%H:%M')}), прошло {fmt_minutes(passed)} из "
                f"{fmt_minutes(run['minutes'])}. {totals}", {"action": "status"})

    def _today(self, person: str, local: datetime) -> str:
        since = datetime.combine(local.date(), time(0), tzinfo=local.tzinfo)
        t = self.store.today_totals(person, since.astimezone(UTC))
        if not t:
            return "Сегодня сессий ещё не было."
        return f"Сегодня: работа {fmt_minutes(t.get('work', 0))}, отдых {fmt_minutes(t.get('rest', 0))}."

    def _do_where(self, person, cmd, said):
        now = self.now()
        local, place = self.local(person, now)
        if place is None:
            return (f"Место не записано. Время сервера: {fmt_local(local)}. Скажите, где вы: «я в Варшаве».",
                    {"action": "where"})
        w = self.weather(place)
        return (f"Вы в городе {place['city']} ({place['country']}) с {_since(place, ZoneInfo(place['tz']))}.\n"
                f"Местное время: {fmt_local(local)}.\n{weather_line(w)}" + self._plans_hint(person, now),
                {"action": "where", "weather": w})

    # ---------- книги ----------

    def _find_book(self, person: str, cmd: Command) -> tuple[dict | None, str]:
        """Книга по номеру, названию или единственная читаемая. (книга, пояснение, если не нашлась)."""
        if cmd.book_no is not None:
            b = self.store.book(person, cmd.book_no)
            return (b, "") if b else (None, f"Книги № {cmd.book_no} нет. {self._books_short(person)}")
        active = self.store.books(person, include_done=False)
        if cmd.book_ref:
            ref = cmd.book_ref.lower().replace("ё", "е")
            found = [b for b in self.store.books(person) if ref[:6] in b["title"].lower().replace("ё", "е")
                     or b["title"].lower().replace("ё", "е")[:6] in ref]
            if len(found) == 1:
                return found[0], ""
        reading = [b for b in active if b["status"] == "reading"] or active
        if cmd.book_no is None and not cmd.book_ref and len(reading) == 1:
            return reading[0], ""
        if not active:
            return None, "Книг пока нет. Зарегистрируйте: «зарегистрируй книгу Л.Н. Толстой „Война и мир“»."
        return None, f"Какая книга? {self._books_short(person)} Например: «книга № 1, страница 70»."

    def _books_short(self, person: str) -> str:
        bs = self.store.books(person, include_done=False)
        return ("Сейчас: " + "; ".join(f"№ {b['num']} «{b['title']}»" for b in bs) + ".") if bs else ""

    def _do_book_add(self, person, cmd, said):
        same = [b for b in self.store.books(person) if b["title"].lower() == (cmd.title or "").lower()
                and (b["author"] or "").lower() == (cmd.author or "").lower()]
        if same:
            return f"Уже есть: книга {book_label(same[0])}.", {"action": "book_add", "ok": False,
                                                                "book": _book_out(same[0])}
        b = self.store.add_book(person, cmd.author or "", cmd.title or "", cmd.total_pages, said, self.now())
        pages = f", {b['total_pages']} стр." if b["total_pages"] else ""
        return (f"Зарегистрировал книгу {book_label(b)}{pages}. Отмечайте: «книга № {b['num']}, страница 70».",
                {"action": "book_add", "ok": True, "book": _book_out(b)})

    def _do_book_page(self, person, cmd, said):
        b, why = self._find_book(person, cmd)
        if b is None:
            return why, {"action": "book_page", "ok": False}
        now = self.now()
        local, place = self.local(person, now)
        log = self.store.reading_log(person, [b["id"]])
        prev = log[-1] if log else None
        self.store.add_reading(b, cmd.page, now, place["city"] if place else None,
                               place["tz"] if place else str(local.tzinfo), said)
        if b["status"] != "reading":
            b = self.store.update_book(b["id"], status="reading", finished_at=None)
        parts = [f"Книга {book_label(b)}: стр. {cmd.page}"]
        if b["total_pages"]:
            parts[0] += f" из {b['total_pages']} ({round(min(cmd.page / b['total_pages'], 1) * 100)} %)"
        sec = section_at(self.store.toc([b["id"]]), cmd.page)
        if sec:
            parts[0] += f", раздел «{sec['title']}» (с стр. {sec['page']})"
        if prev:
            delta = cmd.page - prev["page"]
            when = prev["at"].astimezone(ZoneInfo(prev["tz"]) if _valid(prev.get("tz")) else local.tzinfo)
            if delta >= 0:
                parts.append(f"+{delta} стр. с {when.strftime('%d.%m %H:%M')}")
            else:
                parts.append(f"меньше прежней ({prev['page']}, {when.strftime('%d.%m %H:%M')}) — записал как есть")
        where = f" ({place['city']})" if place else ""
        parts.append(f"записал {local.strftime('%d.%m.%Y %H:%M')}{where}")
        if b["total_pages"] and cmd.page >= b["total_pages"]:
            parts.append("похоже, дочитана — скажите «дочитал книгу " + str(b["num"]) + "»")
        return "; ".join(parts) + ".", {"action": "book_page", "ok": True, "book": _book_out(b), "page": cmd.page}

    def _toc_text(self, b: dict, current: int | None = None) -> str:
        toc = self.store.toc([b["id"]])
        if not toc:
            return "Оглавления нет. Добавьте: «Книга 1, стр. 5, Предисловие» или списком (первая строка «Книга 1»)."
        here = section_at(toc, current)
        lines = []
        for e in toc:
            mark = "  ← вы здесь" if here and e["id"] == here["id"] else ""
            lines.append(f"• {e['title']} — стр. {e['page']}{mark}")
        return "\n".join(lines)

    def _do_book_toc(self, person, cmd, said):
        b, why = self._find_book(person, cmd)
        if b is None:
            return why, {"action": "book_toc", "ok": False}
        added, updated = self.store.upsert_toc(b["id"], cmd.toc)
        if len(cmd.toc) == 1:
            title, page = cmd.toc[0]
            head = (f"Оглавление книги {book_label(b)}: «{title}» — стр. {page}"
                    + (" (страница обновлена)" if updated else "") + ".")
            n = len(self.store.toc([b["id"]]))
            return head + f" Разделов в оглавлении: {n}.", {"action": "book_toc", "ok": True, "added": added,
                                                              "updated": updated}
        log = self.store.reading_log(person, [b["id"]])
        what = f"добавил {added}" + (f", обновил {updated}" if updated else "")
        return (f"Оглавление книги {book_label(b)}: {what}.\n" + self._toc_text(b, log[-1]["page"] if log else None),
                {"action": "book_toc", "ok": True, "added": added, "updated": updated})

    def _do_book_mark_delete(self, person, cmd, said):
        b, why = self._find_book(person, cmd)
        if b is None:
            return why, {"action": "book_mark_delete", "ok": False}
        r = self.store.delete_reading(b["id"], cmd.page)
        if r is None:
            what = f"со страницей {cmd.page}" if cmd.page is not None else ""
            return f"У книги № {b['num']} нет отметок {what}".rstrip() + ".", {"action": "book_mark_delete", "ok": False}
        tz = ZoneInfo(r["tz"]) if _valid(r.get("tz")) else self.host_tz()
        log = self.store.reading_log(person, [b["id"]])
        now = f" Теперь: стр. {log[-1]['page']}." if log else " Отметок больше нет."
        return (f"Удалил отметку книги № {b['num']}: стр. {r['page']}, "
                f"{r['at'].astimezone(tz).strftime('%d.%m.%Y %H:%M')}.{now}", {"action": "book_mark_delete", "ok": True})

    def _do_book_toc_show(self, person, cmd, said):
        b, why = self._find_book(person, cmd)
        if b is None:
            return why, {"action": "book_toc_show", "ok": False}
        log = self.store.reading_log(person, [b["id"]])
        return (f"Оглавление книги {book_label(b)}:\n" + self._toc_text(b, log[-1]["page"] if log else None),
                {"action": "book_toc_show", "ok": True})

    def _do_book_toc_delete(self, person, cmd, said):
        b, why = self._find_book(person, cmd)
        if b is None:
            return why, {"action": "book_toc_delete", "ok": False}
        n = self.store.delete_toc(b["id"], cmd.title or "")
        if not n:
            return f"В оглавлении книги № {b['num']} нет раздела «{cmd.title}».", {"action": "book_toc_delete", "ok": False}
        return f"Убрал из оглавления книги № {b['num']}: «{cmd.title}».", {"action": "book_toc_delete", "ok": True}

    def _do_book_total(self, person, cmd, said):
        b, why = self._find_book(person, cmd)
        if b is None:
            return why, {"action": "book_total", "ok": False}
        b = self.store.update_book(b["id"], total_pages=cmd.total_pages)
        return f"Книга {book_label(b)}: всего {cmd.total_pages} стр.", {"action": "book_total", "ok": True}

    def _book_status(self, person, cmd, status: str, text: str):
        b, why = self._find_book(person, cmd)
        if b is None:
            return why, {"action": f"book_{status}", "ok": False}
        b = self.store.update_book(b["id"], status=status, finished_at=self.now() if status == "done" else None)
        return f"{text}: {book_label(b)}.", {"action": f"book_{status}", "ok": True, "book": _book_out(b)}

    def _do_book_done(self, person, cmd, said):
        return self._book_status(person, cmd, "done", "Отметил как прочитанную")

    def _do_book_pause(self, person, cmd, said):
        return self._book_status(person, cmd, "paused", "Отложил (вернуть — отметьте страницу)")

    def _do_book_delete(self, person, cmd, said):
        if cmd.book_no is None and not cmd.book_ref:
            return "Какую книгу удалить? Например: «удали книгу 3».", {"action": "book_delete", "ok": False}
        return self._book_status(person, cmd, "deleted", "Удалил из списка")

    def _do_books(self, person, cmd, said):
        rows = self.books_table(person)
        if not rows:
            return ("Книг пока нет. Зарегистрируйте: «зарегистрируй книгу Л.Н. Толстой „Война и мир“».",
                    {"action": "books"})
        status = {"reading": "читаю", "paused": "отложена", "done": "прочитана"}
        lines = []
        for r in rows:
            page = f"стр. {r['page']}" + (f" из {r['total_pages']} ({r['percent']} %)" if r["percent"] is not None
                                          else "") if r["page"] is not None else "ещё не отмечали"
            last = f", {r['last_text']}" if r.get("last_text") else ""
            lines.append(f"• {book_label(r)} — {status.get(r['status'], r['status'])}, {page}{last}")
        return "Книги:\n" + "\n".join(lines), {"action": "books"}

    def books_table(self, person: str) -> list[dict]:
        """Таблица книг: всё о книге и её прогресс (books.stats), время последней записи — по поясу того места."""
        now = self.now()
        books = self.store.books(person)
        log = self.store.reading_log(person, [b["id"] for b in books]) if books else []
        toc = self.store.toc([b["id"] for b in books]) if books else []
        out = []
        for b in books:
            st = stats(b, [r for r in log if r["book_id"] == b["id"]], now)
            row = {**_book_out(b), **{k: _iso(v) for k, v in st.items()}}
            book_toc = [e for e in toc if e["book_id"] == b["id"]]
            sec = section_at(book_toc, st["page"])
            row["section"] = sec["title"] if sec else None
            row["toc_count"] = len(book_toc)
            if st["last_at"] is not None:
                tz = ZoneInfo(st["last_tz"]) if _valid(st["last_tz"]) else self.host_tz()
                row["last_text"] = st["last_at"].astimezone(tz).strftime("%d.%m.%Y %H:%M") + (
                    f" ({st['last_city']})" if st["last_city"] else "")
            out.append(row)
        return out

    def book_history(self, person: str, book_id: int) -> dict | None:
        b = self.store.book_by_id(person, book_id)
        if b is None:
            return None
        toc = self.store.toc([book_id])
        rows, prev = [], None
        for r in self.store.reading_log(person, [book_id]):
            tz = ZoneInfo(r["tz"]) if _valid(r.get("tz")) else self.host_tz()
            local = r["at"].astimezone(tz)
            rows.append({"id": r["id"], "at": _iso(r["at"]), "local_text": local.strftime("%d.%m.%Y %H:%M"),
                         "utc_offset": utc_offset(local), "page": r["page"], "city": r.get("city"),
                         "delta": None if prev is None else r["page"] - prev,
                         "section": (section_at(toc, r["page"]) or {}).get("title")})
            prev = r["page"]
        current = rows[-1]["page"] if rows else None
        here = section_at(toc, current)
        contents = [{"id": e["id"], "title": e["title"], "page": e["page"],
                     "state": ("current" if here and e["id"] == here["id"] else
                               "read" if current is not None and e["page"] <= current else "ahead")} for e in toc]
        return {"book": _book_out(b), "entries": list(reversed(rows)), "toc": contents}

    # ---------- окончание сессии (таймер) ----------

    def compose_end(self, session: dict, now: datetime | None = None) -> tuple[str, dict]:
        now = now or self.now()
        ended = session["ends_at"]
        place = self.place(session["person"], ended)
        tz = self.tz_of(place)
        local = ended.astimezone(tz)
        work = session["kind"] == "work"
        title = (f"Рабочая сессия {fmt_minutes(session['minutes'])} закончилась." if work
                 else f"Перерыв {fmt_minutes(session['minutes'])} закончился.")
        where = (f"{place['city']}: {fmt_local(local, seconds=True)}." if place
                 else f"Время сервера: {fmt_local(local, seconds=True)} — место не указано.")
        late = (now - ended).total_seconds()
        if late > LATE_SECONDS:
            where += f" Сообщение с опозданием на {fmt_minutes(int(late // 60))}: служба ядра была недоступна."
        w = self.weather(place)
        hint = "Пора отдохнуть: «перерыв 15 минут»." if work else "Продолжаем? «работаем час»."
        text = "\n".join([title, where, weather_line(w), hint])
        data = {"kind": session["kind"], "minutes": session["minutes"], "ended_at": ended.isoformat(),
                "local_time": local.isoformat(), "utc_offset": utc_offset(local),
                "city": place["city"] if place else None, "country": place["country"] if place else None,
                "tz": place["tz"] if place else str(tz), "weather": w}
        return text, data

    def finish_due(self, limit: int = 20) -> list[dict]:
        """Завершить сессии, у которых вышло время: сообщение с местным временем и погодой."""
        out = []
        for sess in self.store.claim_due(self.now(), limit):
            try:
                text, data = self.compose_end(sess)
            except Exception as exc:  # noqa: BLE001 — сообщение всё равно нужно, хоть без погоды
                text, data = (f"{'Рабочая сессия' if sess['kind'] == 'work' else 'Перерыв'} "
                              f"{fmt_minutes(sess['minutes'])} закончилась.", {"error": str(exc)[:300]})
            row = self.store.finish(sess, text, data, self.now())
            if row:
                out.append(row)
        return out

    # ---------- состояние для веба ----------

    def state(self, person: str) -> dict[str, Any]:
        now = self.now()
        local, place = self.local(person, now)
        run = self.store.running(person)
        return {
            "person": person,
            "now": now.isoformat(),
            "local_time": local.isoformat(),
            "local_text": fmt_local(local),
            "utc_offset": utc_offset(local),
            "tz": place["tz"] if place else str(local.tzinfo),
            "place": _place_out(place) if place else None,
            "upcoming": [_place_out(u) for u in self.store.upcoming(person, now)],
            "session": _session_out(run, now) if run else None,
            "today": self._today(person, local),
            "unread": [_notice_out(n) for n in self.store.notices(person, unread=True)],
            "books": [r for r in self.books_table(person) if r["status"] == "reading"],
        }


def weather_line(w: dict) -> str:
    if w.get("ok"):
        src = " (Яндекс Погода)" if "pogoda" in (w.get("url") or "") else ""
        return f"Погода сейчас: {w['text']}{src}."
    return f"Погоду получить не удалось: {w.get('reason', 'нет данных')}."


def _since(place: dict, tz: tzinfo) -> str:
    return place["effective_at"].astimezone(tz).strftime("%d.%m %H:%M")


def _place_out(p: dict) -> dict:
    return {"id": p["id"], "city": p["city"], "country": p["country"], "tz": p["tz"],
            "since": _iso(p["effective_at"]), "date": move_day(p).isoformat(), "direction": p.get("direction")}


def _session_out(r: dict, now: datetime) -> dict:
    return {"id": r["id"], "kind": r["kind"], "minutes": r["minutes"], "started_at": _iso(r["started_at"]),
            "ends_at": _iso(r["ends_at"]), "remaining_s": max(0, int((r["ends_at"] - now).total_seconds()))}


def _valid(tz: str | None) -> bool:
    from copilot1c.secretary.places import valid_tz

    return bool(tz) and valid_tz(tz)


def _book_out(b: dict) -> dict:
    return {"id": b["id"], "num": b["num"], "author": b["author"], "title": b["title"], "total_pages": b["total_pages"],
            "status": b["status"], "created_at": _iso(b["created_at"]), "finished_at": _iso(b.get("finished_at")),
            "label": book_label(b)}


def _notice_out(n: dict) -> dict:
    return {"id": n["id"], "kind": n["kind"], "text": n["text"], "data": n["data"], "created_at": _iso(n["created_at"]),
            "read": n["read_at"] is not None}


# ---------- разбор моделью ----------

MODEL_SCHEMA = {
    "type": "object",
    "properties": {
        "commands": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["place", "work", "rest", "stop", "status", "where",
                                                          "history", "cancel_trip", "book_add", "book_page",
                                                          "book_total", "book_done", "book_pause", "books",
                                                          "unknown"]},
                    "book_no": {"type": "integer", "description": "номер книги, если назван"},
                    "page": {"type": "integer", "description": "страница, на которой человек сейчас"},
                    "author": {"type": "string"},
                    "title": {"type": "string", "description": "название книги"},
                    "total_pages": {"type": "integer", "description": "всего страниц в книге"},
                    "place": {"type": "string", "description": "город, как сказано"},
                    "date": {"type": "string", "description": "дата переезда YYYY-MM-DD, если названа"},
                    "minutes": {"type": "integer"},
                    "until": {"type": "string", "description": "время окончания HH:MM, если названо"},
                },
                "required": ["action"],
            },
        },
    },
    "required": ["commands"],
}


def model_parse(text: str, today: date, settings) -> list[Command]:
    from copilot1c.index.yandex import chat_json

    data = chat_json(
        f"Сегодня {today.isoformat()} ({WEEKDAY_NAMES[today.weekday()]}). Фраза: «{text}»", MODEL_SCHEMA,
        model=settings.model_batch, settings=settings,
        system="Ты секретарь. Разбери фразу в команды: place — человек находится или едет в город (date — когда, "
               "если не сейчас); work — рабочая сессия на minutes минут или до until; rest — перерыв; stop — "
               "остановить таймер; status — сколько осталось; where — где я, время, погода; history — где я был; "
               "cancel_trip — отменить поездку; book_add — зарегистрировать книгу (author, title); book_page — "
               "отметить страницу (book_no или title, page); book_total — сколько всего страниц; book_done — "
               "дочитал; book_pause — отложил; books — список книг. Не относится к этому — unknown.")
    out = []
    for c in data.get("commands", []):
        a = c.get("action")
        if a in ("book_add", "book_page", "book_total", "book_done", "book_pause", "books"):
            cmd = Command(a, book_no=c.get("book_no"), page=c.get("page"), author=c.get("author"),
                          title=c.get("title"), total_pages=c.get("total_pages"),
                          book_ref=c.get("title") if a != "book_add" and not c.get("book_no") else None)
            if (a == "book_add" and not cmd.title) or (a == "book_page" and not cmd.page):
                continue
            out.append(cmd)
            continue
        if a not in ("place", "work", "rest", "stop", "status", "where", "history", "cancel_trip"):
            continue
        when = None
        if c.get("date"):
            try:
                when = date.fromisoformat(c["date"])
            except ValueError:
                when = None
        until = None
        if c.get("until"):
            try:
                until = time.fromisoformat(c["until"])
            except ValueError:
                until = None
        minutes = c.get("minutes") if isinstance(c.get("minutes"), int) and 0 < c["minutes"] <= 720 else None
        if a == "place" and not c.get("place"):
            continue
        out.append(Command(a, place=c.get("place"), when=when, minutes=minutes, until=until))
    return out
