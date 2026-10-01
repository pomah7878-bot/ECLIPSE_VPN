#!/bin/bash
# ECLIPSE Unlimited VPN — импорт данных на НОВОМ сервере после install.sh.
# Запуск:  bash scripts/migrate_import.sh /root/eclipse_migration_ДАТА.tar.gz [SHA256]
# Бота не запускает — запуск вручную после переключения DNS (см. MIGRATION.md, шаг 5).

set -euo pipefail

APP_DIR="${APP_DIR:-/root/EclipseVPN}"
ARCHIVE="${1:-}"
EXPECT_SUM="${2:-}"
STAMP="$(date +%Y-%m-%d_%H-%M-%S)"
SAFE="/root/eclipse_pre_migration_${STAMP}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

ok()   { echo -e "\033[0;32m[✓]\033[0m $1"; }
warn() { echo -e "\033[1;33m[!]\033[0m $1"; }
err()  { echo -e "\033[0;31m[✗]\033[0m $1"; exit 1; }

[ -n "$ARCHIVE" ] && [ -f "$ARCHIVE" ] || err "Использование: bash scripts/migrate_import.sh АРХИВ.tar.gz [SHA256]"
[ -d "$APP_DIR" ] || err "Не найден $APP_DIR — сначала выполните install.sh (см. MIGRATION.md, шаг 3)"

if [ -n "$EXPECT_SUM" ]; then
    SUM="$(sha256sum "$ARCHIVE" | cut -d' ' -f1)"
    [ "$SUM" = "$EXPECT_SUM" ] || err "SHA256 не совпал — архив повреждён при передаче"
    ok "SHA256 совпал"
fi

tar -C "$WORK" -xzf "$ARCHIVE"
SRC="$(find "$WORK" -maxdepth 1 -mindepth 1 -type d -name 'eclipse_migration_*' | head -1)"
[ -n "$SRC" ] && [ -d "$SRC/files" ] || err "Это не архив миграции ECLIPSE"
echo "--- MANIFEST ---"; cat "$SRC/MANIFEST.txt" 2>/dev/null || true; echo "----------------"

systemctl stop eclipse-vpn 2>/dev/null || true
systemctl stop eclipse-ai 2>/dev/null || true

# Страховка: что было на новом сервере до импорта
mkdir -p "$SAFE"
cd "$SRC/files"
find . -type f | while read -r f; do
    if [ -e "$APP_DIR/$f" ]; then mkdir -p "$SAFE/$(dirname "$f")"; cp -a "$APP_DIR/$f" "$SAFE/$f"; fi
done
ok "Прежние файлы сохранены в $SAFE"

# Восстановление
mkdir -p "$APP_DIR/database"
rm -f "$APP_DIR/database/vpn_bot.db-wal" "$APP_DIR/database/vpn_bot.db-shm" \
      "$APP_DIR/database/ai_history.db-wal" "$APP_DIR/database/ai_history.db-shm"
cp -a "$SRC/files/." "$APP_DIR/"
chmod 600 "$APP_DIR/config.py" "$APP_DIR/secrets.env" 2>/dev/null || true
ok "Файлы бота восстановлены (база, config.py, secrets.env, брендинг, расширения)"

# Проверка базы
python3 - "$APP_DIR/database/vpn_bot.db" <<'PY' || err "Импортированная база не прошла проверку"
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
n = c.execute("select count(*) from users").fetchone()[0]
print(f"   пользователей в базе: {n}")
PY
ok "База проверена"

# nginx
if [ -d "$SRC/system/nginx" ] && command -v nginx >/dev/null 2>&1; then
    for f in "$SRC"/system/nginx/*; do
        [ -f "$f" ] || continue
        b="$(basename "$f")"
        cp "$f" "/etc/nginx/sites-available/$b"
        ln -sf "/etc/nginx/sites-available/$b" "/etc/nginx/sites-enabled/$b"
        ok "nginx: $b"
    done
    if nginx -t 2>/dev/null; then
        systemctl reload nginx && ok "nginx перезагружен"
    else
        warn "nginx -t не прошёл — вероятно, нет сертификатов Let's Encrypt (MIGRATION.md, шаг 4). Конфиги лежат на месте."
    fi
elif [ -d "$SRC/system/nginx" ]; then
    warn "nginx не установлен — поставьте: apt-get install -y nginx certbot python3-certbot-nginx, затем повторите этот скрипт"
fi

if [ -s "$SRC/system/crontab.txt" ]; then
    cp "$SRC/system/crontab.txt" "$SAFE/old_server_crontab.txt"
    warn "На старом сервере были задания cron (сохранены в $SAFE/old_server_crontab.txt). Добавьте нужные через crontab -e:"
    cat "$SRC/system/crontab.txt"
fi

systemctl daemon-reload 2>/dev/null || true
echo
ok "Импорт завершён. Бот НЕ запущен."
echo "Дальше (MIGRATION.md, шаг 5): остановите бота на старом сервере, переключите DNS, затем:"
echo "   systemctl start eclipse-vpn && systemctl is-active eclipse-vpn"
