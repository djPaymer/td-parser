# TD Parser

HTTP API: найти каталог на сайте производителя, собрать инструкцию скрейпа и собрать URL товаров.

Стек: Python 3.12, FastAPI, httpx, Poetry, PostgreSQL (SQLAlchemy + Alembic), Yandex LLM (OpenAI-compatible Responses API).

Интерактивная спецификация после запуска: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) (`/redoc`, `/openapi.json`).

## Требования

- Python 3.12+
- [Poetry](https://python-poetry.org/)
- PostgreSQL, БД `parser` (`APP_CONFIG__DB__URL`)
- ключ Yandex AI для качественного `POST /api/v1/instruction` (без ключа инструкция строится эвристикой, `parse` работает всегда)

## Локальный запуск

Из корня репозитория:

```bash
poetry install
cp app/.env.template app/.env
```

В `app/.env` задайте ключ агента:

```env
APP_CONFIG__AGENT__API_KEY=
APP_CONFIG__AGENT__FOLDER_ID=
APP_CONFIG__AGENT__BASE_URL=https://ai.api.cloud.yandex.net/v1
APP_CONFIG__AGENT__MODEL=deepseek-v4-flash/latest
```

Запуск API **только через Poetry**:

```bash
poetry run uvicorn app.main:main_app --reload
```

Сервис слушает `http://127.0.0.1:8000`. Корневого `GET /` нет — открывайте `/docs`.

## Конфигурация

Префикс переменных: `APP_CONFIG__`. Вложенные ключи через `__`. Файл: `app/.env` (не коммитится).

| Переменная | Назначение | По умолчанию |
|------------|------------|--------------|
| `APP_CONFIG__RUN__HOST` / `PORT` | хост и порт uvicorn | `0.0.0.0:8000` |
| `APP_CONFIG__FETCH__TIMEOUT` | таймаут HTTP к сайтам | `45` |
| `APP_CONFIG__FETCH__DELAY_SECONDS` | пауза между запросами к сайту | `0.35` |
| `APP_CONFIG__FETCH__MAX_BYTES` | лимит размера HTML (страницы с мега-меню доходят до 1 МБ) | `3000000` |
| `APP_CONFIG__FETCH__VERIFY_SSL` | проверка TLS сайтов | `false` |
| `APP_CONFIG__AGENT__API_KEY` | ключ Yandex AI; пусто — LLM не используется | |
| `APP_CONFIG__AGENT__FOLDER_ID` | folder / project | |
| `APP_CONFIG__AGENT__BASE_URL` | `https://ai.api.cloud.yandex.net/v1` | |
| `APP_CONFIG__AGENT__MODEL` | короткое имя или `gpt://folder/model` | `deepseek-v4-flash/latest` |
| `APP_CONFIG__AGENT__MAX_PAGES` | сколько страниц сайта скачать при построении инструкции | `16` |
| `APP_CONFIG__AGENT__MIN_HITS` | минимум найденных ссылок на товары, чтобы принять инструкцию | `3` |
| `APP_CONFIG__AGENT__MAX_OUTPUT_TOKENS` | бюджет ответа модели (reasoning-модели тратят скрытые токены) | `4000` |
| `APP_CONFIG__DB__URL` | DSN PostgreSQL (`postgresql://user:pass@host:5432/parser`); пусто — хранение выключено | |
| `APP_CONFIG__STORE__TTL_DAYS` | через сколько дней авто-инструкция пересобирается (0 — никогда) | `30` |

## API

Базовый префикс: `/api/v1`. Авторизации нет. Тела — JSON. Запросы долгие (обход сайта + LLM): таймаут клиента 2–5 минут.

| Метод | Путь | Тело | Ответ |
|--------|------|------|--------|
| `POST` | `/api/v1/instruction` | `{ "url": "https://…", "refresh": false }` | JSON инструкции (из хранилища, если есть свежая; иначе строится и сохраняется) |
| `POST` | `/api/v1/parse` | `{ "url": "https://…", "instruction": { … } }` | список товаров; `instruction` можно опустить — возьмётся из хранилища |
| `GET` | `/api/v1/instructions` | | список сохранённых инструкций (host, source, listing, href, updated_at) |
| `GET` | `/api/v1/instructions/{host}` | | запись целиком, включая `meta` — диагностику сборки |
| `PUT` | `/api/v1/instructions/{host}` | `{ "url": "https://…", "instruction": { … } }` | сохранить ручную инструкцию (`source: manual`) |
| `DELETE` | `/api/v1/instructions/{host}` | | удалить запись |

`url` снаружи — корень сайта. `instruction.url` — путь входа в каталог (`/products` или `/`).

Ошибки: `{ "detail": "…" }`. 400 — кривой URL, 404 — нет сохранённой инструкции, 422 — кривое тело / невалидный regex, 502 — сайт не отдал HTML, модель или инструкция не собралась, 503 — хранилище выключено.

### Формат инструкции

```json
{
  "engine": "html",
  "url": "/",
  "links": { "href": "^(?:https?://[^/?#]+)?/(?:tools|garden)/[^/?#]+/[^/?#]+/?$", "name": "text" },
  "categories": { "href": "^(?:https?://[^/?#]+)?(?:/(?:tools|garden)|/(?:tools|garden)/[^/?#]+)/?$", "max_pages": 300 },
  "pagination": { "param": "page" }
}
```

- `links.href` — regex товара; проверяется на `href`, полном URL и пути. Дополнительно: `path`, `class` (подстрока класса `<a>`), `skip_href`, `name` (`text` | `title` | `slug`).
- `categories.href` — regex страниц-категорий, по которым парсер спускается от `url` (BFS, до `max_pages` страниц). Без него парсер, как раньше, берёт один уровень ссылок под `url`.
- `pagination.param` — query-параметр пейджера (`page`, `PAGEN_1`…). Если не задан, парсер пытается определить его по ссылкам пейджера.

Все regex валидируются при приёме (422 вместо падения).

## Как строится инструкция

1. **Обход** (`agents/crawl.py`). Скачивается главная и до `MAX_PAGES` страниц. Кандидаты выбираются не по английским словам в URL, а по форме: все внутренние ссылки группируются в *шаблоны путей* (`/tools/{*}/{*}`, `/product/{N}`), и обход спускается в самый глубокий ещё не посещённый шаблон. Так каталог `раздел → категория → подкатегория → товар` проходится сверху вниз на любом языке.
2. **Анализ форм** (`agents/shapes.py`). Для каждого шаблона считаются признаки: число ссылок, доля ссылок с картинкой, длина текста, доля навигационных (есть на большинстве страниц), «лист ли это» (посещённая страница шаблона не показывает вложенных страниц). Шаблоны сортируются эвристикой.
3. **Выбор** (`agents/instruction.py`). LLM получает топ-10 форм со статистикой и примерами и **только выбирает** id форм-товаров — regex она не пишет. Если LLM не настроена или ответила невалидно, выбирает эвристика.
4. **Сборка кодом.** Из выбранных форм синтезируется regex товара, вход в каталог (общий литеральный префикс), regex категорий (уровни между входом и товаром) и параметр пагинации (по ссылкам пейджера на страницах-списках).
5. **Проверка.** Инструкция прогоняется по скачанным страницам: минимум `MIN_HITS` товаров, форма не должна быть навигацией или категорией. Отклонённый вариант заменяется следующим; диагностика сохраняется в `meta` записи.

## Хранение инструкций

Одна запись на хост (`www.` игнорируется): `host`, `site_url`, `instruction`, `source` (`manual` | `agent-llm` | `agent-heuristic`), `meta` (кандидаты, hits, страницы, отклонённые варианты), `created_at`, `updated_at`.

- `POST /instruction` сначала смотрит в хранилище: ручная запись возвращается всегда, автоматическая — если моложе `TTL_DAYS`. `refresh: true` пересобирает и перезаписывает.
- Ручная правка: `PUT /instructions/{host}`. Такие записи не пересобираются автоматически — это способ зафиксировать инструкцию для сайта, где автомат ошибается.
- `POST /parse` без `instruction` берёт сохранённую.

Реализация — PostgreSQL (`app/db/models.py`, `store/instructions.py`). Схема накатывается Alembic (`alembic upgrade head`). В кластере в секрет/helm задайте `APP_CONFIG__DB__URL` на БД `parser`.

## Миграции

Из корня репозитория, с заполненным `APP_CONFIG__DB__URL` в `app/.env`:

```bash
poetry run alembic upgrade head
```

Проверка: `poetry run alembic current`.

## Docker

```bash
cp app/.env.template app/.env
docker compose build
docker compose up
```

Образ запускает:

```text
uvicorn app.main:main_app --host 0.0.0.0 --port 8000
```

Секреты агента и DSN берутся из `app/.env` (compose `env_file`). В кластере те же `APP_CONFIG__*` задаются в релизе helmfile. Миграции: Job/`alembic upgrade head` до старта сервиса.

## Структура

```text
app/
  api/           # роуты /api/v1
  agents/        # crawl.py обход, shapes.py формы URL, instruction.py сборка, llm.py клиент модели
  parsers/       # links.py ссылки, html.py обход каталога, paginate.py пейджер, schema.py формат инструкции
  store/         # хранилище инструкций (PostgreSQL)
  db/            # SQLAlchemy-модели
  services/      # pipeline: агент + хранилище + парсер
  clients/http/  # загрузка страниц
  core/          # конфиг
  main.py        # FastAPI (main_app)
alembic/         # миграции схемы
```
