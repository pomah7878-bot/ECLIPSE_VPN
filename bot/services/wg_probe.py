"""Реальная проверка WireGuard и AmneziaWG без внешних программ.

Бот сам проходит рукопожатие WireGuard (Noise IK) с данными из конфига ключа,
а затем отправляет через туннель DNS-запрос к 1.1.1.1. Ответ на него значит,
что сервер принял ключ и трафик выходит в интернет.

Для AmneziaWG учитываются заголовки H1–H4 и случайные префиксы S1, S2.
Мусорные пакеты Jc не отправляются: сервер их не требует, они нужны только
против DPI в сети клиента. Расширенный AmneziaWG (S3/S4, I1–I5, защита
заголовков и прочие поля сверх классических) не проверяется: его протокол
не описан, и тест давал бы ложное «не работает».

DNS-запрос идёт на сервер из строки DNS конфига (как у настоящего клиента),
если её нет — на 1.1.1.1.

Ограничение: на время проверки сервер считает адресом этого пира сервер бота;
устройство клиента с тем же конфигом переключится обратно со следующим пакетом.
"""
import base64
import hashlib
import hmac
import os
import random
import re
import socket
import struct
import time
from typing import Any, Dict, List, Optional, Tuple

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

WG_KEYS = {"privatekey", "address", "dns", "mtu", "listenport", "table", "fwmark", "saveconfig",
           "preup", "postup", "predown", "postdown"}
AWG_CLASSIC_KEYS = {"jc", "jmin", "jmax", "s1", "s2", "h1", "h2", "h3", "h4"}
CONSTRUCTION = b"Noise_IKpsk2_25519_ChaChaPoly_BLAKE2s"
IDENTIFIER = b"WireGuard v1 zx2c4 Jason@zx2c4.com"
LABEL_MAC1 = b"mac1----"
TEST_DNS_IP = "1.1.1.1"
TEST_NAME = "cp.cloudflare.com"
HANDSHAKE_WAIT = 3.0
DATA_WAIT = 3.0


# ------------------------------------------------------------------ примитивы WireGuard

def _hash(*parts: bytes) -> bytes:
    h = hashlib.blake2s()
    for p in parts:
        h.update(p)
    return h.digest()


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.blake2s).digest()


def _kdf(key: bytes, data: bytes, n: int) -> List[bytes]:
    t0 = _hmac(key, data)
    out, prev = [], b""
    for i in range(1, n + 1):
        prev = _hmac(t0, prev + bytes([i]))
        out.append(prev)
    return out


def _mac(key: bytes, data: bytes) -> bytes:
    return hashlib.blake2s(data, digest_size=16, key=key).digest()


def _nonce(counter: int) -> bytes:
    return b"\0" * 4 + struct.pack("<Q", counter)


def _dh(priv: bytes, pub: bytes) -> bytes:
    return X25519PrivateKey.from_private_bytes(priv).exchange(X25519PublicKey.from_public_bytes(pub))


def _pub(priv: bytes) -> bytes:
    return X25519PrivateKey.from_private_bytes(priv).public_key().public_bytes_raw()


def _tai64n() -> bytes:
    now = time.time()
    return struct.pack(">QI", 0x400000000000000A + int(now), int((now % 1) * 1e9))


# ------------------------------------------------------------------ конфиг

def parse_config(text: str) -> Optional[Dict[str, Any]]:
    """Конфиг .conf → ключи, адрес сервера, адрес в туннеле и параметры AmneziaWG. None, если не разобран."""
    sections: Dict[str, Dict[str, str]] = {}
    current: Optional[Dict[str, str]] = None
    for raw in (text or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1].strip().lower(), {})
            continue
        if current is not None and "=" in line:
            k, v = line.split("=", 1)
            current[k.strip().lower()] = v.strip()
    iface, peer = sections.get("interface", {}), sections.get("peer", {})
    m = re.match(r"^\[?([^\]]+?)\]?:(\d{1,5})$", peer.get("endpoint", ""))
    try:
        priv = _b64key(iface["privatekey"])
        server_pub = _b64key(peer["publickey"])
        psk = _b64key(peer["presharedkey"]) if peer.get("presharedkey") else b"\0" * 32
    except (KeyError, ValueError):
        return None
    if not m:
        return None
    ipv4 = _first_ipv4(iface.get("address", ""))
    # Как панель отличает «для роутеров»: расширенным считается любое поле сверх классических (или S3/S4 ≠ 0).
    extended = any(k not in WG_KEYS | AWG_CLASSIC_KEYS and str(v).strip() not in ("", "0")
                   for k, v in iface.items())
    return {
        "host": m.group(1), "port": int(m.group(2)), "priv": priv, "server_pub": server_pub, "psk": psk,
        "ipv4": ipv4, "dns": _first_ipv4(iface.get("dns", "")) or TEST_DNS_IP, "extended": extended,
        "h": [_range(iface.get(f"h{i}"), i) for i in range(1, 5)],
        "s1": _int(iface.get("s1")), "s2": _int(iface.get("s2")),
    }


