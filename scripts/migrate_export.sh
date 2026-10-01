#!/bin/bash
# ECLIPSE Unlimited VPN — экспорт для переезда на другой сервер.
# Запуск на СТАРОМ сервере:  bash scripts/migrate_export.sh [каталог_для_архива]
# Бота останавливать не нужно: база копируется через SQLite Backup API.
# Инструкция: MIGRATION.md

set -euo pipefail

APP_DIR="${APP_DIR:-/root/EclipseVPN}"
OUT_DIR="${1:-/root}"
STAMP="$(date +%Y-%m-%d_%H-%M-%S)"
NAME="eclipse_migration_${STAMP}"
WORK="$(mktemp -d)"
PKG="$WORK/$NAME"
trap 'rm -rf "$WORK"' EXIT

ok()   { echo -e "\033[0;32m[✓]\033[0m $1"; }
warn() { echo -e "\033[1;33m[!]\033[0m $1"; }
err()  { echo -e "\033[0;31m[✗]\033[0m $1"; exit 1; }

[ -d "$APP_DIR" ] || err "Не найден каталог бота: $APP_DIR (задайте APP_DIR=...)"
[ -f "$APP_DIR/database/vpn_bot.db" ] || err "Не найдена база $APP_DIR/database/vpn_bot.db"
command -v python3 >/dev/null || err "Нужен python3"

mkdir -p "$PKG/files" "$PKG/system" "$OUT_DIR"

# 1. Базы данных (консистентный снимок)
snapshot() {
    python3 - "$1" "$2" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
src.close(); dst.close()
if ok != "ok":
    sys.exit("integrity_check: " + ok)
PY
}
mkdir -p "$PKG/files/database"
snapshot "$APP_DIR/database/vpn_bot.db" "$PKG/files/database/vpn_bot.db" || err "База не прошла проверку целостности"
ok "База vpn_bot.db скопирована и проверена"
if [ -f "$APP_DIR/database/ai_history.db" ]; then
    snapshot "$APP_DIR/database/ai_history.db" "$PKG/files/database/ai_history.db" && ok "База ai_history.db скопирована"
fi

# 2. Конфигурация и секреты
for f in config.py secrets.env; do
    if [ -f "$APP_DIR/$f" ]; then cp "$APP_DIR/$f" "$PKG/files/$f"; ok "$f"; else warn "$f не найден — пропускаю"; fi
done

# 3. Брендинг (логотип и иконки, загруженные админом — не в git)
mkdir -p "$PKG/files/bot/webapp/static"
for f in logo.png favicon.ico favicon-32x32.png favicon-16x16.png; do
    [ -f "$APP_DIR/bot/webapp/static/$f" ] && cp "$APP_DIR/bot/webapp/static/$f" "$PKG/files/bot/webapp/static/$f" && ok "брендинг: $f"
done

# 4. Свои расширения
if [ -d "$APP_DIR/custom_extensions" ]; then
    cp -a "$APP_DIR/custom_extensions" "$PKG/files/custom_extensions"; ok "custom_extensions/"
fi

# 5. nginx (активные сайты, кроме default) и systemd-юниты
if [ -d /etc/nginx/sites-enabled ]; then
    mkdir -p "$PKG/system/nginx"
    for l in /etc/nginx/sites-enabled/*; do
        [ -e "$l" ] || continue
        b="$(basename "$l")"; [ "$b" = "default" ] && continue
        cp -L "$l" "$PKG/system/nginx/$b" && ok "nginx: $b"
    done
fi
mkdir -p "$PKG/system/systemd"
for u in /etc/systemd/system/eclipse-*.service; do
    [ -f "$u" ] && cp "$u" "$PKG/system/systemd/" && ok "systemd: $(basename "$u")"
done
crontab -l > "$PKG/system/crontab.txt" 2>/dev/null || true
[ -s "$PKG/system/crontab.txt" ] && ok "crontab" || rm -f "$PKG/system/crontab.txt"

# 6. Паспорт переезда
{
    echo "created=$STAMP"
    echo "host=$(hostname)"
    echo "app_dir=$APP_DIR"
    echo "git_commit=$(git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
    echo "git_subject=$(git -C "$APP_DIR" log -1 --format=%s 2>/dev/null || echo unknown)"
    echo "python=$(python3 --version 2>&1)"
} > "$PKG/MANIFEST.txt"

ARCHIVE="$OUT_DIR/$NAME.tar.gz"
tar -C "$WORK" -czf "$ARCHIVE" "$NAME"
chmod 600 "$ARCHIVE"
SUM="$(sha256sum "$ARCHIVE" | cut -d' ' -f1)"

echo
ok "Готово: $ARCHIVE ($(du -h "$ARCHIVE" | cut -f1))"
echo "   SHA256: $SUM"
echo
warn "В архиве токен бота и секреты. Не пересылайте его в мессенджерах, удалите после переезда."
echo "Дальше на новом сервере:  bash scripts/migrate_import.sh $ARCHIVE $SUM"
