# Contributing

1. Форк → ветка `feat/my-feature`.
2. On Debian/Ubuntu, run `./scripts/bootstrap-tools.sh` for the complete
   Python and filesystem-checker environment; use `SKIP_SYSTEM_TOOLS=1` on
   Windows/macOS and install the platform tools with the native package
   manager. The Python-only alternative is `pip install -r requirements-all.txt`.
3. `pre-commit install`.
4. Тесты в `tests/`, коммиты в [Conventional Commits](https://www.conventionalcommits.org/).
5. PR.

Перед пушем полезно локально прогнать то же, что и CI:

```bash
make lint
make test
```

The bootstrap deliberately installs tools from manifests rather than storing
platform binaries in Git. Verify an installed environment with:

```bash
python3 scripts/bootstrap-tools.py
```
