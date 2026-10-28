#!/usr/bin/env python3
"""Только чтение: проверяет список IP на блокировку (cheburcheck) и показывает чистые.
Запуск из папки бота: venv/bin/python scripts/ip_pick.py 203.0.113.10 203.0.113.20 ..."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot.services import block_check as bc  # noqa: E402


async def main(ips):
    for ip in ips:
        res = await bc.check_ip(ip)
        icon, text = bc.verdict_line(res)
        extra = " | в списке заблокированных подсетей" if res.get("listed") else ""
        print(icon, ip, "-", text + extra)
        await asyncio.sleep(bc.PAUSE)

asyncio.run(main(sys.argv[1:]))
