# 1С Project Copilot

Аналитическая RAG-система для проектов на 1С на Yandex Cloud AI Studio. Один индекс связывает
проектную переписку, ТЗ, допсоглашения и ПиМИ с метаданными и BSL-кодом конфигураций (.cf),
расширений (.cfe) и внешних обработок (.epf). Концепт — в проекте RAG8: «Концепт аналитической
RAG-системы для 1С на Yandex Cloud AI».

## Что уже есть (фаза 1: документы и почта + разбор кода)

| Модуль | Что делает |
| --- | --- |
| `ingest/msg.py` | Письма .msg: свойства MAPI, вложения рекурсивно (msg в msg), дедупликация вложенных писем |
| `ingest/cleaning.py` | Отрезает цитируемые хвосты, подписи и дисклеймеры, раскрывает Safe Links, маскирует телефоны и e-mail |
| `ingest/docx.py` | .docx по стилям заголовков; таблицы ПиМИ → тест-кейсы (№ · Функция · Методика · Критерий · Результат) с разделом |
| `ingest/entities.py` | Regex-якоря: версии платформы и конфигураций, серверы, ДС/ТЗ/ПиМИ, объекты КС_ / (КС) |
| `ingest/entities_llm.py` | Сущности и связи LLM со structured output (JSON-схема) |
| `code1c/platform.py` | Команды 1cv8 DESIGNER: распаковка .cf/.cfe/.epf, сборка расширения, /CheckModules |
| `code1c/bsl.py` | Метод BSL = чанк: комментарий, аннотации (&НаСервере, &После…), область, вызовы, ссылки на метаданные |
| `code1c/metadata.py` | Карточки объектов из XML-выгрузки: реквизиты, типы, табличные части, заимствование |
| `index/yandex.py` | AI Studio через OpenAI-совместимый API: эмбеддинги, structured output, Vector Store |
| `graph/schema.sql`, `graph/store.py` | Граф сущностей и реестры в PostgreSQL: тест-кейсы, требования, методы, вызовы |
| `agent/tools.py` | Инструменты агента (search_docs, search_code, graph_query, get_module, diff_versions, sql, build_and_check) и цикл function calling |

## Быстрый старт

```bash
uv sync --extra dev
uv run pytest

# Локально, без облака: посмотреть, как режутся ваши материалы
uv run copilot1c parse-docs path/to/mails path/to/docs
uv run copilot1c entities path/to/mails
uv run copilot1c parse-code path/to/dump --config "УТ 11.5.27.75"
```

С облаком: скопируйте `.env.example` в `.env`, заполните каталог и API-ключ AI Studio, затем

```bash
uv run copilot1c init-db
export COPILOT_VECTOR_STORE_ID=$(uv run copilot1c create-index ut11-update)
uv run copilot1c index-docs data/mails data/docs --llm-entities
uv run copilot1c ask "Почему обновляемся на 11.5.27.75, а не на 11.6?"
```

Распаковка кода — на ВМ с платформой 1С 8.3.27 (путь к `1cv8` в `.env`):

```bash
uv run copilot1c unpack data/cf/UT11.cf data/dumps/ut11-5-27-75
uv run copilot1c unpack data/cfe/KS.cfe data/dumps/ks --extension КС_Доработки
```

## Что дальше

1. Проверить разбор на реальных письмах и ПиМИ проекта, дособрать правила очистки.
2. Реестр требований из ТЗ и связка «требование ↔ тест-кейс ↔ объект».
3. Запись графа кода (`bsl_methods`, `bsl_calls`, `md_objects`) и batch-автоописание методов.
4. BSL Language Server для диагностик; генерация .cfe и сценариев Vanessa Automation из ПиМИ.
5. Набор eval-вопросов (recall@10, достоверность) — до включения генерации кода.

Имена моделей в `.env.example` сверьте с Model Gallery AI Studio. Исходные материалы (`*.msg`,
`*.cf`, `data/`) в репозиторий не коммитятся — см. `.gitignore`.
