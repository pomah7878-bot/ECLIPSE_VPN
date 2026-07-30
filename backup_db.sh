#!/bin/bash
# Ежедневный бэкап баз данных ECLIPSE Unlimited VPN.
# Использует встроенный SQLite Backup API через python3 (не просто cp!) —
# безопасно даже если бот в этот момент что-то пишет в базу (WAL-режим).

set -e

SRC_DIR="/root/EclipseVPN/database"
BACKUP_DIR="/root/backups/db"
DATE=$(date +%Y-%m-%d_%H-%M-%S)
RETENTION_DAYS=14

mkdir -p "$BACKUP_DIR"

for DB_NAME in vpn_bot.db ai_history.db; do
    SRC="$SRC_DIR/$DB_NAME"
    if [ -f "$SRC" ]; then
        DEST="$BACKUP_DIR/${DB_NAME%.db}_$DATE.db"
        python3 -c "
import sqlite3
src = sqlite3.connect('$SRC')
dst = sqlite3.connect('$DEST')
with dst:
    src.backup(dst)
src.close()
dst.close()
"
        gzip "$DEST"
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Бэкап создан: ${DEST}.gz"
    fi
done

# Удаляем бэкапы старше RETENTION_DAYS дней
find "$BACKUP_DIR" -name "*.db.gz" -mtime +$RETENTION_DAYS -delete

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Готово. Текущие бэкапы:"
ls -lh "$BACKUP_DIR" | tail -10
