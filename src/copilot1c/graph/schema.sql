-- Граф знаний и реестры «1С Project Copilot» (Managed PostgreSQL + pgvector)
-- pgvector есть в Managed PostgreSQL Yandex Cloud; локально без него схема тоже создаётся
DO $$ BEGIN
    CREATE EXTENSION IF NOT EXISTS vector;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'pgvector недоступен: колонка chunks.embedding не создаётся';
END $$;

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id     text PRIMARY KEY,
    project      text NOT NULL,
    doc_type     text NOT NULL,
    source       text NOT NULL,
    title        text,
    doc_version  text,
    created_at   timestamptz,
    author       text,
    objects      text[] NOT NULL DEFAULT '{}',
    attrs        jsonb  NOT NULL DEFAULT '{}',
    text         text   NOT NULL,
    vs_file_id   text              -- id файла в AI Studio Vector Store
);
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        -- резервный локальный поиск; размерность сверить с моделью эмбеддингов
        ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embedding vector(256);
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS chunks_objects_idx ON chunks USING gin (objects);
CREATE INDEX IF NOT EXISTS chunks_type_idx ON chunks (project, doc_type);

CREATE TABLE IF NOT EXISTS entities (
    key      text PRIMARY KEY,     -- kind:name (casefold)
    kind     text NOT NULL,
    name     text NOT NULL,
    attrs    jsonb NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS relations (
    src           text NOT NULL REFERENCES entities(key),
    rel           text NOT NULL,
    dst           text NOT NULL REFERENCES entities(key),
    source_chunk  text REFERENCES chunks(chunk_id),
    PRIMARY KEY (src, rel, dst)
);
CREATE INDEX IF NOT EXISTS relations_dst_idx ON relations (dst, rel);

-- Упоминания сущностей в чанках: основа ссылок «ответ → источник»
CREATE TABLE IF NOT EXISTS mentions (
    entity    text NOT NULL REFERENCES entities(key),
    chunk_id  text NOT NULL REFERENCES chunks(chunk_id),
    PRIMARY KEY (entity, chunk_id)
);

-- Реестры для аналитики (агрегаты через инструмент sql)
-- Требования: пункты плана тестирования ТЗ (Приложение 1) и пункты ТЗ
CREATE TABLE IF NOT EXISTS requirements (
    project   text NOT NULL,
    req_id    text NOT NULL,       -- № пункта плана тестирования / пункта ТЗ
    doc       text NOT NULL,       -- «ТЗ … (ред. 2.0)», «ДС № 10 … (ред. 3)»
    grp       text,                -- группа плана: «3. НСИ», «1.1 EDI FML»
    object    text,                -- объект проверки
    text      text NOT NULL,
    status    text,                -- согласовано / на согласовании / исключено
    objects   text[] NOT NULL DEFAULT '{}',
    source    text,
    PRIMARY KEY (project, req_id, doc)
);

CREATE TABLE IF NOT EXISTS test_cases (
    project    text NOT NULL,
    doc        text NOT NULL,      -- заголовок ПиМИ
    num        text NOT NULL,
    section    text,
    function   text,
    steps      jsonb NOT NULL DEFAULT '[]',   -- [{method, criterion, result}]
    result     text,               -- итог по шагам: Работает / Не работает / …
    objects    text[] NOT NULL DEFAULT '{}',
    source     text,
    PRIMARY KEY (project, doc, num)
);

-- Покрытие требований: из таблицы ТЗ «документ → пункты плана» (test_num = '*') или ручная/LLM-разметка
CREATE TABLE IF NOT EXISTS requirement_tests (
    project  text NOT NULL,
    req_id   text NOT NULL,
    doc      text NOT NULL,
    test_doc text NOT NULL,
    test_num text NOT NULL DEFAULT '*',
    coverage text,                 -- «Полное», «Полное при условии п. 129…»
    PRIMARY KEY (project, req_id, doc, test_doc, test_num)
);

-- Граф кода
CREATE TABLE IF NOT EXISTS md_objects (
    config     text NOT NULL,      -- метка выгрузки: «УТ 11.5.27.75», «КС_Доработки 1.0.3»
    full_name  text NOT NULL,      -- Справочник.Номенклатура
    synonym    text,
    custom     boolean NOT NULL DEFAULT false,
    adopted    boolean NOT NULL DEFAULT false,
    card       text,
    PRIMARY KEY (config, full_name)
);

CREATE TABLE IF NOT EXISTS bsl_methods (
    config      text NOT NULL,
    module      text NOT NULL,
    name        text NOT NULL,
    owner       text NOT NULL,     -- объект метаданных
    signature   text NOT NULL,
    export      boolean NOT NULL,
    start_line  int NOT NULL,
    end_line    int NOT NULL,
    description text,
    intercepts  jsonb NOT NULL DEFAULT '[]',
    PRIMARY KEY (config, module, name)
);

CREATE TABLE IF NOT EXISTS bsl_calls (
    config  text NOT NULL,
    module  text NOT NULL,
    caller  text NOT NULL,
    callee  text NOT NULL,         -- «Модуль.Метод» или «Метод»
    PRIMARY KEY (config, module, caller, callee)
);

-- Анализ влияния: нетиповые объекты, пересекающиеся с изменениями релиза
CREATE OR REPLACE VIEW custom_objects AS
SELECT config, full_name, synonym FROM md_objects WHERE custom;

-- Требования без тест-кейсов
CREATE OR REPLACE VIEW uncovered_requirements AS
SELECT r.* FROM requirements r
LEFT JOIN requirement_tests rt USING (project, req_id, doc)
WHERE rt.test_num IS NULL;

-- Материалы, загруженные через веб: файл, его путь в data/uploads и судьба в конвейере
-- (в очереди → разбор → индексация → запись в базу → готово | уже есть | ошибка)
CREATE TABLE IF NOT EXISTS materials (
    id           bigserial PRIMARY KEY,
    project      text NOT NULL,
    filename     text NOT NULL,          -- имя, как его загрузили
    path         text NOT NULL,          -- путь относительно рабочего каталога ядра
    sha256       text NOT NULL,
    size         bigint NOT NULL,
    status       text NOT NULL DEFAULT 'queued',
    detail       text,                   -- что сейчас происходит или причина ошибки
    report       jsonb NOT NULL DEFAULT '{}',
    uploaded_at  timestamptz NOT NULL DEFAULT now(),
    started_at   timestamptz,
    finished_at  timestamptz,
    UNIQUE (project, sha256)
);
CREATE INDEX IF NOT EXISTS materials_status_idx ON materials (status, id);

-- ===== Обращения (проблемы, о которых сообщают аналитики интегратора) =====
-- Заводят обращения только аналитики интегратора (через веб или чат). Пользователи заказчика с системой
-- не работают: они — инициаторы проблем и хранятся в contacts (обычно извлекаются из приложенного письма).

-- Контакты: люди заказчика (и при необходимости интегратора), от которых приходят обращения
CREATE TABLE IF NOT EXISTS contacts (
    id            bigserial PRIMARY KEY,
    name          text NOT NULL,
    email         text,                   -- уникален без учёта регистра; может отсутствовать
    organization  text,
    position      text,                   -- должность из подписи письма
    phone         text,
    entity_key    text,                   -- сущность графа (participant:…), если человек уже известен по переписке
    first_seen    timestamptz NOT NULL DEFAULT now(),
    last_seen     timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS contacts_email_idx ON contacts (lower(email)) WHERE email IS NOT NULL;

CREATE TABLE IF NOT EXISTS issues (
    id                     bigserial PRIMARY KEY,   -- номер для людей: ОБР-0001 (формируется из id)
    project                text NOT NULL,
    title                  text NOT NULL,
    -- суть проблемы
    description            text,                    -- исходный текст обращения
    summary                text,                    -- краткое описание (позже — от модели)
    error_text             text,                    -- текст ошибки 1С, как есть
    steps                  text,                    -- шаги воспроизведения
    expected               text,
    actual                 text,
    -- классификация и работа
    category               text NOT NULL DEFAULT 'bug',
    priority               text NOT NULL DEFAULT 'medium',
    status                 text NOT NULL DEFAULT 'new',
    tags                   text[] NOT NULL DEFAULT '{}',
    assignee               text,                    -- аналитик из COPILOT_ANALYSTS
    due_date               date,
    -- окружение
    infobase               text,                    -- рабочая / тестовая / имя базы
    server                 text,
    config_version         text,
    platform_version       text,
    objects                text[] NOT NULL DEFAULT '{}',   -- объекты метаданных
    -- происхождение
    initiator_contact_id   bigint REFERENCES contacts(id) ON DELETE SET NULL,
    reported_at            timestamptz,             -- когда сообщил заказчик (дата письма), не дата регистрации
    registered_by          text,                    -- аналитик, заведший обращение
    source                 text NOT NULL DEFAULT 'manual',  -- manual / chat / email
    source_ref             text,                    -- запись журнала вопросов, путь к письму…
    source_message_id      text,                    -- Message-ID письма: повторно приложенное письмо не даёт дубля
    classifier_confidence  real,
    -- связи
    duplicate_of           bigint REFERENCES issues(id) ON DELETE SET NULL,
    requirement_ids        text[] NOT NULL DEFAULT '{}',
    test_case_ids          text[] NOT NULL DEFAULT '{}',   -- № тест-кейсов ПиМИ
    -- итог
    root_cause             text,
    resolution             text,
    resolved_at            timestamptz,
    kb_material_id         bigint REFERENCES materials(id) ON DELETE SET NULL,  -- разбор, отправленный в базу знаний
    -- служебное
    version                int NOT NULL DEFAULT 1,  -- защита от одновременной правки двумя аналитиками
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS issues_status_idx ON issues (project, status, id);
CREATE INDEX IF NOT EXISTS issues_objects_idx ON issues USING gin (objects);
CREATE UNIQUE INDEX IF NOT EXISTS issues_message_idx ON issues (project, source_message_id)
    WHERE source_message_id IS NOT NULL;

-- Вложения обращений: скриншоты, логи, письма. В индекс (Vector Store) не попадают.
CREATE TABLE IF NOT EXISTS issue_attachments (
    id              bigserial PRIMARY KEY,
    issue_id        bigint NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    filename        text NOT NULL,
    path            text NOT NULL,                  -- относительно рабочего каталога ядра
    mime            text,
    size            bigint NOT NULL,
    sha256          text NOT NULL,
    extracted_text  text,                           -- OCR скриншота, текст лога (заполняется позже)
    uploaded_by     text,
    uploaded_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (issue_id, sha256)
);

-- История обращения: каждое создание, правка поля, смена статуса, комментарий, вложение
CREATE TABLE IF NOT EXISTS issue_events (
    id         bigserial PRIMARY KEY,
    issue_id   bigint NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
    at         timestamptz NOT NULL DEFAULT now(),
    actor      text,                                -- аналитик или «агент»
    type       text NOT NULL,                       -- created / field / status / comment / attachment
    field      text,
    old_value  jsonb,
    new_value  jsonb,
    comment    text
);
CREATE INDEX IF NOT EXISTS issue_events_issue_idx ON issue_events (issue_id, id);
