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
