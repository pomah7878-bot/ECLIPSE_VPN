#!/bin/bash
# Автообновление ECLIPSE Unlimited VPN с GitHub.
# Проверяет новые коммиты в pomah7878-bot/ECLIPSE_VPN (remote "origin"),
# и если они есть — подтягивает и перезапускает сервисы.
# Ничего не делает, если новых коммитов нет (быстрая, лёгкая проверка).
# Не трогает репозиторий, если на сервере есть несохранённые локальные правки —
# чтобы случайно не потерять что-то, что ещё не закоммичено.
#
# Защита от битого релиза:
#  1) до перезапуска проверяется синтаксис изменённых .py и ставятся новые зависимости;
#  2) после перезапуска сервисы должны остаться в статусе active;
#  3) при сбое код возвращается на прошлый коммит, а «плохой» коммит запоминается
#     (BAD_FILE) и не применяется повторно, пока в main не появится новый.

set -e

REPO_DIR="${REPO_DIR:-/root/EclipseVPN}"
REMOTE="${REMOTE:-origin}"
BRANCH="main"
LOG_FILE="${LOG_FILE:-/root/backups/auto_deploy.log}"
BAD_FILE="${BAD_FILE:-/root/backups/auto_deploy.bad}"
PY="${PY:-$REPO_DIR/venv/bin/python}"
PIP="${PIP:-$REPO_DIR/venv/bin/pip}"
HEALTH_WAIT="${HEALTH_WAIT:-10}"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

restart_services() {
    systemctl restart eclipse-vpn 2>> "$LOG_FILE" || return 1
    if systemctl is-enabled eclipse-ai >/dev/null 2>&1; then
        systemctl restart eclipse-ai 2>> "$LOG_FILE" || return 1
    fi
}

services_ok() {
    systemctl is-active --quiet eclipse-vpn || return 1
    if systemctl is-enabled eclipse-ai >/dev/null 2>&1; then
        systemctl is-active --quiet eclipse-ai || return 1
    fi
}

# Возврат на прошлый рабочий коммит. $2 = restart — ещё и перезапустить сервисы.
rollback() {
    log "❌ $1 — откат на $(git rev-parse --short "$LOCAL_HEAD")"
    echo "$REMOTE_HEAD" > "$BAD_FILE"
    git reset --hard "$LOCAL_HEAD" >> "$LOG_FILE" 2>&1
    if [ "$2" == "restart" ]; then
        restart_services || log "❌ После отката сервисы не перезапустились — проверьте systemctl status eclipse-vpn"
    fi
    exit 1
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

# Этот коммит уже не запустился — ждём нового, чтобы не перезапускать бота каждые 5 минут
if [ "$REMOTE_HEAD" == "$(cat "$BAD_FILE" 2>/dev/null)" ]; then
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

# До перезапуска: синтаксис изменённых .py и новые зависимости
CHANGED=$(git diff --name-only "$LOCAL_HEAD" HEAD)
PYFILES=$(echo "$CHANGED" | grep '\.py$' || true)
if [ -n "$PYFILES" ] && ! echo "$PYFILES" | xargs -d '\n' "$PY" -m py_compile >> "$LOG_FILE" 2>&1; then
    rollback "Ошибка синтаксиса в новом коде, перезапуск не выполнялся"
fi
if echo "$CHANGED" | grep -qx 'requirements.txt'; then
    if ! "$PIP" install -q -r requirements.txt >> "$LOG_FILE" 2>&1; then
        rollback "Не удалось установить зависимости из requirements.txt, перезапуск не выполнялся"
    fi
    log "✅ Зависимости обновлены"
fi

# Перезапуск сервисов и проверка, что они остались живыми
if restart_services; then
    log "✅ Сервисы перезапущены"
else
    rollback "Не удалось перезапустить сервисы" restart
fi

sleep "$HEALTH_WAIT"
if ! services_ok; then
    rollback "После перезапуска сервис не в статусе active" restart
fi

rm -f "$BAD_FILE"
log "Автообновление завершено."
