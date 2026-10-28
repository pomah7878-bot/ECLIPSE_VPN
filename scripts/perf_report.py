#!/usr/bin/env python3
"""Только чтение: что нагружает бота. Адреса, токены и числа-идентификаторы маскируются.

Запуск из папки бота:  python3 scripts/perf_report.py
Опции: --hours 6 (за сколько часов смотреть журнал), --service eclipse-vpn
"""
import argparse
import collections
import os
import re
import sqlite3
import subprocess
import time

ap = argparse.ArgumentParser()
ap.add_argument("--hours", type=int, default=6)
ap.add_argument("--service", default="eclipse-vpn")
ap.add_argument("--dir", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
args = ap.parse_args()


def sh(cmd, timeout=60):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout).stdout
    except Exception as e:  # noqa: BLE001
        return f"(ошибка: {e})"


def mask(s):
    s = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", "<ip>", s)
    s = re.sub(r"[A-Za-z0-9_\-]{24,}", "<id>", s)
    s = re.sub(r"https?://\S+", "<url>", s)
    s = re.sub(r"\b\d{5,}\b", "<N>", s)
    return re.sub(r"\d+(\.\d+)?", "#", s)[:140]


print("=== Система ===")
print(sh("uptime").strip())
print(sh("free -m | head -3").strip())
pid = sh(f"systemctl show -p MainPID --value {args.service}").strip()
if pid and pid != "0":
    print("Процесс бота (pid, %CPU за всё время, %MEM, RSS МБ, потоков, время CPU):")
    print(sh(f"ps -o pid=,pcpu=,pmem=,rss=,nlwp=,time= -p {pid}").strip().replace("\n", " "))
    print("Открытых файлов/соединений:", sh(f"ls /proc/{pid}/fd 2>/dev/null | wc -l").strip())
print("Запуск сервиса:", sh(f"systemctl show -p ActiveEnterTimestamp --value {args.service}").strip())

print("\n=== База данных ===")
db = os.path.join(args.dir, "database", "vpn_bot.db")
for suffix in ("", "-wal", "-shm"):
    f = db + suffix
    if os.path.exists(f):
        print(f"{os.path.basename(f)}: {os.path.getsize(f) / 1048576:.1f} МБ")
try:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    rows = []
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        try:
            rows.append((con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0], t))
        except sqlite3.Error:
            pass
    print("Самые большие таблицы (строк):", ", ".join(f"{t}={n}" for n, t in sorted(rows, reverse=True)[:10]))
except Exception as e:  # noqa: BLE001
    print("БД не открыта:", e)

print(f"\n=== Журнал за {args.hours} ч ===")
log = sh(f"journalctl -u {args.service} --since '{args.hours} hours ago' --no-pager -o cat", timeout=120)
lines = log.splitlines()
print("Строк:", len(lines), f"(~{len(lines) / max(args.hours * 60, 1):.1f} в минуту)")
levels = collections.Counter()
for l in lines:
    m = re.search(r"\b(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b", l)
    if m:
        levels[m.group(1)] += 1
print("По уровням:", dict(levels))

stalls = [l for l in lines if "Цикл событий не отвечает" in l]
back = [l for l in lines if "Цикл событий снова отвечает" in l]
print("Зависания цикла событий:", len(stalls))
for l in back[-5:]:
    print("  ", mask(l))
print("\nСамые частые сообщения (WARNING/ERROR):")
c = collections.Counter(mask(l) for l in lines if re.search(r"\b(WARNING|ERROR|CRITICAL)\b", l))
for msg, n in c.most_common(15):
    print(f"  {n:>5}× {msg}")
print("\nСамые частые сообщения (любые):")
c = collections.Counter(mask(l) for l in lines)
for msg, n in c.most_common(10):
    print(f"  {n:>5}× {msg}")
slow = [l for l in lines if re.search(r"(мониторинг|ответ за|медленн|timeout|таймаут|deadline)", l, re.I)]
print("\nПризнаки медленных панелей/таймаутов:", len(slow))
for msg, n in collections.Counter(mask(l) for l in slow).most_common(6):
    print(f"  {n:>5}× {msg}")

print("\n=== Диск ===")
print(sh(f"du -sh {args.dir}/logs {args.dir}/database 2>/dev/null").strip())
print(time.strftime("Снято: %Y-%m-%d %H:%M"))