def _first_ipv4(value: str) -> str:
    return next((a.split("/")[0].strip() for a in value.split(",")
                 if re.match(r"^\s*\d+\.\d+\.\d+\.\d+\s*(/\d+)?\s*$", a)), "")


def _b64key(value: str) -> bytes:
    key = base64.b64decode(value.strip(), validate=True)
    if len(key) != 32:
        raise ValueError("ключ не 32 байта")
    return key


def _int(value: Optional[str]) -> int:
    return int(value) if value and value.strip().isdigit() else 0


def _range(value: Optional[str], default: int) -> Tuple[int, int]:
    """H1–H4: число или диапазон «a-b» (AmneziaWG 2.0). По умолчанию — типы WireGuard 1–4."""
    m = re.match(r"^\s*(\d+)\s*(?:-\s*(\d+))?\s*$", value or "")
    if not m:
        return default, default
    a, b = int(m.group(1)), int(m.group(2) or m.group(1))
    return min(a, b), max(a, b)


# ------------------------------------------------------------------ пакеты

def build_initiation(cfg: Dict[str, Any]) -> Tuple[bytes, Dict[str, Any]]:
    """Пакет «начало рукопожатия» (с префиксом S1) и состояние для разбора ответа."""
    e_priv = X25519PrivateKey.generate().private_bytes_raw()
    e_pub = _pub(e_priv)
    sender = random.getrandbits(32)
    c = _hash(CONSTRUCTION)
    h = _hash(_hash(c, IDENTIFIER), cfg["server_pub"])
    c = _kdf(c, e_pub, 1)[0]
    h = _hash(h, e_pub)
    c, k = _kdf(c, _dh(e_priv, cfg["server_pub"]), 2)
    enc_static = ChaCha20Poly1305(k).encrypt(_nonce(0), _pub(cfg["priv"]), h)
    h = _hash(h, enc_static)
    c, k = _kdf(c, _dh(cfg["priv"], cfg["server_pub"]), 2)
    enc_time = ChaCha20Poly1305(k).encrypt(_nonce(0), _tai64n(), h)
    h = _hash(h, enc_time)
    msg = struct.pack("<II", random.randint(*cfg["h"][0]), sender) + e_pub + enc_static + enc_time
    msg += _mac(_hash(LABEL_MAC1, cfg["server_pub"]), msg) + b"\0" * 16
    return os.urandom(cfg["s1"]) + msg, {"c": c, "h": h, "e_priv": e_priv, "sender": sender}


def read_response(cfg: Dict[str, Any], st: Dict[str, Any], data: bytes) -> Optional[Dict[str, Any]]:
    """Ответ сервера → ключи туннеля {send, recv, peer}. None — чужой пакет; InvalidTag — ключи не подошли."""
    data = data[cfg["s2"]:]
    if len(data) != 92:
        return None
    typ, peer, receiver = struct.unpack("<III", data[:12])
    lo, hi = cfg["h"][1]
    if not lo <= typ <= hi or receiver != st["sender"]:
        return None
    e_r, enc_empty = data[12:44], data[44:60]
    c = _kdf(st["c"], e_r, 1)[0]
    h = _hash(st["h"], e_r)
    c = _kdf(c, _dh(st["e_priv"], e_r), 1)[0]
    c = _kdf(c, _dh(cfg["priv"], e_r), 1)[0]
    c, tau, k = _kdf(c, cfg["psk"], 3)
    h = _hash(h, tau)
    ChaCha20Poly1305(k).decrypt(_nonce(0), enc_empty, h)
    send, recv = _kdf(c, b"", 2)
    return {"send": send, "recv": recv, "peer": peer}


def build_data(cfg: Dict[str, Any], keys: Dict[str, Any], counter: int, payload: bytes) -> bytes:
    padded = payload + b"\0" * (-len(payload) % 16)
    head = struct.pack("<IIQ", random.randint(*cfg["h"][3]), keys["peer"], counter)
    return head + ChaCha20Poly1305(keys["send"]).encrypt(_nonce(counter), padded, None)


