#!/usr/bin/env bash
# Проверка перед релизом: ничего личного и секретного не попало в коммит.
# Запуск: bash scripts/check_release.sh   (после `git add -u`, до `git commit`)
#
# Свои «опасные» строки (ваши IP, Telegram ID, домены) НЕ храните в репозитории:
# положите их по одной в строке в файл ~/.eclipse_release_guard (вне проекта).
set -u
cd "$(dirname "$0")/.." || exit 2
bad=0

files=$(git diff --cached --name-only)
if [ -z "$files" ]; then echo "Нет добавленных изменений (сначала git add -u)."; exit 2; fi

hit=$(echo "$files" | grep -iE '\.db($|-)|\.env$|secrets\.env$|(^|/)config\.py$|\.bak|\.pem$|\.tgz$|(^|/)backup/' || true)
if [ -n "$hit" ]; then echo "СТОП: лишние файлы в коммите:"; echo "$hit"; bad=1; fi

diff=$(git diff --cached -U0 | grep -E '^\+' | grep -vE '^\+\+\+' || true)
pat='[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|BEGIN [A-Z ]*PRIVATE KEY|AKIA[0-9A-Z]{16}'
if echo "$diff" | grep -nE "$pat" >/dev/null; then
  echo "СТОП: похоже на токен или приватный ключ:"; echo "$diff" | grep -nE "$pat" | cut -c1-120; bad=1
fi

guard="${ECLIPSE_RELEASE_GUARD:-$HOME/.eclipse_release_guard}"
if [ -f "$guard" ]; then
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    if echo "$diff" | grep -nF -- "$line" >/dev/null; then echo "СТОП: в изменениях есть личная строка из $guard"; bad=1; fi
  done < "$guard"
else
  echo "Подсказка: создайте $guard со своими IP/ID/доменами (по одному в строке) для более строгой проверки."
fi

[ "$bad" = 0 ] && echo "OK: изменения чистые, можно коммитить."
exit "$bad"
