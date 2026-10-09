-- Секретарь: местоположение, рабочие сессии и перерывы, сообщения таймера. Личное, не зависит от проекта:
-- записи разделяются по person (поле «Я» в вебе). Создаётся ядром само при первом обращении (secretary/store.py).

-- Журнал мест: где человек с effective_at. «Завтра в Лондон» — запись с effective_at = завтра 00:00 по местному
-- времени; текущее место — последняя не отменённая запись с effective_at <= сейчас.
CREATE TABLE IF NOT EXISTS sec_places (
    id            bigserial PRIMARY KEY,
    person        text NOT NULL,
    city          text NOT NULL,
    country       text NOT NULL DEFAULT '',
    tz            text NOT NULL,
    effective_at  timestamptz NOT NULL,
    direction     text NOT NULL DEFAULT 'arrive',   -- arrive | depart — только для текста ответа
    on_date       date,                             -- день переезда, как его назвали («завтра» → 10.10)
    said          text,
    cancelled     boolean NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE sec_places ADD COLUMN IF NOT EXISTS on_date date;
ALTER TABLE sec_places ADD COLUMN IF NOT EXISTS lat double precision;  -- координаты — для астрономии
ALTER TABLE sec_places ADD COLUMN IF NOT EXISTS lon double precision;
CREATE INDEX IF NOT EXISTS sec_places_person_idx ON sec_places (person, effective_at DESC, id DESC);

-- Города, найденные моделью: форма из фразы → город, страна, пояс (чтобы не спрашивать модель повторно)
CREATE TABLE IF NOT EXISTS sec_place_names (
    form        text PRIMARY KEY,
    city        text NOT NULL,
    country     text NOT NULL DEFAULT '',
    tz          text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Сессии: работа или перерыв. running → (по таймеру) finishing → done; досрочно — stopped; новая сессия поверх
-- идущей — replaced. finishing переживает перезапуск службы: после старта такие сессии завершаются снова.
CREATE TABLE IF NOT EXISTS sec_sessions (
    id           bigserial PRIMARY KEY,
    person       text NOT NULL,
    kind         text NOT NULL CHECK (kind IN ('work', 'rest')),
    minutes      int NOT NULL,
    started_at   timestamptz NOT NULL DEFAULT now(),
    ends_at      timestamptz NOT NULL,
    status       text NOT NULL DEFAULT 'running',
    finished_at  timestamptz,
    said         text
);
ALTER TABLE sec_place_names ADD COLUMN IF NOT EXISTS lat double precision;
ALTER TABLE sec_place_names ADD COLUMN IF NOT EXISTS lon double precision;

CREATE INDEX IF NOT EXISTS sec_sessions_due_idx ON sec_sessions (ends_at) WHERE status IN ('running', 'finishing');
CREATE INDEX IF NOT EXISTS sec_sessions_person_idx ON sec_sessions (person, started_at DESC);

-- Сообщения секретаря, которые надо доставить в веб (окончание сессии). read_at — когда показано.
CREATE TABLE IF NOT EXISTS sec_notices (
    id          bigserial PRIMARY KEY,
    person      text NOT NULL,
    session_id  bigint REFERENCES sec_sessions(id) ON DELETE SET NULL,
    kind        text NOT NULL DEFAULT 'session_end',
    text        text NOT NULL,
    data        jsonb NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now(),
    read_at     timestamptz
);
CREATE INDEX IF NOT EXISTS sec_notices_person_idx ON sec_notices (person, id);

-- Книги: несколько параллельно, номер — у каждого человека свой (книга № 1, № 2…). status: reading | paused |
-- done | deleted (удалённые не показываются, номер не переиспользуется).
CREATE TABLE IF NOT EXISTS sec_books (
    id           bigserial PRIMARY KEY,
    person       text NOT NULL,
    num          int NOT NULL,
    author       text NOT NULL DEFAULT '',
    title        text NOT NULL,
    total_pages  int,
    status       text NOT NULL DEFAULT 'reading',
    said         text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    UNIQUE (person, num)
);

-- Журнал чтения: страница на момент записи, где человек был (город и пояс — для местного времени в таблице)
CREATE TABLE IF NOT EXISTS sec_reading (
    id       bigserial PRIMARY KEY,
    book_id  bigint NOT NULL REFERENCES sec_books(id) ON DELETE CASCADE,
    person   text NOT NULL,
    page     int NOT NULL,
    at       timestamptz NOT NULL DEFAULT now(),
    city     text,
    tz       text,
    said     text
);
CREATE INDEX IF NOT EXISTS sec_reading_book_idx ON sec_reading (book_id, at);

-- Оглавление книги: раздел и страница, с которой он начинается («Книга 1, стр. 5, Предисловие»). Раздел с тем же
-- названием перезаписывается новой страницей.
CREATE TABLE IF NOT EXISTS sec_book_toc (
    id          bigserial PRIMARY KEY,
    book_id     bigint NOT NULL REFERENCES sec_books(id) ON DELETE CASCADE,
    title       text NOT NULL,
    page        int NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (book_id, title)
);
CREATE INDEX IF NOT EXISTS sec_book_toc_page_idx ON sec_book_toc (book_id, page);

-- Небо: события Солнца, Луны, планет и звёзд в месте, где человек (secretary/astro.py). Планировщик таймера
-- считает их на сутки вперёд, таймер в момент события пишет тихое сообщение (sec_notices.kind = 'astro').
-- Ключ — на местные сутки: пересчёт не плодит дубли. Переехал — события прежнего места пропускаются (skipped).
CREATE TABLE IF NOT EXISTS sec_astro (
    id         bigserial PRIMARY KEY,
    person     text NOT NULL,
    place_id   bigint NOT NULL,
    body       text NOT NULL,
    kind       text NOT NULL,
    local_day  date NOT NULL,
    at         timestamptz NOT NULL,
    alt        double precision,
    text       text NOT NULL,
    data       jsonb NOT NULL DEFAULT '{}',
    status     text NOT NULL DEFAULT 'pending',   -- pending | sent | skipped
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (person, place_id, body, kind, local_day)
);
CREATE INDEX IF NOT EXISTS sec_astro_due_idx ON sec_astro (at) WHERE status = 'pending';

-- Настройки секретаря человека: присылать ли небо, планеты, звёзды
CREATE TABLE IF NOT EXISTS sec_settings (
    person      text PRIMARY KEY,
    astro       boolean NOT NULL DEFAULT true,
    planets     boolean NOT NULL DEFAULT true,
    stars       boolean NOT NULL DEFAULT true,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
