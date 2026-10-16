#!/usr/bin/env bash
# Запуск виртуального дисплея, VNC/noVNC и самого Mount File.
set -euo pipefail
cd "$(dirname "$0")/.."
export DISPLAY=:1

echo "Останавливаю старые процессы (если были)..."
pkill -f "Xvfb :1" 2>/dev/null || true
pkill -f "x11vnc" 2>/dev/null || true
pkill -f "websockify" 2>/dev/null || true
pkill -f "main.py" 2>/dev/null || true
sleep 1

echo "Запускаю Xvfb (виртуальный дисплей :1, 1280x800)..."
Xvfb :1 -screen 0 1280x800x24 &
sleep 2

echo "Запускаю рабочий стол XFCE..."
startxfce4 >/dev/null 2>&1 &

echo "Запускаю VNC-мост (x11vnc + noVNC на порту 6080)..."
x11vnc -display :1 -nopw -forever -shared -bg
websockify --web=/usr/share/novnc 6080 localhost:5900 &
sleep 2

echo "Запускаю Mount File с тестовым образом..."
[ -f test.img ] || { dd if=/dev/zero of=test.img bs=1M count=64; mkfs.vfat -F 32 test.img; }
./.venv/bin/python main.py test.img &

sleep 3
cat <<'EOF'

  Готово! Дальше:
  1. В Codespaces откройте вкладку "Ports"
  2. Найдите порт 6080
  3. Нажмите значок глобуса (Open in browser)
  4. В открывшемся окне noVNC нажмите "Connect" — увидите рабочий стол с Mount File

EOF
