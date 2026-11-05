# backend-labs
## Что это за репозиторий

Данный репозиторий предназначен для освоения программы по прокачки знаний бэкенда до Senior+ уровня.

## Структура репозитория

Структура репозитория представляет собой набор выполненных заданий в виде `labs/NN-topic/`:
```
backend-labs/
├── pyproject.toml              ← виртуальный корень: workspace, dev-группа, конфиги ruff/mypy/pytest
├── uv.lock
├── .python-version             ← 3.14
├── Makefile
├── .pre-commit-config.yaml
├── .github/
│   ├── workflows/ci.yml
│   └── dependabot.yml
├── libs/
│   └── labkit/                 ← общая библиотека для всех лаб
│       ├── pyproject.toml
│       ├── src/labkit/{__init__.py, py.typed, settings.py, logging.py}
│       └── tests/
└── labs/
    └── 01-foundation/
        ├── pyproject.toml
        ├── .env.example
        ├── src/foundation_app/{__init__.py, py.typed, config.py, main.py}
        └── tests/
```

## Лабы

| № | Лаба | Тема | Пакет |
|---|---|---|---|
| 01 | [`labs/01-foundation`](labs/01-foundation) | Инженерный фундамент: uv workspace, ruff, mypy, pre-commit, конфиг без утечек секретов, structlog, CI | `foundation-app` |
| 02 | [`labs/02-aggregator`](labs/02-aggregator) | Асинхронный агрегатор предложений: опрос площадок через asyncio, ретраи, дедлайны, отмена задач | `aggregator-app` |
| 03 | [`labs/03-orders`](labs/03-orders) | HTTP API заказов: FastAPI и PostgreSQL, Problem Details, идемпотентность, курсорная пагинация. Подробнее в [README](labs/03-orders/README.md) | `orders-app` |

Общий код всех лаб лежит в [`libs/labkit`](libs/labkit): базовые настройки (`env`, `log_level`, `log_json`) и `configure_logging` с маскированием секретов.

### Быстрый старт
При первом клонировании нужно выполнить команду `make install && make check`

### Makefile команды
- `install`: делает синк `uv workspace` и устанавливает pre-commit хуки
- `fmt`: форматирование кода через `ruff`
- `lint`: линтинг кода через `ruff`
- `typecheck`: тайпчекинг через `mypy`
- `test`: запуск тестов через `pytest`
- `check`: полный запуск проверки (`lint` + `typecheck` + `test`)
- `pre-pr`: подготовка к PR (`fmt` + `lint` + `typecheck`)
