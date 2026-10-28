"""Нода мастер-панели заблокирована: убрать её inbound'ы из подписки и вернуть обратно.

В 3x-ui у inbound'а есть признак excludeFromSub («не включать в подписку») и поле nodeId.
Бот ставит excludeFromSub только на включённые inbound'ы выбранной ноды (выключенные вручную
не трогает), запоминает их и при возврате снимает признак ровно с них. Сам inbound остаётся
включённым, поэтому нода после замены IP начинает работать без дополнительных действий.

Защита от порчи: первый inbound меняется «пробно» — после записи бот перечитывает его и
сверяет остальные поля; при расхождении возвращает исходное состояние и останавливается.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Tuple

from database.requests import get_setting, set_setting

logger = logging.getLogger(__name__)

_CHECK_FIELDS = ("enable", "port", "protocol", "remark", "settings", "streamSettings", "sniffing",
                 "nodeId", "listen", "tag", "total", "expiryTime", "shareAddr", "shareAddrStrategy",
                 "subSortIndex")


def _key(server_id: int, node_id: int) -> str:
    return f"node_excluded_{int(server_id)}_{int(node_id)}"


def excluded_ids(server_id: int, node_id: int) -> List[int]:
    try:
        return [int(x) for x in json.loads(get_setting(_key(server_id, node_id), "[]") or "[]")]
    except Exception:  # noqa: BLE001
        return []


async def _raw_inbounds(client) -> List[Dict[str, Any]]:
    res = await client._request("GET", "/panel/api/inbounds/list")
    obj = res.get("obj", [])
    return [i for i in obj if isinstance(i, dict)] if isinstance(obj, list) else []


async def _write(client, inbound: Dict[str, Any], exclude: bool) -> bool:
    """Меняет excludeFromSub и проверяет, что остальное не пострадало."""
    before = dict(inbound)
    body = {k: v for k, v in inbound.items() if k not in ("clientStats", "id")}
    body["excludeFromSub"] = bool(exclude)
    await client._request("POST", f"/panel/api/inbounds/update/{inbound['id']}", data=body)
    after = next((i for i in await _raw_inbounds(client) if i.get("id") == inbound["id"]), None)
    if after is None:
        raise RuntimeError(f"inbound {inbound['id']} пропал после записи")
    bad = [f for f in _CHECK_FIELDS if before.get(f) != after.get(f)]
    if bad or bool(after.get("excludeFromSub")) != bool(exclude):
        body["excludeFromSub"] = bool(before.get("excludeFromSub"))
        await client._request("POST", f"/panel/api/inbounds/update/{inbound['id']}", data=body)
        raise RuntimeError(f"inbound {inbound['id']}: после записи изменились поля {bad or ['excludeFromSub']}, возвращено исходное")
    return True


async def set_node_excluded(server_id: int, node_id: int, exclude: bool) -> Tuple[int, str]:
    """Возвращает (сколько inbound'ов изменено, текст ошибки или пусто)."""
    from bot.services.vpn_api import get_client
    client = await get_client(server_id)
    inbounds = await _raw_inbounds(client)
    if exclude:
        targets = [i for i in inbounds if i.get("nodeId") == node_id and i.get("enable") and not i.get("excludeFromSub")]
    else:
        ids = set(excluded_ids(server_id, node_id))
        targets = [i for i in inbounds if i.get("id") in ids and i.get("excludeFromSub")]
    done: List[int] = []
    err = ""
    for ib in targets:
        try:
            await _write(client, ib, exclude)
            done.append(int(ib["id"]))
        except Exception as e:  # noqa: BLE001
            err = str(e)
            logger.warning("node_failover: %s", e)
            break  # первый сбой: дальше не идём
    prev = excluded_ids(server_id, node_id)
    if exclude:
        set_setting(_key(server_id, node_id), json.dumps(sorted(set(prev) | set(done))))
    else:
        set_setting(_key(server_id, node_id), json.dumps([i for i in prev if i not in done] if err else []))
    return len(done), err
