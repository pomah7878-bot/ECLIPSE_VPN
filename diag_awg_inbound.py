#!/usr/bin/env python3
"""Только чтение: показывает пиры inbound AmneziaWG/WireGuard на сервере и
ищет совпадающие адреса. Ничего не меняет.
Запуск: cd /root/EclipseVPN && python3 diag_awg_inbound.py [server_id] [inbound_id]"""
import asyncio, json, sys
from collections import defaultdict

SERVER_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 5
INBOUND_ID = int(sys.argv[2]) if len(sys.argv) > 2 else 298


def ips_of(peer: dict) -> list:
    for k in ("allowedIPs", "allowedIps", "allowed_ips", "allowedIP"):
        v = peer.get(k)
        if v:
            return [str(x) for x in v] if isinstance(v, list) else [x.strip() for x in str(v).split(",")]
    return []


async def main():
    from bot.services.vpn_api import get_client
    client = await get_client(SERVER_ID)
    await client.login()
    inbounds = await client._get_all_inbounds()
    inb = next((i for i in inbounds if int(i.get("id", -1)) == INBOUND_ID), None)
    if not inb:
        print("inbound не найден; доступные:", [(i.get("id"), i.get("protocol"), i.get("remark")) for i in inbounds])
        return
    print(f"inbound {INBOUND_ID}: protocol={inb.get('protocol')} remark={inb.get('remark')!r} port={inb.get('port')}")
    try:
        settings = json.loads(inb.get("settings") or "{}")
    except ValueError:
        settings = {}
    print("ключи settings:", sorted(settings.keys()))
    peers = settings.get("peers") or settings.get("clients") or []
    print("пиров:", len(peers))
    by_ip = defaultdict(list)
    for p in peers:
        name = p.get("email") or p.get("name") or p.get("id") or "?"
        ips = ips_of(p)
        print(f"  {name:40} ips={ips} enable={p.get('enable')}")
        for ip in ips:
            by_ip[ip].append(name)
    dup = {ip: n for ip, n in by_ip.items() if len(n) > 1}
    print("\nАдреса, занятые несколькими пирами:", dup or "нет")
    if peers:
        print("\nполя первого пира:", sorted(peers[0].keys()))

asyncio.run(main())
