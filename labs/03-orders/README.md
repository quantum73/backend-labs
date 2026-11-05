# HTTP API cервис заказов маркетплейса на FastAPI и PostgreSQL.

Создание заказа с идемпотентностью по `Idempotency-Key`, чтение заказа и курсорная пагинация списка заказов клиента.
Все ошибки отдаются в формате Problem Details (RFC 9457).

## Как запустить

Установить зависимости репозитория (`make install` в корне) и `Docker`.

**1. PostgreSQL** <br>
Запуск из compose (порт `5433`, чтобы не конфликтовать с другими контейнерами):
```bash
docker compose -f labs/03-orders/compose.yaml up -d --wait
```

**2. Сервер.**<br>
Настройки читаются из переменных окружения `APP_*`, а при локальном запуске ещё и из файла `.env` в текущей папке.
Проще всего скопировать пример:
```bash
cd labs/03-orders
cp .env.example .env
uv run uvicorn --factory orders_app.entrypoint:entrypoint --port 8000 --log-config log_config.json --no-access-log
```
Для нескольких процессов: `--workers 4`.<br>
`--log-config` переводит в JSON и логи самого сервера, включая строки родительского процесса при нескольких воркерах.
`--no-access-log` убирает access log uvicorn: приложение пишет свой, с `request_id` и шаблоном маршрута.
Granian запускается с тем же конфигом:
`uv run granian --interface asgi --factory --workers 4 --log-config log_config.json orders_app.entrypoint:entrypoint`.<br>
Фабрика именно `entrypoint`, а не `create_app`:
точка входа читает настройки и настраивает логирование один раз на процесс (см. [ключевые решения](#ключевые-решения)).

Документация API: http://127.0.0.1:8000/docs, схема: http://127.0.0.1:8000/openapi.json.

**3. Тесты**<br>
Запускаются из корня репозитория:
```bash
make check
```
`PostgreSQL` для тестов поднимается сам через `testcontainers` (нужен запущенный `Docker`).
Для частых прогонов быстрее использовать БД из compose:
```bash
ORDERS_TEST_DATABASE_URL=postgresql+asyncpg://orders:orders@localhost:5433/orders uv run pytest labs/03-orders
```

**4. Проверка гонки между процессами.**<br>
При запущенном сервере с несколькими воркерами:
```bash
uv run python labs/03-orders/scripts/concurrency_check.py
```
Скрипт отправляет 40 параллельных `POST` с одним ключом и завершается с кодом 1,
если результат отличается от ожидаемого: все `201`, один `id`, 39 повторов, один заказ у клиента.

**5. Снимок OpenAPI.**<br>
Схема API хранится в [`openapi.json`](openapi.json), так изменения контракта видны в diff PR.
После изменения API обнови её из корня репозитория: `make openapi` (входит в `make pre-pr`).
CI проверяет, что снимок соответствует коду, и прогоняет `oasdiff breaking` против версии из `main`:
ломающее изменение контракта роняет PR.

## Переменные окружения

Все переменные имеют префикс `APP_`.
Неверное значение останавливает старт с событием `config_invalid` в логе и кодом выхода 2.
Значения при этом в лог не попадают.

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `APP_DATABASE_URL` | — (обязательна) | DSN `PostgreSQL`, только драйвер `postgresql+asyncpg`. Секрет: в логах пароль скрыт |
| `APP_DB_POOL_SIZE` | `10` | Соединений в пуле на процесс, без overflow. Нужно, чтобы `воркеры × pool_size × поды < max_connections` с запасом |
| `APP_DB_STATEMENT_TIMEOUT_MS` | `2000` | Предельное время одного SQL-запроса на стороне PostgreSQL |
| `APP_DB_LOCK_TIMEOUT_MS` | `1000` | Предельное ожидание блокировки. Должен быть меньше `statement_timeout`: повтор, который ждёт оригинал, получает `409`, а не `500` |
| `APP_DB_CONNECT_TIMEOUT_S` | `1.0` | Таймаут подключения к `PostgreSQL` (у `asyncpg` по умолчанию `60 с`) |
| `APP_DB_POOL_TIMEOUT_S` | `1.0` | Ожидание свободного соединения из пула, после него — `503` |
| `APP_IDEMPOTENCY_TTL_S` | `86400` | Сколько хранится ключ идемпотентности (не меньше `60`) |
| `APP_PAGE_DEFAULT_LIMIT` | `20` | Размер страницы списка по умолчанию (`1..100`) |
| `APP_MAX_REQUEST_BODY_BYTES` | `65536` | Предельный размер тела запроса, больше — `413` |
| `APP_ENV` | `local` | `local`, `test`, `staging` или `production` |
| `APP_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` или `ERROR` |
| `APP_LOG_JSON` | `true` | JSON-логи; `false` — читаемый вывод для консоли |

Худший случай обращения к БД с умолчаниями: пул 1 с + подключение 1 с + запрос 2 с = 4 с.
Это меньше обычного таймаута клиента и балансировщика (5–10 с).

## Эндпоинты

| Метод и путь | Что делает | Основные ответы |
|---|---|---|
| `POST /v1/orders` | Создаёт заказ. Заголовок `Idempotency-Key` обязателен (`^[A-Za-z0-9_-]{8,64}$`) | `201` + `Location`; повтор с тем же ключом и телом — тот же ответ и `Idempotent-Replayed: true`; `400` нет ключа; `409` оригинал ещё выполняется; `413`; `422` невалидное тело или ключ с другим телом |
| `GET /v1/orders/{order_id}` | Заказ по id | `200`, `404`, `422` |
| `GET /v1/orders?customer_id=&limit=&cursor=` | Заказы клиента, новые сверху. `customer_id` обязателен | `200` с `next_cursor` (`null`, когда дальше пусто); `400` битый или чужой курсор; `422` |
| `GET /livez` | Процесс жив. БД не трогает | `200` |
| `GET /readyz` | Готов принимать трафик: БД отвечает и схема создана | `200`, `503` |

Любая ошибка — `application/problem+json` с полями `type`, `title`, `status`, `instance`,
при необходимости `detail` и `errors`.<br>
Клиенту стоит ветвиться по `type`, а не по тексту. <br>
При недоступной БД маршруты `/v1/orders…` отвечают `503` с `Retry-After`.<br>
В каждом ответе есть заголовок `x-request-id`.

`/livez` и `/readyz` не входят в `OpenAPI`: это интерфейс к инфраструктуре (kubelet, балансировщик), а не к клиентам API.

## Ключевые решения

- **Фабрика без побочных эффектов, логирование в точке входа.**
Импорт и `create_app` не трогают окружение и не настраивают логи: настройка логирования глобальна для процесса.
Это делает [`entrypoint`](src/orders_app/entrypoint.py#L15), почему — в docstring [`create_app`](src/orders_app/main.py#L23).
- **Старт без БД: процесс жив, но не готов.**
Падение при старте в k8s даёт `CrashLoopBackOff` с растущими паузами, поэтому приложение стартует и ждёт БД.
Пока БД не отвечает или схема не создана, `/readyz` отдаёт `503`, и трафик на под не идёт:
[`lifespan`](src/orders_app/main.py#L42), [`readyz`](src/orders_app/health.py#L23), [`Database`](src/orders_app/db.py#L77).
- **Зависимости через Dishka, commit до ответа.**
Engine и настройки живут в `Scope.APP`, соединение — в `Scope.REQUEST`: [`InfraProvider`](src/orders_app/di.py#L12).
Провайдер соединения только выдаёт и закрывает его, а транзакцию открывает и коммитит сервис (`async with conn.begin()`).
Commit в finalizer провайдера выполнился бы уже после отправки ответа, и упавший commit клиент увидел бы как `201`.
Почему так — в docstring [`connection`](src/orders_app/di.py#L36), тест `test_commit_failure_before_response` это ловит.
Ошибки подключения переводятся в `503` там же, на границе с БД.
- **Идемпотентность claim-first.**
Первым действием транзакции ключ занимается через `INSERT … ON CONFLICT`.
Параллельные повторы ждут на уникальном индексе и получают сохранённый ответ;
повтор, не дождавшийся оригинала за `lock_timeout`, получает `409` с `Retry-After`.
Ключ, заказ и ответ пишутся в одной транзакции.
Почему именно так и что изменилось бы с внешним платёжным API — в docstring [`idempotent_create_order`](src/orders_app/orders/services.py#L110).
Отпечаток тела — sha256 каноничного JSON валидированной модели: [`request_fingerprint`](src/orders_app/orders/services.py#L25).
- **Курсорная пагинация.**
Keyset по `(created_at, id)` с `LIMIT limit+1`, без `OFFSET` и `COUNT`;
план — Index Scan по `ix_orders_customer_created_id` без `Sort`.
Курсор — версионированный base64url JSON, привязанный к отпечатку фильтров.
Его разбирает строгая pydantic-модель, поэтому битый курсор даёт `400`, а не `500`.
Почему подпись не нужна — в docstring [`decode_cursor`](src/orders_app/core/pagination.py#L66).
Позиции заказов страницы загружаются одним запросом с `IN`, без N+1: [`_get_order_items`](src/orders_app/orders/services.py#L31).
- **Ошибки по RFC 9457.**
Сервисный код бросает прикладные ошибки, обработчики превращают их в problem+json
и никогда не отдают клиенту внутренние детали: [`core/errors.py`](src/orders_app/core/errors.py#L1).
В OpenAPI все ошибки ссылаются на одну схему `ProblemDetails`,
а примеры строятся из классов ошибок: [`core/openapi.py`](src/orders_app/core/openapi.py#L1).
- **Один формат логов.**
Логи приложения, uvicorn, granian и библиотек идут одним JSON-потоком с маскированием секретов:
`labkit.configure_logging` подключает `structlog.stdlib.ProcessorFormatter` к root-логгеру,
а [`log_config.json`](log_config.json) применяет тот же форматер ещё до загрузки приложения.
- **Ограничения на входе.**
Тело запроса больше лимита отклоняется до чтения в память: [`BodySizeLimitMiddleware`](src/orders_app/core/middlewares/body_limit.py#L15).
Таймауты подключения, пула, запроса и блокировок описаны в [`OrdersSettings`](src/orders_app/settings.py#L20).

## Ограничения учебной версии

- **Схема создаётся `metadata.create_all` при старте.**
В продакшене её меняют миграции отдельным шагом деплоя (Модуль 7): `create_all` не меняет существующие таблицы,
а при нескольких процессах и rolling update запускается одновременно.
- **Истёкшие ключи идемпотентности не удаляются.** Индекс по `expires_at` есть, периодической очистки нет.
- **Нет ограничения частоты запросов** от одного клиента и общего дедлайна на запрос.