def read_data(cfg: Dict[str, Any], keys: Dict[str, Any], sender: int, data: bytes) -> Optional[bytes]:
    if len(data) < 32:
        return None
    typ, receiver, counter = struct.unpack("<IIQ", data[:16])
    lo, hi = cfg["h"][3]
    if not lo <= typ <= hi or receiver != sender:
        return None
    try:
        return ChaCha20Poly1305(keys["recv"]).decrypt(_nonce(counter), data[16:], None)
    except InvalidTag:
        return None


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    s = sum(struct.unpack(f">{len(data) // 2}H", data))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


def dns_query_packet(src_ip: str, dns_ip: str, qid: int) -> bytes:
    """IPv4/UDP-пакет с DNS-запросом A-записи TEST_NAME к dns_ip."""
    qname = b"".join(bytes([len(p)]) + p.encode() for p in TEST_NAME.split(".")) + b"\0"
    dns = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0) + qname + struct.pack(">HH", 1, 1)
    src, dst = socket.inet_aton(src_ip), socket.inet_aton(dns_ip)
    sport = random.randint(20000, 60000)
    udp_len = 8 + len(dns)
    pseudo = src + dst + struct.pack(">BBH", 0, 17, udp_len)
    udp = struct.pack(">HHHH", sport, 53, udp_len, 0) + dns
    udp = udp[:6] + struct.pack(">H", _checksum(pseudo + udp) or 0xFFFF) + udp[8:]
    ip = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + udp_len, qid, 0, 64, 17, 0, src, dst)
    ip = ip[:10] + struct.pack(">H", _checksum(ip)) + ip[12:]
    return ip + udp


def is_dns_reply(packet: bytes, dns_ip: str, qid: int) -> bool:
    if len(packet) < 28 or packet[0] >> 4 != 4 or packet[9] != 17:
        return False
    if packet[12:16] != socket.inet_aton(dns_ip):
        return False
    udp = packet[(packet[0] & 0x0F) * 4:]
    return len(udp) >= 10 and struct.unpack(">H", udp[:2])[0] == 53 and struct.unpack(">H", udp[8:10])[0] == qid


# ------------------------------------------------------------------ проверка

def _recv_until(sock: socket.socket, deadline: float, accept) -> Any:
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        sock.settimeout(left)
        try:
            data = sock.recv(4096)
        except socket.timeout:
            return None
        except OSError:
            return None
        result = accept(data)
        if result is not None:
            return result


def probe_sync(config_text: str) -> Dict[str, Any]:
    """Рукопожатие + DNS-запрос через туннель. Возвращает {status, detail, ms?} как остальные проверки."""
    cfg = parse_config(config_text)
    if not cfg:
        return {"status": "skip", "detail": "конфиг WireGuard не разобран"}
    if cfg["extended"]:
        return {"status": "skip", "detail": "расширенный AmneziaWG (защита заголовков): тест не поддерживает"}
    try:
        family, _, _, _, addr = socket.getaddrinfo(cfg["host"], cfg["port"], type=socket.SOCK_DGRAM)[0]
    except OSError:
        return {"status": "bad", "detail": "адрес сервера не найден"}
    started = time.monotonic()
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(addr)
        except OSError:
            return {"status": "bad", "detail": "сервер недоступен"}
        keys = st = None
        try:
            for _ in range(2):  # новое рукопожатие на каждую попытку: повтор старого сервер отбросит
                packet, st = build_initiation(cfg)
                sock.send(packet)
                keys = _recv_until(sock, time.monotonic() + HANDSHAKE_WAIT, lambda d: read_response(cfg, st, d))
                if keys:
                    break
        except InvalidTag:
            return {"status": "bad", "detail": "рукопожатие не прошло: ключи не подходят"}
        except OSError:
            return {"status": "bad", "detail": "сервер недоступен"}
        if not keys:
            return {"status": "bad", "detail": "сервер не ответил на рукопожатие"}
        if not cfg["ipv4"]:
            ms = int((time.monotonic() - started) * 1000)
            return {"status": "ok", "detail": f"рукопожатие прошло, {ms} мс", "ms": ms}
        for counter in range(2):
            qid = random.randint(1, 0xFFFF)
            try:
                sock.send(build_data(cfg, keys, counter, dns_query_packet(cfg["ipv4"], cfg["dns"], qid)))
            except OSError:
                break
            got = _recv_until(sock, time.monotonic() + DATA_WAIT,
                              lambda d: True if (p := read_data(cfg, keys, st["sender"], d)) and is_dns_reply(p, cfg["dns"], qid)
                              else None)
            if got:
                ms = int((time.monotonic() - started) * 1000)
                return {"status": "ok", "detail": f"трафик идёт, {ms} мс", "ms": ms}
    return {"status": "bad", "detail": "рукопожатие прошло, трафик не идёт"}
