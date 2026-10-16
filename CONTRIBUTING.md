# Contributing

1. Форк → ветка `feat/my-feature`.
2. `pip install -e ".[dev]"` + `pre-commit install`.
3. Тесты в `tests/`, коммиты в [Conventional Commits](https://www.conventionalcommits.org/).
4. PR.

Перед пушем полезно локально прогнать то же, что и CI:

```bash
make lint
make test
```
