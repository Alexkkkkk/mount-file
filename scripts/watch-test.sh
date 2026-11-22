#!/usr/bin/env bash
# watch-test.sh — перезапуск быстрого тестового прогона после каждого сохранения.
# Цикл «правка -> Ctrl+S -> тесты сами» без внешних пакетов: inotifywait,
# если он установлен, иначе опрос каждые 2 с. Запуск: make watch-test.
# Ctrl+C — остановить. Прогон тот же, что в PR-гейте: make test-fast
# (параллельно, без gui/e2e).
set -u

run() {
  make test-fast
}

echo "Первый прогон..."
run

if command -v inotifywait >/dev/null 2>&1; then
  echo "watch: inotifywait (реакция мгновенная)"
  while :; do
    inotifywait -q -r -e modify,create,delete,move \
      --include '\.py$' mountfile tests main.py
    run
  done
else
  echo "watch: опрос каждые 2с (установите inotifywait для мгновенной реакции)"
  stamp="$(mktemp)"
  touch "$stamp"
  trap 'rm -f "$stamp"' EXIT
  while :; do
    changed="$(find mountfile tests main.py -name '*.py' -newer "$stamp" -print -quit 2>/dev/null)"
    if [ -n "$changed" ]; then
      touch "$stamp"
      run
    fi
    sleep 2
  done
fi
