"""Разбор и группировка отдельных ссылок подключения внутри одной подписки
(vless/vmess/trojan/hysteria2/tuic и конфиги AmneziaWG/WireGuard) — для
отображения клиенту списка конкретных inbound'ов вместо одной агрегированной
ссылки.

v1.197: добавлены TUIC v5 (tuic://) и AmneziaWG/WireGuard. Панель 3x-ui отдаёт
их в подписке строкой vpn://<base64 от .conf>; такую строку разбираем в
обычный конфиг, который клиент может скачать файлом или отсканировать.
"""
import base64
import binascii
import io
import json
import re
import urllib.parse
import zlib
from typing import Any, Optional

# Протоколы поверх UDP — TCP-пинг к их порту бессмысленен.
UDP_PROTOCOLS = {"hysteria2", "tuic", "amneziawg", "wireguard"}
# Протоколы, показ которых включается функцией лицензии "extra_protocols".
EXTRA_PROTOCOLS = {"tuic", "amneziawg", "wireguard"}
EXTRA_FEATURE = "extra_protocols"

_PROTOCOL_LABELS = {
    "vless": "Vless",
    "vmess": "VMess",
    "trojan": "Trojan",
    "ss": "Shadowsocks",
    "hysteria2": "Hysteria2",
    "tuic": "TUIC",
}
_AWG_KEYS = {"jc", "jmin", "jmax", "s1", "s2", "s3", "s4", "h1", "h2", "h3", "h4", "i1"}


def _b64_to_bytes(raw: str) -> Optional[bytes]:
    """Декодирует base64 (обычный или URL-safe, с паддингом или без)."""
    try:
        text = urllib.parse.unquote(raw).strip().replace("+", "-").replace("/", "_")
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        return None


def _conf_from_amnezia_json(blob: bytes) -> Optional[str]:
    """Официальный формат vpn:// приложения Amnezia: 4 байта длины + zlib(JSON).
    Достаём из JSON готовый конфиг (последний встреченный), если он там есть."""
    try:
        data = json.loads(zlib.decompress(blob[4:]).decode("utf-8"))
    except Exception:
        return None
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "last_config" and isinstance(value, str):
                    try:
                        inner = json.loads(value)
                        cfg = inner.get("config") if isinstance(inner, dict) else None
                        if isinstance(cfg, str):
                            found.append(cfg)
                    except Exception:
                        found.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    for cfg in found:
        if "[Interface]" in cfg and "[Peer]" in cfg:
            return cfg
    return None


def decode_vpn_link(link: str) -> Optional[str]:
    """vpn://... -> текст конфига WireGuard/AmneziaWG или None."""
    blob = _b64_to_bytes(link[len("vpn://"):])
    if not blob:
        return None
    try:
        text = blob.decode("utf-8")
        if "[Interface]" in text and "[Peer]" in text:
            return text.strip() + "\n"
    except UnicodeDecodeError:
        pass
    cfg = _conf_from_amnezia_json(blob)
    return (cfg.strip() + "\n") if cfg else None


def parse_conf(text: str) -> Optional[dict[str, Any]]:
    """Разбирает конфиг WireGuard/AmneziaWG: секции, endpoint, название."""
    section = None
    interface: dict[str, str] = {}
    peer: dict[str, str] = {}
    comment = ""
    peer_comment = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            comment = line.lstrip("#").strip()
            continue
        low = line.lower()
        if low == "[interface]":
            section = interface
            continue
        if low == "[peer]":
            section = peer
            peer_comment = comment
            continue
        if section is None or "=" not in line:
            continue
        key, value = line.split("=", 1)
        section[key.strip()] = value.strip()
    endpoint = peer.get("Endpoint", "")
    match = re.match(r"^\[?([^\]]+?)\]?:(\d{1,5})$", endpoint)
    if not match or not interface or not peer:
        return None
    is_awg = any(k.lower() in _AWG_KEYS for k in interface)
    return {
        "host": match.group(1),
        "port": int(match.group(2)),
        "name": peer_comment,
        "protocol": "amneziawg" if is_awg else "wireguard",
    }


def _qr_png_data_url(payload: str) -> Optional[str]:
    """QR-код локально (приватные ключи не отправляются сторонним сервисам)."""
    try:
        import qrcode

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=6,
            border=2,
        )
        qr.add_data(payload)
        qr.make(fit=True)
        buf = io.BytesIO()
        qr.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def _parse_vpn_link(link: str) -> Optional[dict[str, Any]]:
    conf = decode_vpn_link(link)
    if not conf:
        return None
    info = parse_conf(conf)
    if not info:
        return None
    protocol = info["protocol"]
    is_awg = protocol == "amneziawg"
    return {
        "protocol": protocol,
        "protocol_label": "AmneziaWG" if is_awg else "WireGuard",
        "transport_label": "UDP",
        "security_label": "AWG" if is_awg else "WG",
        "host": info["host"],
        "port": info["port"],
        "name": info["name"] or ("AmneziaWG" if is_awg else "WireGuard"),
        "link": link,
        "kind": "conf",
        "config_text": conf,
        "filename": f"{protocol}-{re.sub(r'[^A-Za-z0-9.-]', '_', info['host'])}.conf",
    }


