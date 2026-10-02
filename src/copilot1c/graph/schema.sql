-- Граф знаний и реестры «1С Project Copilot» (Managed PostgreSQL + pgvector)
CREATE EXTENSION IF NOT EXISTS vector;

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
    vs_file_id   text,             -- id файла в AI Studio Vector Store
    embedding    vector(256)       -- резервный локальный поиск; размерность сверить с моделью
);
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
CREATE TABLE IF NOT EXISTS requirements (
    project   text NOT NULL,
    req_id    text NOT NULL,       -- номер пункта ТЗ
    doc       text NOT NULL,       -- ТЗ ред. N
    text      text NOT NULL,
    status    text,                -- согласовано / на согласовании / исключено
    objects   text[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (project, req_id, doc)
);

CREATE TABLE IF NOT EXISTS test_cases (
    project    text NOT NULL,
    doc        text NOT NULL,      -- ПиМИ
    num        text NOT NULL,
    section    text,
    function   text,
    method     text,
    criterion  text,
    result     text,               -- Работает / Не работает / …
    objects    text[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (project, doc, num)
);

CREATE TABLE IF NOT EXISTS requirement_tests (
    project  text NOT NULL,
    req_id   text NOT NULL,
    doc      text NOT NULL,
    test_doc text NOT NULL,
    test_num text NOT NULL,
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
