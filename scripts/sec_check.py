#!/usr/bin/env python3
"""Только чтение: проверка безопасности сервера бота. Ничего не меняет.
Запуск из папки бота: python3 scripts/sec_check.py https://example.com
Секреты, пароли и токены не печатаются."""
import os
import re
import stat
import subprocess
import sys
import urllib.request

SITE = (sys.argv[1] if len(sys.argv) > 1 else "").rstrip("/")
BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30).stdout
    except Exception as e:  # noqa: BLE001
        return f"(ошибка: {e})"


def line(icon, text):
    print(f"{icon} {text}")


print("=== Открытые порты (слушают на всех адресах) ===")
for l in sh("ss -ltnupH").splitlines():
    parts = l.split()
    local = parts[4] if len(parts) > 4 else ""
    proc = re.search(r'\("([^"]+)"', l)
    name = proc.group(1) if proc else "?"
    if local.startswith(("127.", "[::1]", "localhost")):
        continue
    print(f"   {parts[0]:4} {local:28} {name}")
print("   (порты бота/AI/ollama должны слушать только 127.0.0.1, наружу — только nginx, ssh, xray)")

print("\n=== Файрвол ===")
ufw = sh("ufw status 2>/dev/null")
line("🟢" if "Status: active" in ufw else "🔴", "ufw: " + (ufw.splitlines()[0] if ufw else "не установлен"))

print("\n=== SSH ===")
cfg = sh("sshd -T 2>/dev/null")
def val(k):
    m = re.search(rf"^{k} (\S+)", cfg, re.M)
    return m.group(1) if m else "?"
pa, prl = val("passwordauthentication"), val("permitrootlogin")
line("🟢" if pa == "no" else "🟡", f"Вход по паролю: {pa} (лучше no — только ключи)")
line("🟢" if prl in ("no", "prohibit-password", "without-password") else "🟡", f"Root-вход: {prl} (лучше prohibit-password)")
f2b = sh("fail2ban-client status sshd 2>/dev/null")
m = re.search(r"Total banned:\s*(\d+)", f2b)
line("🟢" if f2b else "🟡", f"fail2ban sshd: {'работает, забанено всего ' + m.group(1) if m else 'не настроен'}")

print("\n=== Права на файлы с секретами ===")
for rel in ("config.py", "secrets.env", ".env", "database/vpn_bot.db"):
    p = os.path.join(BOT_DIR, rel)
    if os.path.exists(p):
        mode = stat.S_IMODE(os.stat(p).st_mode)
        ok = not (mode & 0o007)
        line("🟢" if ok else "🔴", f"{rel}: {oct(mode)}" + ("" if ok else " — читается всеми, выполните: chmod 600 " + p))

print("\n=== Обновления безопасности ОС ===")
up = sh("apt list --upgradable 2>/dev/null | grep -ci security")
line("🟢" if up.strip() in ("", "0") else "🟡", f"Ожидают обновлений безопасности: {up.strip() or 0}")

if SITE:
    print(f"\n=== Заголовки сайта {SITE} ===")
    try:
        req = urllib.request.Request(SITE + "/shop", headers={"User-Agent": "sec-check"})
        h = urllib.request.urlopen(req, timeout=15).headers
        for name, why in (
            ("Strict-Transport-Security", "HSTS: браузер всегда ходит по https"),
            ("X-Content-Type-Options", "nosniff"),
            ("Content-Security-Policy", "защита от встраивания и XSS"),
            ("X-Frame-Options", "защита от встраивания в чужой сайт (кликджекинг)"),
        ):
            line("🟢" if h.get(name) else "🟡", f"{name}: {'есть' if h.get(name) else 'нет'} — {why}")
        srv = h.get("Server", "")
        line("🟡" if re.search(r"\d", srv) else "🟢", f"Server: {srv or '(скрыт)'} — версию лучше скрыть (server_tokens off)")
    except Exception as e:  # noqa: BLE001
        line("⚪", f"сайт не открылся: {e}")
    for path in ("/.git/config", "/config.py", "/database/vpn_bot.db", "/secrets.env"):
        try:
            code = urllib.request.urlopen(SITE + path, timeout=10).status
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception:  # noqa: BLE001
            code = "—"
        line("🔴" if code == 200 else "🟢", f"{path}: {code} (должно быть 404/403)")