def parse_inbound_link(link: str) -> Optional[dict[str, Any]]:
    """Разбирает одну ссылку на составные части.

    Args:
        link: Полная ссылка (vless://..., hysteria2://..., tuic://..., vpn://...)

    Returns:
        Словарь {protocol, host, port, name, link, kind} или None при ошибке.
        `kind` — "link" (обычная ссылка) или "conf" (конфиг-файл WG/AWG,
        тогда есть ещё config_text и filename).
        `name` — это remark из ссылки (уже красиво оформлен панелью,
        с флагами и медалями приоритета — используем как есть).
    """
    try:
        if link.lower().startswith("vpn://"):
            return _parse_vpn_link(link)

        parsed = urllib.parse.urlparse(link)
        if not parsed.hostname or not parsed.port:
            return None
        scheme = parsed.scheme.lower()
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
        elif scheme in ("hysteria2", "tuic"):
            security_label = "TLS"
        else:
            security_label = security_raw.upper() or "NONE"
        if scheme == "tuic":
            net_type = "QUIC"

        return {
            "protocol": scheme,
            "protocol_label": _PROTOCOL_LABELS.get(scheme, scheme.upper() or "Vless"),
            "transport_label": net_type,
            "security_label": security_label,
            "host": parsed.hostname,
            "port": parsed.port,
            "name": name.strip(),
            "link": link,
            "kind": "link",
        }
    except Exception:
        return None


