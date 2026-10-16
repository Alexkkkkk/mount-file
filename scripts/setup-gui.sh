#!/usr/bin/env bash
# Установка GUI-окружения для Mount File в GitHub Codespaces.
# Выполняется один раз при создании codespace (postCreateCommand).
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== [1/4] Системные пакеты ==="
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  xvfb x11vnc xfce4 xfce4-goodies novnc websockify \
  dosfstools python3-pip python3-venv

echo "=== [2/4] Python-зависимости проекта ==="
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

echo "=== [3/4] Тестовый FAT32-образ ==="
if [ ! -f test.img ]; then
  dd if=/dev/zero of=test.img bs=1M count=64
  mkfs.vfat -F 32 test.img
  echo "создан test.img (64 МБ, FAT32)"
else
  echo "test.img уже есть, пропускаю"
fi

echo "=== [4/4] Готово ==="
echo "Запустите GUI:  bash scripts/start-gui.sh"
echo "Затем откройте: Ports -> 6080 -> значок глобуса (noVNC)"
