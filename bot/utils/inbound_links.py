"""Разбор и группировка отдельных ссылок подключения (vless/vmess/
trojan/hysteria2) внутри одной подписки — для отображения клиенту
списка конкретных inbound'ов вместо одной агрегированной ссылки.
"""
import urllib.parse
from typing import Any, Optional


def parse_inbound_link(link: str) -> Optional[dict[str, Any]]:
    """Разбирает одну ссылку на составные части.

    Args:
        link: Полная ссылка (vless://..., hysteria2://... и т.п.)

    Returns:
        Словарь {protocol, host, port, name, link} или None при ошибке разбора.
        `name` — это remark из ссылки (уже красиво оформлен панелью,
        с флагами и медалями приоритета — используем как есть).
    """
    try:
        parsed = urllib.parse.urlparse(link)
        if not parsed.hostname or not parsed.port:
            return None
        name = urllib.parse.unquote(parsed.fragment) if parsed.fragment else parsed.hostname
        query = urllib.parse.parse_qs(parsed.query)

        # Отдельные бейджи транспорта/шифрования (как в клиентах Karing/v2rayN):
        # протокол — из схемы ссылки, транспорт/шифрование — из query-параметров.
        net_type = (query.get("type", [""])[0] or "tcp").upper()
        security_raw = (query.get("security", [""])[0] or "").lower()
        if security_raw == "reality":
            security_label = "REALITY"
        elif security_raw == "tls":
            security_label = "TLS"
        elif parsed.scheme == "hysteria2":
            security_label = "TLS"
        else:
            security_label = security_raw.upper() or "NONE"

        return {
            "protocol": parsed.scheme,
            "protocol_label": "Hysteria2" if parsed.scheme == "hysteria2" else "Vless",
            "transport_label": net_type,
            "security_label": security_label,
            "host": parsed.hostname,
            "port": parsed.port,
            "name": name.strip(),
            "link": link,
        }
    except Exception:
        return None


def parse_and_group_inbound_links(raw_links_text: str) -> list[dict[str, Any]]:
    """Разбирает многострочный текст подписки (одна ссылка на строку) и
    группирует по хосту.

    Args:
        raw_links_text: Сырой текст из get_subscription_link (\n-разделённый)

    Returns:
        Список групп: [{host, inbounds: [{protocol, port, name, link}, ...]}],
        отсортировано по хосту для стабильного порядка.
    """
    if not raw_links_text:
        return []

    groups: dict[str, list[dict[str, Any]]] = {}
    for line in raw_links_text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parsed = parse_inbound_link(line)
        if not parsed:
            continue
        host = parsed["host"]
        groups.setdefault(host, []).append({
            "protocol": parsed["protocol"],
            "protocol_label": parsed["protocol_label"],
            "transport_label": parsed["transport_label"],
            "security_label": parsed["security_label"],
            "port": parsed["port"],
            "name": parsed["name"],
            "link": parsed["link"],
        })

    return [
        {"host": host, "inbounds": inbounds}
        for host, inbounds in sorted(groups.items())
    ]


def _tcp_ping(host: str, port: int, timeout: float = 2.0) -> int | None:
    """Измеряет задержку TCP-подключения к host:port в миллисекундах.
    Возвращает None, если сервер недоступен или порт закрыт."""
    import socket
    import time

    start = time.monotonic()
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return int((time.monotonic() - start) * 1000)
    except (socket.timeout, OSError):
        return None


async def add_ping_to_groups(groups: list[dict]) -> list[dict]:
    """Дополняет каждый inbound внутри групп полем latency_ms — реальным
    TCP-пингом до его конкретного host:port (в отличие от пинга всего
    сервера, здесь у каждого inbound может быть свой порт/маршрут)."""
    import asyncio

    loop = asyncio.get_event_loop()
    for group in groups:
        tcp_pings_this_host = []
        for inbound in group["inbounds"]:
            if inbound["protocol"] == "hysteria2":
                inbound["latency_ms"] = None
                inbound["ping_unsupported"] = True
                inbound["is_approximate"] = False
                continue
            latency = await loop.run_in_executor(
                None, _tcp_ping, group["host"], inbound["port"]
            )
            inbound["latency_ms"] = latency
            inbound["ping_unsupported"] = False
            inbound["is_approximate"] = False
            if latency is not None:
                tcp_pings_this_host.append(latency)

        # Hysteria2 работает по UDP/QUIC — обычный TCP-пинг технически
        # неприменим напрямую к его порту. Вместо пустого поля показываем
        # приближённую оценку — среднюю задержку TCP-подключений того же
        # физического хоста (помечено как "≈", не точное измерение).
        if tcp_pings_this_host:
            approx = round(sum(tcp_pings_this_host) / len(tcp_pings_this_host))
            for inbound in group["inbounds"]:
                if inbound["protocol"] == "hysteria2":
                    inbound["latency_ms"] = approx
                    inbound["ping_unsupported"] = False
                    inbound["is_approximate"] = True
    return groups
