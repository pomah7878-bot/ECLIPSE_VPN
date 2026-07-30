#!/bin/bash
set -euo pipefail

# Защита от главной причины прошлых сбоев: если бот и AI уже управляются
# systemd — НИКОГДА не запускать ещё одну копию вручную поверх них.
# Ручной запуск создаёт неуправляемый "осиротевший" процесс, который держит
# порт/сессию Telegram и не даёт нормально работать systemd-версии.
if systemctl is-active --quiet eclipse-vpn 2>/dev/null || systemctl is-active --quiet eclipse-ai 2>/dev/null; then
    echo "❌ Сервисы eclipse-vpn/eclipse-ai уже запущены через systemd."
    echo "   НЕ запускайте этот скрипт поверх них — так появляются 'осиротевшие'"
    echo "   процессы, которые ломают работу бота (двойной Telegram-поллинг,"
    echo "   занятый порт 8086 и т.п.)."
    echo ""
    echo "   Что сделать вместо этого:"
    echo "     systemctl restart eclipse-vpn   # перезапустить бота"
    echo "     systemctl restart eclipse-ai    # перезапустить AI-сервис"
    echo ""
    echo "   Если вам действительно нужно запустить вручную для локальной"
    echo "   отладки — сначала остановите systemd-версии:"
    echo "     systemctl stop eclipse-vpn eclipse-ai"
    exit 1
fi

# Активируем venv
source venv/bin/activate

# Секреты берём из окружения или из локального secrets.env (не коммитится в git).
# НИКОГДА не храните реальные ключи прямо в этом файле — он отслеживается git.
if [ -f "secrets.env" ]; then
    set -a
    source secrets.env
    set +a
fi

export BOT_DB_PATH="database/vpn_bot.db"

if [ -z "${GROQ_API_KEY:-}" ] || [ -z "${SUPPORT_API_TOKEN:-}" ]; then
    echo "❌ Не заданы GROQ_API_KEY и/или SUPPORT_API_TOKEN."
    echo "   Задайте их через переменные окружения или создайте файл secrets.env"
    echo "   (см. secrets.env.example), например:"
    echo "     GROQ_API_KEY=ваш_ключ"
    echo "     SUPPORT_API_TOKEN=ваш_токен"
    exit 1
fi

# Запустим AI-сервис в фоне
echo "🚀 Запуск AI сервиса на порту 8086..."
nohup uvicorn ai_support_main:app --host 127.0.0.1 --port 8086 > logs/ai_service.log 2>&1 &
AI_PID=$!

# Регистрируем очистку ДО запуска бота, чтобы AI-сервис
# гарантированно завершался и при обычном выходе, и при Ctrl+C/kill
trap "kill $AI_PID 2>/dev/null" EXIT

sleep 2

echo "🚀 Запуск Telegram бота..."
python main.py
