#!/bin/bash
# Автообновление ECLIPSE Unlimited VPN с GitHub.
# Проверяет новые коммиты в pomah7878-bot/ECLIPSE_VPN (remote "eclipse-vpn"),
# и если они есть — подтягивает и перезапускает сервисы.
# Ничего не делает, если новых коммитов нет (быстрая, лёгкая проверка).
# Не трогает репозиторий, если на сервере есть несохранённые локальные правки —
# чтобы случайно не потерять что-то, что ещё не закоммичено.

set -e

REPO_DIR="/root/EclipseVPN"
REMOTE="eclipse-vpn"
BRANCH="main"
LOG_FILE="/root/backups/auto_deploy.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

mkdir -p "$(dirname "$LOG_FILE")"
cd "$REPO_DIR"

# Если есть несохранённые изменения — не трогаем, только предупреждаем в лог
if [ -n "$(git status --porcelain)" ]; then
    log "⚠️  Есть несохранённые локальные изменения — автообновление пропущено. Проверьте вручную: git status"
    exit 0
fi

git fetch "$REMOTE" "$BRANCH" --quiet

LOCAL_HEAD=$(git rev-parse HEAD)
REMOTE_HEAD=$(git rev-parse "$REMOTE/$BRANCH")

if [ "$LOCAL_HEAD" == "$REMOTE_HEAD" ]; then
    # Новых коммитов нет — тихо выходим, ничего не логируем (иначе лог быстро распухнет)
    exit 0
fi

log "🔄 Обнаружены новые коммиты: $LOCAL_HEAD -> $REMOTE_HEAD"
log "Список новых коммитов:"
git log --oneline "$LOCAL_HEAD..$REMOTE_HEAD" >> "$LOG_FILE" 2>&1

if ! git merge --ff-only "$REMOTE/$BRANCH" >> "$LOG_FILE" 2>&1; then
    log "❌ git merge --ff-only не удался (история разошлась?) — автообновление остановлено, разберитесь вручную"
    exit 1
fi

log "✅ Код обновлён до $(git rev-parse --short HEAD)"

# Перезапуск сервисов
if systemctl restart eclipse-vpn 2>> "$LOG_FILE"; then
    log "✅ eclipse-vpn перезапущен"
else
    log "❌ Не удалось перезапустить eclipse-vpn — проверьте systemctl status eclipse-vpn"
fi

if systemctl is-enabled eclipse-ai >/dev/null 2>&1; then
    if systemctl restart eclipse-ai 2>> "$LOG_FILE"; then
        log "✅ eclipse-ai перезапущен"
    else
        log "❌ Не удалось перезапустить eclipse-ai"
    fi
fi

log "Автообновление завершено."