def parse_and_group_inbound_links(raw_links_text: str) -> list[dict[str, Any]]:
    """Разбирает многострочный текст подписки (одна ссылка на строку) и
    группирует по хосту.

    Args:
        raw_links_text: Сырой текст из get_subscription_link (\n-разделённый)

    Returns:
        Список групп: [{host, inbounds: [{protocol, port, name, link, ...}, ...]}],
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
        item = {
            "protocol": parsed["protocol"],
            "protocol_label": parsed["protocol_label"],
            "transport_label": parsed["transport_label"],
            "security_label": parsed["security_label"],
            "port": parsed["port"],
            "name": parsed["name"],
            "link": parsed["link"],
            "kind": parsed.get("kind", "link"),
        }
        if item["kind"] == "conf":
            item["config_text"] = parsed["config_text"]
            item["filename"] = parsed["filename"]
        groups.setdefault(host, []).append(item)

    return [
        {"host": host, "inbounds": inbounds}
        for host, inbounds in sorted(groups.items())
    ]


def extra_protocols_allowed() -> bool:
    """Включена ли функция лицензии «AmneziaWG/WireGuard/TUIC в подключениях»."""
    try:
        from bot.services.license import is_feature_available
        return bool(is_feature_available(EXTRA_FEATURE))
    except Exception:
        return False


def filter_extra_protocols(groups: list[dict], allowed: bool) -> list[dict]:
    """Без лицензии убирает AmneziaWG/WireGuard/TUIC из списка (остальное — как раньше)."""
    if allowed:
        return groups
    result = []
    for group in groups:
        kept = [ib for ib in group["inbounds"] if ib["protocol"] not in EXTRA_PROTOCOLS]
        if kept:
            result.append({"host": group["host"], "inbounds": kept})
    return result


def attach_local_qr(groups: list[dict]) -> list[dict]:
    """Для новых протоколов делаем QR на нашей стороне: в конфиге лежит
    приватный ключ клиента, отдавать его сторонним сервисам нельзя."""
    for group in groups:
        for ib in group["inbounds"]:
            if ib["protocol"] in EXTRA_PROTOCOLS:
                payload = ib.get("config_text") or ib["link"]
                qr = _qr_png_data_url(payload)
                if qr:
                    ib["qr_png"] = qr
    return groups


def build_connection_groups(raw_links_text: str) -> list[dict[str, Any]]:
    """Готовый список подключений для бота, мини-приложения и сайта."""
    groups = parse_and_group_inbound_links(raw_links_text)
    groups = filter_extra_protocols(groups, extra_protocols_allowed())
    return attach_local_qr(groups)


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
            if inbound["protocol"] in UDP_PROTOCOLS:
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

        # Hysteria2/TUIC/AmneziaWG работают по UDP — обычный TCP-пинг
        # технически неприменим напрямую к их порту. Вместо пустого поля
        # показываем приближённую оценку — среднюю задержку TCP-подключений
        # того же физического хоста (помечено как "≈", не точное измерение).
        if tcp_pings_this_host:
            approx = round(sum(tcp_pings_this_host) / len(tcp_pings_this_host))
            for inbound in group["inbounds"]:
                if inbound["protocol"] in UDP_PROTOCOLS:
                    inbound["latency_ms"] = approx
                    inbound["ping_unsupported"] = False
                    inbound["is_approximate"] = True
    return groups


# ===== v1.203: .conf -> официальный ключ vpn:// приложения AmneziaVPN =====
# Панель 3x-ui отдаёт vpn:// как base64 от «голого» .conf. Приложение AmneziaVPN
# такой ключ не принимает (ErrorCode 900: «Конфигурация не содержит контейнеров»):
# ему нужен JSON с контейнерами, сжатый qCompress (4 байта длины + zlib) и base64url.
_AWG_CANON = {
    "jc": "Jc", "jmin": "Jmin", "jmax": "Jmax",
    "s1": "S1", "s2": "S2", "s3": "S3", "s4": "S4",
    "h1": "H1", "h2": "H2", "h3": "H3", "h4": "H4",
    "i1": "I1", "i2": "I2", "i3": "I3", "i4": "I4", "i5": "I5",
}


def conf_to_amnezia_vpn(conf_text: str, description: str = "ECLIPSE") -> Optional[str]:
    """Конфиг WireGuard/AmneziaWG -> ключ vpn:// в формате AmneziaVPN (или None)."""
    import struct
    interface: dict[str, str] = {}
    peer: dict[str, str] = {}
    section = None
    for raw in (conf_text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        low = line.lower()
        if low == "[interface]":
            section = interface
            continue
        if low == "[peer]":
            section = peer
            continue
        if section is None or "=" not in line:
            continue
        key, value = line.split("=", 1)
        section[key.strip()] = value.strip()
    match = re.match(r"^\[?([^\]]+?)\]?:(\d{1,5})$", peer.get("Endpoint", ""))
    if not match or not interface.get("PrivateKey") or not peer.get("PublicKey"):
        return None
    host, port = match.group(1), int(match.group(2))
    # v1.217: переносим все не-WireGuard параметры (AWG 2.0 и 3.x), а не только
    # заранее известные — иначе новые поля 3.x теряются и ключ не работает.
    _wg_std = {"privatekey", "address", "dns", "mtu", "listenport", "table",
               "preup", "postup", "predown", "postdown", "saveconfig", "fwmark"}
    awg = {}
    for _k, _v in interface.items():
        _kl = _k.lower()
        if _kl in _AWG_CANON:
            awg[_AWG_CANON[_kl]] = _v
        elif _kl not in _wg_std:
            awg[_k] = _v
    is_awg = bool(awg)
    dns = [d.strip() for d in interface.get("DNS", "").split(",") if d.strip()]
    last_config: dict[str, Any] = dict(awg)
    last_config.update({
        "client_priv_key": interface["PrivateKey"],
        "client_ip": interface.get("Address", "").split(",")[0].split("/")[0].strip(),
        "server_pub_key": peer["PublicKey"],
        "psk_key": peer.get("PresharedKey", ""),
        "allowed_ips": [a.strip() for a in peer.get("AllowedIPs", "0.0.0.0/0, ::/0").split(",") if a.strip()],
        "persistent_keep_alive": peer.get("PersistentKeepalive", "25"),
        "mtu": interface.get("MTU", "1280"),
        "hostName": host,
        "port": port,
        "config": conf_text.strip() + "\n",
    })
    container_body: dict[str, Any] = dict(awg)
    container_body.update({
        "last_config": json.dumps(last_config, ensure_ascii=False, separators=(",", ":")),
        "port": str(port),
        "transport_proto": "udp",
        "isThirdPartyConfig": True,
    })
    name = "amnezia-awg" if is_awg else "amnezia-wireguard"
    payload = {
        "containers": [{"container": name, ("awg" if is_awg else "wireguard"): container_body}],
        "defaultContainer": name,
        "description": description or "ECLIPSE",
        "dns1": dns[0] if dns else "1.1.1.1",
        "dns2": dns[1] if len(dns) > 1 else "1.0.0.1",
        "hostName": host,
        "isThirdPartyConfig": True,
    }
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    blob = struct.pack(">I", len(data)) + zlib.compress(data)
    return "vpn://" + base64.urlsafe_b64encode(blob).decode("ascii").rstrip("=")
