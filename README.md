# TD Parser

HTTP API: найти инструкцию скрейпа сайта производителя и собрать URL товаров.

Стек: Python 3.12, FastAPI, httpx, Poetry, Yandex LLM (OpenAI-compatible Responses API).

Интерактивная спецификация после запуска: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) (`/redoc`, `/openapi.json`).

Базы данных нет. Состояние не хранится: каждый запрос заново качает HTML.

## Требования

- Python 3.12+
- [Poetry](https://python-poetry.org/)
- ключ Yandex AI для `POST /api/v1/instruction` (parse работает и без него)

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

| Переменная | Назначение |
|------------|------------|
| `APP_CONFIG__RUN__HOST` | хост uvicorn |
| `APP_CONFIG__RUN__PORT` | порт |
| `APP_CONFIG__FETCH__TIMEOUT` | таймаут HTTP к сайтам |
| `APP_CONFIG__FETCH__VERIFY_SSL` | проверка TLS сайтов |
| `APP_CONFIG__AGENT__API_KEY` | ключ Yandex AI |
| `APP_CONFIG__AGENT__FOLDER_ID` | folder / project |
| `APP_CONFIG__AGENT__BASE_URL` | `https://ai.api.cloud.yandex.net/v1` |
| `APP_CONFIG__AGENT__MODEL` | короткое имя или `gpt://folder/model` |

## API

Базовый префикс: `/api/v1`. Авторизации нет. Тела — JSON. Запросы долгие (LLM + обход сайта): таймаут клиента 2–5 минут.

| Метод | Путь | Тело | Ответ |
|--------|------|------|--------|
| `POST` | `/api/v1/instruction` | `{ "url": "https://…" }` | JSON инструкции |
| `POST` | `/api/v1/parse` | `{ "url": "https://…", "instruction": { … } }` | список товаров |

`url` снаружи — корень сайта. `instruction.url` — путь каталога (`/products`).

Ошибки: `{ "detail": "…" }`. 422 — кривое тело, 503 — нет LLM, 502 — сайт / модель / пустой regex.

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

Секреты агента берутся из `app/.env` (compose `env_file`). В кластере те же `APP_CONFIG__*` задаются в релизе helmfile.

## Структура

```text
app/
  api/           # роуты /api/v1
  agents/        # LLM → инструкция
  parsers/       # HTML → товары
  clients/http/  # загрузка страниц
  core/          # конфиг
  main.py        # FastAPI (main_app)
```
