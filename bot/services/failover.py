"""Резервный узел: пока основной сервер заблокирован, клиенты получают ссылки запасного.

Как работает.
  * Админ у сервера включает «Узел недоступен» и выбирает резервный сервер.
  * Ссылка подписки клиента (/happ-sub/{sub_id}) не меняется. Бот отдаёт содержимое
    подписки не с основной панели, а с резервной, где для клиента заранее
    (при первом запросе) создаётся такой же клиент с тем же sub_id и сроком.
  * Когда админ выключает «Узел недоступен», подписки снова идут с основного сервера,
    а временные клиенты на резервном удаляются.

Настройки хранятся в settings (failover_cfg, JSON), временные клиенты — в таблице
failover_clients. Миграция БД не нужна.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from database.connection import get_db
from database.requests import get_setting, set_setting

logger = logging.getLogger(__name__)

_SETTING = "failover_cfg"
_locks: Dict[int, asyncio.Lock] = {}


# ── настройки ────────────────────────────────────────────────────────────────

def _load() -> Dict[str, Dict[str, Any]]:
    try:
        data = json.loads(get_setting(_SETTING, "{}") or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save(cfg: Dict[str, Dict[str, Any]]) -> None:
    set_setting(_SETTING, json.dumps(cfg))


def get_config(server_id: int) -> Dict[str, Any]:
    c = _load().get(str(server_id)) or {}
    return {"backup": int(c.get("backup") or 0), "down": bool(c.get("down"))}


def set_backup(server_id: int, backup_id: int) -> None:
    cfg = _load()
    entry = cfg.setdefault(str(server_id), {})
    entry["backup"] = int(backup_id or 0)
    _save(cfg)


def set_down(server_id: int, down: bool) -> None:
    cfg = _load()
    cfg.setdefault(str(server_id), {})["down"] = bool(down)
    _save(cfg)


def is_down(server_id: Optional[int]) -> bool:
    return bool(server_id) and get_config(int(server_id))["down"]


def active_backup_for(server_id: Optional[int]) -> Optional[int]:
    """ID резервного сервера, если основной помечен недоступным и резерв годится."""
    if not server_id:
        return None
    c = get_config(int(server_id))
    if not c["down"] or not c["backup"] or c["backup"] == int(server_id):
        return None
    from database.db_servers import get_server_by_id
    backup = get_server_by_id(c["backup"])
    if not backup or not backup.get("is_active") or is_down(c["backup"]):
        return None
    return c["backup"]


# ── временные клиенты на резервном сервере ───────────────────────────────────

def _ensure_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS failover_clients (
            key_id INTEGER NOT NULL,
            primary_server_id INTEGER NOT NULL,
            backup_server_id INTEGER NOT NULL,
            panel_email TEXT NOT NULL,
            expires_at TEXT,
            PRIMARY KEY (key_id, backup_server_id)
        )
        """
    )


def _row(key_id: int, backup_id: int) -> Optional[Dict[str, Any]]:
    with get_db() as conn:
        _ensure_table(conn)
        r = conn.execute(
            "SELECT * FROM failover_clients WHERE key_id = ? AND backup_server_id = ?",
            (key_id, backup_id),
        ).fetchone()
        return dict(r) if r else None


async def _create_standby(key: Dict[str, Any], backup_id: int) -> bool:
    from bot.services.vpn_api import (
        _panel_total_gb_for_key, get_client, get_client_subscription_inbounds,
    )
    client = await get_client(backup_id)
    inbounds = await get_client_subscription_inbounds(client)
    if not inbounds:
        return False
    expires_at = datetime.fromisoformat(str(key["expires_at"]))
    delta = expires_at - datetime.now()
    days_left = delta.days + (1 if delta.seconds > 0 else 0)
    if days_left < 1:
        return False
    limit_ip = 1
    if key.get("tariff_id"):
        from database.db_tariffs import get_tariff_by_id
        tariff = get_tariff_by_id(key["tariff_id"])
        if tariff:
            limit_ip = tariff.get("max_ips", 1)
    created = 0
    for inb in inbounds:
        try:
            flow = await client.get_inbound_flow(inb["id"])
            await client.add_client(
                inbound_id=inb["id"], email=key["panel_email"],
                total_gb=_panel_total_gb_for_key(key, 0), expire_days=days_left,
                limit_ip=limit_ip, enable=True, tg_id="", flow=flow, sub_id=key["sub_id"],
            )
            created += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("failover: клиент ключа %s не создан в inbound %s: %s", key.get("id"), inb.get("id"), e)
    return created > 0


async def source_key(key: Dict[str, Any]) -> Dict[str, Any]:
    """Ключ, с чьего сервера нужно брать подписку. Если резерв не нужен или не удалось
    подготовить клиента, возвращает исходный ключ (подписка идёт с основного сервера)."""
    try:
        backup_id = active_backup_for(key.get("server_id"))
        if not backup_id or not key.get("sub_id") or not key.get("panel_email") or not key.get("expires_at"):
            return key
        async with _locks.setdefault(int(key["id"]), asyncio.Lock()):
            row = _row(int(key["id"]), backup_id)
            if not row or row["expires_at"] != str(key["expires_at"]):
                if row:  # срок изменился (продление): пересоздаём
                    from bot.services.vpn_api import get_client
                    await (await get_client(backup_id)).delete_clients_by_email_on_server(row["panel_email"])
                if not await _create_standby(key, backup_id):
                    return key
                with get_db() as conn:
                    _ensure_table(conn)
                    conn.execute(
                        "INSERT OR REPLACE INTO failover_clients VALUES (?, ?, ?, ?, ?)",
                        (int(key["id"]), int(key["server_id"]), backup_id, key["panel_email"], str(key["expires_at"])),
                    )
        return {**key, "server_id": backup_id}
    except Exception as e:  # noqa: BLE001
        logger.warning("failover: резерв для ключа %s недоступен: %s", key.get("id"), e)
        return key


async def cleanup(primary_server_id: int) -> int:
    """Удаляет временные клиенты на резервных серверах после восстановления основного."""
    from bot.services.vpn_api import get_client
    with get_db() as conn:
        _ensure_table(conn)
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM failover_clients WHERE primary_server_id = ?", (primary_server_id,)
        ).fetchall()]
    done = 0
    for r in rows:
        try:
            await (await get_client(r["backup_server_id"])).delete_clients_by_email_on_server(r["panel_email"])
        except Exception as e:  # noqa: BLE001
            logger.warning("failover: не удалил временного клиента %s: %s", r["panel_email"], e)
            continue
        with get_db() as conn:
            conn.execute(
                "DELETE FROM failover_clients WHERE key_id = ? AND backup_server_id = ?",
                (r["key_id"], r["backup_server_id"]),
            )
        done += 1
    return done
