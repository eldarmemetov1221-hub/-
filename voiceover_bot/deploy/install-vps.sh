#!/usr/bin/env bash
# Установка бота как службы systemd на Ubuntu/Debian VPS.
# Ставит ТОЛЬКО базового бота (edge-tts) — легко и безопасно для сайта на том же
# сервере. Клонирование голоса (XTTS) на VPS НЕ ставится намеренно.
#
# Запуск:
#   1) создай файл bot.env со строкой:  BOT_TOKEN=твой_токен
#   2) bash deploy/install-vps.sh
set -e

DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(whoami)"
cd "$DIR"

if [ ! -f bot.env ]; then
  echo "❌ Нет файла bot.env."
  echo "   Создай его так (подставь свой токен):"
  echo "       echo 'BOT_TOKEN=твой_токен' > bot.env"
  echo "   и запусти скрипт снова."
  exit 1
fi

echo "==> Ставлю python venv (если нужно)…"
if ! python3 -m venv --help >/dev/null 2>&1; then
  sudo apt-get update -y
  sudo apt-get install -y python3-venv
fi

echo "==> Создаю venv и ставлю базовые зависимости…"
python3 -m venv venv
./venv/bin/pip install -U pip
./venv/bin/pip install -r requirements.txt

echo "==> Создаю службу systemd…"
sudo tee /etc/systemd/system/voiceover-bot.service >/dev/null <<EOF
[Unit]
Description=Voiceover Telegram bot (edge-tts)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$DIR
EnvironmentFile=$DIR/bot.env
ExecStart=$DIR/venv/bin/python bot.py
Restart=on-failure
RestartSec=5
MemoryMax=400M
CPUWeight=20

[Install]
WantedBy=multi-user.target
EOF

chmod 600 bot.env
sudo systemctl daemon-reload
sudo systemctl enable --now voiceover-bot

echo
echo "✅ Готово! Бот запущен как служба voiceover-bot."
echo "   Статус:   sudo systemctl status voiceover-bot"
echo "   Логи:     journalctl -u voiceover-bot -f"
echo "   Стоп:     sudo systemctl stop voiceover-bot"
echo "   Рестарт:  sudo systemctl restart voiceover-bot"
sudo systemctl --no-pager status voiceover-bot || true
