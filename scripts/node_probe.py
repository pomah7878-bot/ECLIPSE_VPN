#!/usr/bin/env python3
"""Только чтение: как мастер-панель 3x-ui отдаёт ноды и привязку inbound'ов к нодам.
Запуск из папки бота: venv/bin/python scripts/node_probe.py
Адреса, ключи и пароли не печатаются."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.requests import get_all_servers  # noqa: E402
from bot.services.vpn_api import get_client  # noqa: E402

SECRET = ("token", "pass", "key", "secret", "address", "host", "url", "ip")


def safe(k, v):
    if any(s in k.lower() for s in SECRET):
        return "<скрыто>"
    return v if len(str(v)) < 60 else str(v)[:57] + "..."


async def main():
    for s in get_all_servers():
        if not s.get("is_active"):
            continue
        c = get_client(s["id"]) if not asyncio.iscoroutinefunction(get_client) else await get_client(s["id"])
        try:
            nodes = await c.get_nodes()
        except Exception as e:  # noqa: BLE001
            print(f"[{s['name']}] ноды: ошибка {e}")
            continue
        if not nodes:
            continue
        print(f"\n=== Мастер: {s['name']} (id {s['id']}) ===")
        print("Ноды (поля):", sorted(nodes[0].keys()))
        for n in nodes:
            print("  ", {k: safe(k, n.get(k)) for k in ("id", "name", "status", "enable", "enabled") if k in n})
        inbs = await c._get_all_inbounds()
        print("Поля inbound'а:", sorted(inbs[0].keys()) if inbs else "нет")
        for i in inbs:
            nk = {k: safe(k, v) for k, v in i.items() if "node" in k.lower()}
            print("  inbound", i.get("id"), "| enable:", i.get("enable"), "|", nk, "|", str(i.get("remark"))[:30])


asyncio.run(main())
