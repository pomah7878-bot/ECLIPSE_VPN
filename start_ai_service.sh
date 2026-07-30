#!/bin/bash

# Защита: не запускать поверх уже работающего systemd-сервиса (см. причину
# в start_all.sh) — особенно важно для этого скрипта, так как он часто
# используется для локальной отладки с --reload.
if systemctl is-active --quiet eclipse-ai 2>/dev/null; then
    echo "❌ eclipse-ai уже запущен через systemd — не запускайте это поверх."
    echo "   Для перезапуска используйте: systemctl restart eclipse-ai"
    echo "   Для локальной отладки сначала: systemctl stop eclipse-ai"
    exit 1
fi

if [ -f "secrets.env" ]; then
    set -a
    source secrets.env
    set +a
fi

export GROQ_API_KEY="${GROQ_API_KEY:-your-groq-api-key}"
export SUPPORT_API_TOKEN="${SUPPORT_API_TOKEN:-your-support-token}"
export BOT_DB_PATH="database/vpn_bot.db"

echo "🚀 Запуск AI сервиса..."
echo "  Порт: 8086"
echo "  GROQ_API_KEY: ${GROQ_API_KEY:0:10}..."
echo "  SUPPORT_API_TOKEN: ${SUPPORT_API_TOKEN:0:10}..."

uvicorn ai_support_main:app --host 127.0.0.1 --port 8086 --reload
