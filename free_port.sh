#!/bin/bash
# free_port.sh <порт> — если порт занят посторонним процессом, освобождает его.
# Используется как ExecStartPre в systemd-юнитах, чтобы сервис никогда не
# падал в бесконечный цикл рестартов из-за "address already in use",
# оставшегося от случайно не закрытого ручного запуска.

PORT="$1"
if [ -z "$PORT" ]; then
    exit 0
fi

PID=$(ss -tlnp 2>/dev/null | grep ":${PORT} " | grep -oP 'pid=\K[0-9]+' | head -1)

if [ -n "$PID" ]; then
    echo "free_port.sh: порт $PORT занят процессом $PID — освобождаю перед запуском"
    kill "$PID" 2>/dev/null
    sleep 1
    # Если процесс не завершился по-хорошему за секунду — добиваем
    kill -9 "$PID" 2>/dev/null
fi

exit 0
