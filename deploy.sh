#!/usr/bin/env bash
# Установка обновления с проверкой и автооткатом.
#   ./deploy.sh /root/apply_cdn_v1365.py 1.365 "заголовок" ["+ строка описания" ...]
# Что делает: pull → применяет скрипт → проверяет (нет удалений, код компилируется) →
# коммит только изменённых файлов → push → перезапуск сервисов → ждёт запуска бота;
# если бот не поднялся — сам откатывает коммит (revert + push) и перезапускает.
set -u
APPLY="${1:?укажите файл apply_cdn_vX.py}"; VER="${2:?укажите версию, например 1.365}"; TITLE="${3:?укажите заголовок}"
shift 3
BODY="$(printf '%s\n' "$@")"
SERVICES="${DEPLOY_SERVICES:-eclipse-vpn eclipse-ai}"
MARKER="${DEPLOY_MARKER:-Run polling}"     # строка в журнале eclipse-vpn при успешном запуске
WAIT="${DEPLOY_WAIT:-45}"

APPLY="$(readlink -f "$APPLY")"   # путь считаем до смены папки
cd "$(dirname "$(readlink -f "$0")")" || exit 1
fail() { echo "❌ $*"; exit 1; }
restart() { systemctl restart $SERVICES; }

[ -f "$APPLY" ] || fail "нет файла $APPLY"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail "это не git-папка"
[ -z "$(git status --porcelain --untracked-files=no)" ] || { git status --short --untracked-files=no; fail "в папке есть незакоммиченные изменения — разберитесь с ними и повторите"; }
git pull --ff-only origin main || fail "git pull не прошёл"

BEFORE="$(git ls-files --others --exclude-standard | sort)"
python3 "$APPLY" || fail "скрипт обновления не применился (он сам откатил свои изменения)"
NEW="$(comm -13 <(printf '%s\n' "$BEFORE") <(git ls-files --others --exclude-standard | sort))"

undo() { git checkout -- . ; [ -n "$NEW" ] && printf '%s\n' "$NEW" | xargs -r rm -f; }
if git status --porcelain --untracked-files=no | grep -qE '^( D|D )'; then
    git status --short --untracked-files=no | grep -E '^( D|D )'
    undo; fail "обновление удаляет файлы — это ошибка, всё возвращено как было"
fi
git ls-files '*.py' | xargs python3 -m py_compile || { undo; fail "в коде синтаксическая ошибка, всё возвращено как было"; }

FILES="$(git status --porcelain --untracked-files=no | awk '{print $2}')"
echo "Файлы обновления:"; printf '  %s\n' $FILES $NEW
git add $FILES $NEW
git commit -q -m "Версия $VER: $TITLE" -m "$BODY" || { undo; fail "коммит не создан"; }
git push origin main || { git reset -q --hard HEAD~1; fail "git push не прошёл, коммит отменён локально"; }

START="$(date '+%Y-%m-%d %H:%M:%S')"
restart
OK=0
for _ in $(seq 1 "$WAIT"); do
    sleep 1
    if systemctl is-active --quiet ${SERVICES%% *} && journalctl -u ${SERVICES%% *} --since "$START" 2>/dev/null | grep -q "$MARKER"; then OK=1; break; fi
done
if [ "$OK" != 1 ]; then
    echo "⚠️ бот не поднялся за ${WAIT} с — откатываю обновление"
    git revert --no-edit HEAD >/dev/null && git push origin main && restart
    fail "обновление $VER откатено, бот перезапущен на прежней версии"
fi
echo "✅ Версия $VER установлена, сервисы работают"
if command -v gh >/dev/null 2>&1; then
    gh release create "v$VER" --title "v$VER" --notes "$TITLE" || echo "⚠️ релиз на GitHub не создан — создайте вручную"
fi
