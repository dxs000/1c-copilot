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
