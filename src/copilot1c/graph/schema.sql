-- Граф знаний, реестры и база поиска «1С Project Copilot» (PostgreSQL + pgvector)
-- Поиск идёт в PostgreSQL (search.py): без pgvector ядро не работает.
CREATE EXTENSION IF NOT EXISTS vector;

-- Фрагменты базы проекта: единица поиска. status: active — в поиске; superseded — заменён новой редакцией
-- (в поиск по умолчанию не попадает). contours — контуры (система / процесс / проект), к которым относится
-- фрагмент; material_id — загрузка, из которой фрагмент пришёл (NULL — проиндексирован командой index-docs).
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
    embedding    vector,                         -- без размерности: модель эмбеддингов можно сменить
    status       text NOT NULL DEFAULT 'active',
    contours     bigint[] NOT NULL DEFAULT '{}',
    material_id  bigint,
    indexed_at   timestamptz NOT NULL DEFAULT now(),
    tsv          tsvector GENERATED ALWAYS AS (to_tsvector('russian', coalesce(title, '') || ' ' || text)) STORED
);
-- Переход с Vector Store (07.10.2026) для таблицы прежней схемы. Выполняется один раз: ALTER берёт
-- исключительную блокировку и при каждом init-db ждал бы открытых транзакций демона.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'chunks' AND column_name = 'tsv') THEN
        ALTER TABLE chunks DROP COLUMN IF EXISTS vs_file_id;
        ALTER TABLE chunks DROP COLUMN IF EXISTS embedding;  -- прежний резервный vector(256) не заполнялся
        ALTER TABLE chunks ADD COLUMN embedding vector;
        ALTER TABLE chunks ADD COLUMN IF NOT EXISTS status text NOT NULL DEFAULT 'active';
        ALTER TABLE chunks ADD COLUMN IF NOT EXISTS contours bigint[] NOT NULL DEFAULT '{}';
        ALTER TABLE chunks ADD COLUMN IF NOT EXISTS material_id bigint;
        ALTER TABLE chunks ADD COLUMN IF NOT EXISTS indexed_at timestamptz NOT NULL DEFAULT now();
        ALTER TABLE chunks ADD COLUMN tsv tsvector
            GENERATED ALWAYS AS (to_tsvector('russian', coalesce(title, '') || ' ' || text)) STORED;
    END IF;
END $$;
CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS chunks_attrs_idx ON chunks USING gin (attrs jsonb_path_ops);
CREATE INDEX IF NOT EXISTS chunks_contours_idx ON chunks USING gin (contours);
CREATE INDEX IF NOT EXISTS chunks_material_idx ON chunks (material_id);
CREATE INDEX IF NOT EXISTS chunks_status_idx ON chunks (project, status);
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

-- Контуры: к чему относится материал — система (УТ 11, БП 3.0…), процесс или тема (командировки, НСИ…),
-- проект (обновление УТ 11 до 11.5.27.75). Справочник ведут аналитики; notes — предметные пояснения для
-- промпта агента (префиксы доработок, версии, особенности), aliases — как контур называют в письмах.
CREATE TABLE IF NOT EXISTS contours (
    id          bigserial PRIMARY KEY,
    project     text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('system', 'process', 'project')),
    name        text NOT NULL,
    parent_id   bigint REFERENCES contours(id) ON DELETE SET NULL,
    aliases     text[] NOT NULL DEFAULT '{}',
    notes       text,
    active      boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project, kind, name)
);

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

-- Вложения обращений: скриншоты, логи, письма. В базу поиска не попадают.
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

-- ===== Письма и ветки переписки =====
-- Единица хранения — отдельное письмо, а не файл: файл с ответом несёт в цитатах всю ветку, и уже известные
-- письма не индексируются повторно. Ветка связывает письма одного обсуждения независимо от смены темы
-- (по пересечению с известными письмами); у неё сводка состояния, которая пересобирается при новых письмах.
CREATE TABLE IF NOT EXISTS threads (
    id               bigserial PRIMARY KEY,
    project          text NOT NULL,
    subject          text NOT NULL,              -- тема первого письма без RE/FW
    title            text,                       -- название ветки, если аналитик переименовал
    contours         bigint[] NOT NULL DEFAULT '{}',
    issue_id         bigint REFERENCES issues(id) ON DELETE SET NULL,
    summary          jsonb,                      -- {problem, done[], waiting, status, open_questions[]}
    summary_text     text,
    summary_letters  int NOT NULL DEFAULT 0,     -- сколько писем было, когда собиралась сводка
    summary_at       timestamptz,
    summary_chunk    text,                       -- фрагмент базы поиска со сводкой
    first_at         timestamptz,
    last_at          timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS threads_project_idx ON threads (project, last_at DESC);

CREATE TABLE IF NOT EXISTS letters (
    id            bigserial PRIMARY KEY,
    project       text NOT NULL,
    thread_id     bigint NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    parent_id     bigint REFERENCES letters(id) ON DELETE SET NULL,   -- на какое письмо это ответ
    message_id    text,                          -- только у писем, пришедших файлом
    sender        text,
    sender_email  text,
    sender_key    text NOT NULL,                 -- фамилия или имя ящика: одинаковы в письме и в цитате
    recipients    text,
    sent_at       timestamptz,
    subject       text,
    body          text NOT NULL,                 -- очищенный текст без цитат
    inline_notes  text,                          -- ответы, вписанные этим письмом внутрь цитаты
    origin        text NOT NULL,                 -- file | quoted (известно только по цитате)
    source        text,
    material_id   bigint REFERENCES materials(id) ON DELETE SET NULL,
    chunk_id      text,
    first_seen    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS letters_message_idx ON letters (project, message_id) WHERE message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS letters_sender_idx ON letters (project, sender_key, sent_at);
CREATE INDEX IF NOT EXISTS letters_thread_idx ON letters (thread_id, sent_at);
