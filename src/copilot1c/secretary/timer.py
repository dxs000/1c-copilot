"""Таймер секретаря — поток внутри демона ядра.

Раз в секунду проверяет sec_sessions: у идущих сессий вышло время → сообщение (местное время, погода) в
sec_notices, сессия done. Время хранится в базе, поэтому таймер переживает перезапуск службы: сессия,
закончившаяся, пока ядро было остановлено, завершается сразу после старта (в сообщении — сколько опоздало).
"""

from __future__ import annotations

import logging
import threading

from copilot1c.config import Settings

log = logging.getLogger("copilot1c.secretary")
POLL_SECONDS = 1.0


class SecretaryTimer:
    def __init__(self, settings: Settings, poll: float = POLL_SECONDS):
        self.s = settings
        self.poll = poll
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.fired = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="secretary-timer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _loop(self) -> None:
        from copilot1c.graph.store import try_connect
        from copilot1c.secretary.service import Secretary

        g = None
        recovered = False
        while not self._stop.is_set():
            try:
                if g is None:
                    g = try_connect(self.s)
                    if g is None:
                        self._stop.wait(10)  # PostgreSQL недоступен — ждём
                        continue
                sec = Secretary(g.conn, self.s)
                if not recovered:
                    n = sec.store.recover()
                    recovered = True
                    if n:
                        log.info("таймер: %s сессий после перезапуска вернул в очередь", n)
                for row in sec.finish_due():
                    self.fired += 1
                    log.info("таймер: сессия %s завершена, сообщение %s", row.get("session_id"), row.get("id"))
            except Exception:  # noqa: BLE001 — поток не должен умирать; соединение — заново
                log.exception("таймер секретаря")
                if g is not None:
                    try:
                        g.close()
                    except Exception:  # noqa: BLE001
                        pass
                    g = None
                self._stop.wait(5)
                continue
            self._stop.wait(self.poll)
        if g is not None:
            g.close()
