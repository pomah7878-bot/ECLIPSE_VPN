"""Проверка работоспособности подключений из подписки.

Администратор выбирает один свой ключ — «ключ для проверки». Бот берёт его
подключения ровно в том виде, как их получает клиент (список ссылок подписки),
и проверяет каждое:

  Проверка 1 (быстрая, без внешних программ):
    • порт открыт и отвечает (TCP);
    • для TLS/REALITY проходит рукопожатие;
    • для TLS-подключений с доменом — срок действия сертификата.
  Проверка 2 (реальный тест, нужен Xray на сервере бота):
    • бот поднимает на несколько секунд клиент Xray с данными этого подключения
      и делает запрос в интернет через него. Так проверяются вход, Reality,
      маршрутизация и каскад до выходного узла — всё, как у настоящего клиента.

Ограничения (их нужно понимать):
  • проверка идёт с сервера бота, а не из сети клиента. Она показывает, что
    подключение живо, но не покажет, что его блокирует конкретный оператор;
  • реальным тестом проверяются VLESS, Trojan, VMess, Shadowsocks и Hysteria2
    (без обфускации); TUIC, WireGuard и AmneziaWG не проверяются;
  • тестовый ключ в панели видит IP сервера бота как «устройство»: берите ключ
    без жёсткого лимита устройств.
"""
import asyncio
import base64
import json
import logging
import os
import shutil
import socket
import ssl
import tempfile
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

KEY_SETTING = "probe_key_id"
AUTO_SETTING = "probe_auto"
CONNECT_TIMEOUT = 4.0
XRAY_TIMEOUT = 14.0
XRAY_PARALLEL = 3
CERT_WARN_DAYS = 14
CERT_BAD_DAYS = 3
TEST_URLS = ("https://cp.cloudflare.com/generate_204", "https://www.gstatic.com/generate_204")
XRAY_PATHS = (
    "/opt/eclipse-probe/xray",
    "/usr/local/bin/xray",
    "/usr/bin/xray",
    "/usr/local/x-ui/bin/xray-linux-amd64",
)
UDP_PROTOCOLS = {"hysteria2", "tuic", "amneziawg", "wireguard", "masque"}

Check = Tuple[str, str, str]

# Итог последней проверки: показывается в «Диагностике» без запуска новой проверки.
last_summary: Dict[str, Any] = {}


# ------------------------------------------------------------------ настройки

def get_key_id() -> Optional[int]:
    from database.db_settings import get_setting
    raw = str(get_setting(KEY_SETTING, "") or "").strip()
    return int(raw) if raw.isdigit() else None


def set_key_id(key_id: Optional[int]) -> None:
    from database.db_settings import set_setting
    set_setting(KEY_SETTING, str(key_id) if key_id else "")


def is_licensed() -> bool:
    try:
        from bot.services.license import is_feature_available
        return is_feature_available("connection_probe")
    except Exception:
        return False


def auto_enabled() -> bool:
    if not is_licensed():
        return False
    from database.db_settings import get_setting
    return str(get_setting(AUTO_SETTING, "1") or "1") == "1"


def set_auto(enabled: bool) -> None:
    from database.db_settings import set_setting
    set_setting(AUTO_SETTING, "1" if enabled else "0")


def find_xray() -> Optional[str]:
    """Путь к Xray на сервере бота или None."""
    candidates = [shutil.which("xray")] + list(XRAY_PATHS)
    for path in candidates:
        if path and os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return None


# ------------------------------------------------------------------ ссылки ключа

async def load_links(key: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Плоский список подключений ключа: [{protocol, host, port, name, link, ...}]."""
    from bot.services.vpn_api import get_client
    from bot.services.cdn import get_connection_links
    from bot.utils.inbound_links import build_connection_groups
    client = await get_client(key["server_id"])
    raw = await get_connection_links(client, key["id"], key["sub_id"])
    items: List[Dict[str, Any]] = []
    for group in build_connection_groups(raw):
        for ib in group["inbounds"]:
            items.append({**ib, "host": group["host"]})
    return items


def _query(link: str) -> Dict[str, str]:
    parsed = urllib.parse.urlparse(link)
    return {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items() if v}


# ------------------------------------------------------------------ проверка 1: порт, TLS, сертификат

def _tls_probe_sync(host: str, port: int, security: str, sni: str, insecure: bool) -> Dict[str, Any]:
    """Подключение и (если нужно) TLS-рукопожатие. Возвращает {ok, ms, error, cert_days}."""
    started = time.monotonic()
    result: Dict[str, Any] = {"ok": False, "ms": None, "error": "", "cert_days": None}
    try:
        sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    except socket.timeout:
        result["error"] = "таймаут подключения"
        return result
    except OSError as e:
        result["error"] = "порт закрыт или сервер недоступен" if getattr(e, "errno", None) else "нет ответа"
        return result
    try:
        if security in ("tls", "reality"):
            ctx = ssl.create_default_context()
            if security == "reality" or insecure:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock.settimeout(CONNECT_TIMEOUT)
            try:
                tls = ctx.wrap_socket(sock, server_hostname=sni or host)
            except ssl.SSLCertVerificationError:
                result["error"] = "недействительный сертификат"
                return result
            except (ssl.SSLError, OSError):
                result["error"] = "TLS не отвечает"
                return result
            try:
                if security == "tls" and not insecure:
                    cert = tls.getpeercert() or {}
                    not_after = cert.get("notAfter")
                    if not_after:
                        expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(not_after), timezone.utc)
                        result["cert_days"] = (expires - datetime.now(timezone.utc)).days
            finally:
                tls.close()
                sock = None
        result["ok"] = True
        result["ms"] = int((time.monotonic() - started) * 1000)
        return result
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


async def probe_basic(item: Dict[str, Any]) -> Dict[str, Any]:
    """Проверка 1 для одного подключения."""
    proto = item.get("protocol", "")
    if proto in UDP_PROTOCOLS or item.get("kind") == "conf":
        return {"status": "skip", "detail": skip_reason(item)}
    q = _query(item.get("link", ""))
    security = (q.get("security") or ("tls" if proto == "trojan" else "")).lower()
    sni = q.get("sni") or q.get("host") or ""
    insecure = q.get("allowInsecure") == "1" or q.get("insecure") == "1"
    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(
        None, _tls_probe_sync, item["host"], int(item["port"]), security, sni, insecure)
    if not res["ok"]:
        return {"status": "bad", "detail": res["error"] or "не отвечает"}
    days = res.get("cert_days")
    if days is not None:
        if days < 0:
            return {"status": "bad", "detail": "сертификат истёк", "ms": res["ms"]}
        if days <= CERT_BAD_DAYS:
            return {"status": "bad", "detail": f"сертификат истекает через {days} дн.", "ms": res["ms"]}
        if days <= CERT_WARN_DAYS:
            return {"status": "warn", "detail": f"сертификат истекает через {days} дн.", "ms": res["ms"]}
    return {"status": "ok", "detail": f"порт отвечает, {res['ms']} мс", "ms": res["ms"], "cert_days": days}


# ------------------------------------------------------------------ проверка 2: реальный тест через Xray

def _norm_pin(value: str) -> str:
    """Отпечаток сертификата (SHA-256) → 64 символа hex в нижнем регистре; иначе пустая строка."""
    pin = (value or "").replace(":", "").replace(" ", "").lower()
    return pin if len(pin) == 64 and all(c in "0123456789abcdef" for c in pin) else ""


def _is_insecure(q: Dict[str, str]) -> bool:
    return (q.get("allowInsecure") or q.get("insecure") or "") in ("1", "true")


def needs_pin(link: str) -> bool:
    """Подключение с «небезопасным» сертификатом (allowInsecure/insecure), но без закреплённого отпечатка.
    В новом Xray флаг allowInsecure удалён: сертификат нужно закрепить по отпечатку."""
    q = _query(link)
    return _is_insecure(q) and not _norm_pin(q.get("pinSHA256") or q.get("pin") or "")


def _fetch_cert_pin_sync(host: str, port: int, sni: str) -> str:
    """Отпечаток сертификата сервера (по TCP+TLS) для подключений с самоподписанным сертификатом."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT) as sock:
        with ctx.wrap_socket(sock, server_hostname=sni or host) as tls:
            der = tls.getpeercert(binary_form=True)
    import hashlib
    return hashlib.sha256(der).hexdigest() if der else ""


def _stream_settings(q: Dict[str, str], host: str, default_security: str = "", pin: str = "") -> Dict[str, Any]:
    net = (q.get("type") or "tcp").lower()
    if net == "http":
        net = "tcp"
    security = (q.get("security") or default_security or "none").lower()
    stream: Dict[str, Any] = {"network": net, "security": security}
    sni = q.get("sni") or ""
    if security == "tls":
        tls: Dict[str, Any] = {"serverName": sni or q.get("host") or host}
        if pin:
            tls["pinnedPeerCertSha256"] = pin
        if q.get("fp"):
            tls["fingerprint"] = q["fp"]
        if q.get("alpn"):
            tls["alpn"] = [a for a in q["alpn"].split(",") if a]
        stream["tlsSettings"] = tls
    elif security == "reality":
        stream["realitySettings"] = {
            "serverName": sni, "fingerprint": q.get("fp") or "chrome",
            "publicKey": q.get("pbk", ""), "shortId": q.get("sid", ""), "spiderX": q.get("spx", ""),
        }
    path = q.get("path") or "/"
    if net == "ws":
        stream["wsSettings"] = {"path": path, "headers": {"Host": q.get("host", "")} if q.get("host") else {}}
    elif net == "grpc":
        stream["grpcSettings"] = {"serviceName": q.get("serviceName", "")}
    elif net == "httpupgrade":
        stream["httpupgradeSettings"] = {"path": path, "host": q.get("host", "")}
    elif net == "xhttp":
        xh: Dict[str, Any] = {"path": path, "host": q.get("host", ""), "mode": q.get("mode") or "auto"}
        if q.get("extra"):
            try:
                xh["extra"] = json.loads(q["extra"])
            except ValueError:
                pass
        stream["xhttpSettings"] = xh
    elif net not in ("tcp",):
        return {}  # kcp, quic и прочее — не поддерживается тестом
    return stream


def link_to_outbound(link: str, pin: str = "") -> Optional[Dict[str, Any]]:
    """Ссылка подключения → исходящее подключение Xray. None, если тип не поддерживается.

    pin — отпечаток сертификата (hex), нужен для ссылок с insecure/allowInsecure."""
    try:
        parsed = urllib.parse.urlparse(link)
        scheme = parsed.scheme.lower()
        host, port = parsed.hostname, parsed.port
        if scheme == "vmess":
            raw = link[len("vmess://"):]
            data = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8"))
            q = {"type": data.get("net", "tcp"), "security": "tls" if data.get("tls") == "tls" else "none",
                 "sni": data.get("sni", ""), "host": data.get("host", ""), "path": data.get("path", "/"),
                 "fp": data.get("fp", ""), "alpn": data.get("alpn", ""), "serviceName": data.get("path", "")}
            stream = _stream_settings(q, data["add"])
            if not stream:
                return None
            return {"protocol": "vmess", "streamSettings": stream, "settings": {"vnext": [{
                "address": data["add"], "port": int(data["port"]),
                "users": [{"id": data["id"], "alterId": int(data.get("aid") or 0),
                           "security": data.get("scy") or "auto"}]}]}}
        if not host or not port:
            return None
        q = _query(link)
        user = urllib.parse.unquote(parsed.username or "")
        pin = _norm_pin(q.get("pinSHA256") or q.get("pin") or "") or _norm_pin(pin)
        if _is_insecure(q) and not pin:
            return None  # без отпечатка Xray не примет самоподписанный сертификат
        if scheme in ("hysteria2", "hy2"):
            # Обфускация (obfs) тестом не поддерживается: без неё результат был бы
            # неточным, поэтому такие подключения не проверяем.
            if q.get("obfs") or q.get("obfs-password"):
                return None
            auth = user + (":" + urllib.parse.unquote(parsed.password) if parsed.password else "")
            tls: Dict[str, Any] = {"serverName": q.get("sni") or host,
                                   "alpn": [a for a in (q.get("alpn") or "h3").split(",") if a]}
            if pin:
                tls["pinnedPeerCertSha256"] = pin
            return {"protocol": "hysteria", "settings": {"version": 2, "address": host, "port": port},
                    "streamSettings": {"network": "hysteria", "security": "tls", "tlsSettings": tls,
                                       "hysteriaSettings": {"version": 2, "auth": auth}}}
        if scheme == "vless":
            stream = _stream_settings(q, host, pin=pin)
            if not stream:
                return None
            u: Dict[str, Any] = {"id": user, "encryption": q.get("encryption") or "none"}
            if q.get("flow"):
                u["flow"] = q["flow"]
            return {"protocol": "vless", "streamSettings": stream,
                    "settings": {"vnext": [{"address": host, "port": port, "users": [u]}]}}
        if scheme == "trojan":
            stream = _stream_settings(q, host, default_security="tls", pin=pin)
            if not stream:
                return None
            return {"protocol": "trojan", "streamSettings": stream,
                    "settings": {"servers": [{"address": host, "port": port, "password": user}]}}
        if scheme == "ss":
            if q.get("plugin"):
                return None
            if parsed.password is not None:
                method, password = urllib.parse.unquote(user), urllib.parse.unquote(parsed.password)
            else:
                raw = urllib.parse.unquote(user)
                decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
                method, _, password = decoded.partition(":")
            return {"protocol": "shadowsocks", "settings": {"servers": [{
                "address": host, "port": port, "method": method, "password": password}]}}
    except Exception as e:  # noqa: BLE001
        logger.debug("Ссылка не преобразована в конфиг Xray: %s", type(e).__name__)
    return None


def skip_reason(item: Dict[str, Any]) -> str:
    """Короткая понятная причина, почему подключение нельзя проверить реальным тестом."""
    proto = str(item.get("protocol") or "").lower()
    q = _query(item.get("link", ""))
    if proto == "tuic":
        return "для TUIC нет клиента"
    if proto in ("wireguard", "amneziawg") or item.get("kind") == "conf":
        return "для WireGuard и AmneziaWG нет клиента"
    if proto in ("hysteria2", "hy2"):
        if q.get("obfs") or q.get("obfs-password"):
            return "обфускация (obfs) не поддерживается тестом"
        if needs_pin(item.get("link", "")):
            return "самоподписанный сертификат без отпечатка"
    if _is_insecure(q) and needs_pin(item.get("link", "")):
        return "самоподписанный сертификат: не удалось закрепить"
    if proto in ("vless", "trojan", "vmess") and (q.get("type") or "tcp").lower() in ("kcp", "quic", "mkcp"):
        return "транспорт не поддерживается тестом"
    return "тип подключения не поддерживается тестом"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def probe_xray(item: Dict[str, Any], xray: str) -> Dict[str, Any]:
    """Проверка 2: реальный запрос в интернет через это подключение."""
    link = item.get("link", "")
    pin = ""
    if needs_pin(link):
        q = _query(link)
        if item.get("protocol") in UDP_PROTOCOLS:
            return {"status": "skip", "detail": skip_reason(item)}
        try:
            pin = await asyncio.get_running_loop().run_in_executor(
                None, _fetch_cert_pin_sync, item["host"], int(item["port"]), q.get("sni") or q.get("host") or "")
        except Exception:  # noqa: BLE001
            return {"status": "bad", "detail": "не удалось получить сертификат сервера"}
    outbound = link_to_outbound(link, pin=pin)
    if outbound is None:
        return {"status": "skip", "detail": skip_reason(item)}
    result = await _run_xray(xray, outbound)
    if result.get("start_failed"):
        # В разных версиях Xray отпечаток сертификата записывается строкой или списком.
        tls = (outbound.get("streamSettings") or {}).get("tlsSettings") or {}
        if isinstance(tls.get("pinnedPeerCertSha256"), str):
            tls["pinnedPeerCertSha256"] = [tls["pinnedPeerCertSha256"]]
            result = await _run_xray(xray, outbound)
    result.pop("start_failed", None)
    return result


async def _run_xray(xray: str, outbound: Dict[str, Any]) -> Dict[str, Any]:
    port = _free_port()
    config = {
        "log": {"loglevel": "none"},
        "inbounds": [{"listen": "127.0.0.1", "port": port, "protocol": "http", "settings": {}}],
        "outbounds": [{**outbound, "tag": "probe"}],
    }
    tmpdir = tempfile.mkdtemp(prefix="eclipse-probe-")
    proc = None
    try:
        cfg_path = Path(tmpdir) / "c.json"
        cfg_path.write_text(json.dumps(config), encoding="utf-8")
        os.chmod(cfg_path, 0o600)
        proc = await asyncio.create_subprocess_exec(
            xray, "run", "-c", str(cfg_path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        for _ in range(30):  # ждём, пока Xray откроет локальный порт (до 3 с)
            if proc.returncode is not None:
                return {"status": "skip", "detail": "Xray не запустился (проверьте его версию)", "start_failed": True}
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                await asyncio.sleep(0.1)
        import aiohttp
        started = time.monotonic()
        last_error = "нет ответа"
        timeout = aiohttp.ClientTimeout(total=XRAY_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for url in TEST_URLS:
                try:
                    async with session.get(url, proxy=f"http://127.0.0.1:{port}") as resp:
                        if resp.status in (200, 204):
                            ms = int((time.monotonic() - started) * 1000)
                            return {"status": "ok", "detail": f"трафик идёт, {ms} мс", "ms": ms}
                        last_error = ("подключение не установилось" if resp.status >= 500
                                      else f"ответ {resp.status}")
                except asyncio.TimeoutError:
                    last_error = "таймаут: трафик не идёт"
                except Exception as e:  # noqa: BLE001
                    last_error = "трафик не идёт"
                    logger.debug("Тест через Xray: %s", type(e).__name__)
        return {"status": "bad", "detail": last_error}
    finally:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), 3)
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(tmpdir, ignore_errors=True)


# ------------------------------------------------------------------ общий прогон

def combine(basic: Dict[str, Any], real: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Итог по подключению. Реальный тест главнее: он показывает, идёт ли трафик."""
    if basic["status"] == "bad":
        return {"status": "bad", "detail": basic["detail"], "level": "порт"}
    if real and real["status"] == "bad":
        return {"status": "bad", "detail": real["detail"], "level": "тест"}
    if real and real["status"] == "ok":
        status = "warn" if basic["status"] == "warn" else "ok"
        detail = real["detail"] if status == "ok" else f"{real['detail']}; {basic['detail']}"
        return {"status": status, "detail": detail, "level": "тест"}
    if basic["status"] in ("ok", "warn"):
        return {"status": basic["status"], "detail": basic["detail"], "level": "порт"}
    reason = real["detail"] if real and real["status"] == "skip" else basic["detail"]
    return {"status": "skip", "detail": reason, "level": ""}


def _label(item: Dict[str, Any]) -> str:
    name = str(item.get("name") or item.get("protocol_label") or "подключение")
    proto = str(item.get("protocol_label") or "").strip()
    sec = str(item.get("security_label") or "").strip()
    tail = " ".join(x for x in (proto, sec) if x and x.lower() not in name.lower())
    return f"{name} · {tail}" if tail else name


async def probe_all(key: Dict[str, Any], use_xray: bool = True) -> Dict[str, Any]:
    """Проверяет все подключения ключа. Возвращает {results, xray, error}."""
    xray = find_xray() if use_xray else None
    try:
        items = await load_links(key)
    except Exception as e:  # noqa: BLE001
        from bot.services.system_health import _short_error
        return {"results": [], "xray": bool(xray), "error": _short_error(e)}
    sem = asyncio.Semaphore(XRAY_PARALLEL)

    async def one(item: Dict[str, Any]) -> Dict[str, Any]:
        basic = await probe_basic(item)
        real = None
        if xray and basic["status"] != "bad":
            async with sem:
                real = await probe_xray(item, xray)
        res = combine(basic, real)
        res["title"] = _label(item)
        res["proto"] = str(item.get("protocol_label") or item.get("protocol") or "").strip()
        return res

    results = await asyncio.gather(*(one(i) for i in items), return_exceptions=True)
    clean = [r for r in results if isinstance(r, dict)]
    summary = {"at": time.time(), "total": len(clean),
               "bad": sum(1 for r in clean if r["status"] == "bad"),
               "warn": sum(1 for r in clean if r["status"] == "warn"),
               "ok": sum(1 for r in clean if r["status"] == "ok"),
               "skip": sum(1 for r in clean if r["status"] == "skip"),
               "real": bool(xray)}
    last_summary.clear()
    last_summary.update(summary)
    return {"results": clean, "xray": bool(xray), "error": ""}


async def probe_configured() -> Optional[Dict[str, Any]]:
    """Проверка выбранного ключа. None, если ключ не выбран или не найден."""
    key_id = get_key_id()
    if not key_id or not is_licensed():
        return None
    from database.db_keys import get_vpn_key_by_id
    key = await asyncio.get_running_loop().run_in_executor(None, get_vpn_key_by_id, key_id)
    if not key:
        return None
    return await probe_all(key)


# ------------------------------------------------------------------ представление

_ICON = {"ok": "✅", "warn": "⚠️", "bad": "🔴", "skip": "⚪"}


def to_checks(report: Dict[str, Any]) -> List[Check]:
    """Проверки для оповещений: на каждое подключение строка (значок, название, подробности)."""
    from bot.services.system_health import _hide_addresses
    if report.get("error"):
        return [("🔴", "Подключения ключа для проверки", _hide_addresses(report["error"]))]
    return [(_ICON[r["status"]], f"Подключение {r['title']}", _hide_addresses(r["detail"]))
            for r in report.get("results", [])]


def check_connections_summary() -> Check:
    """Строка для экрана «Диагностика»: итог последней проверки подключений."""
    if get_key_id() is None:
        return ("⚪", "Подключения", "ключ для проверки не выбран")
    if not last_summary:
        return ("⚪", "Подключения", "ещё не проверялись")
    age = int((time.time() - last_summary["at"]) // 60)
    when = "только что" if age < 1 else f"{age} мин назад"
    checked = last_summary["total"] - last_summary.get("skip", 0)
    text = f"проверено {checked} из {last_summary['total']}, не работает {last_summary['bad']} ({when})"
    if last_summary["bad"]:
        return ("🔴", "Подключения", text)
    if last_summary["warn"]:
        return ("⚠️", "Подключения", text)
    return ("✅", "Подключения", text)


def format_report(report: Dict[str, Any]) -> str:
    from bot.utils.text import escape_html
    lines = ["🧪 <b>Проверка подключений</b>", ""]
    if report.get("error"):
        lines.append(f"🔴 Не удалось получить подключения ключа: {escape_html(report['error'])}")
        return "\n".join(lines)
    results = report.get("results", [])
    if not results:
        lines.append("У ключа нет подключений.")
        return "\n".join(lines)
    order = {"bad": 0, "warn": 1, "ok": 2}
    shown = [r for r in results if r["status"] != "skip"]
    skipped = [r for r in results if r["status"] == "skip"]
    for r in sorted(shown, key=lambda x: order.get(x["status"], 9)):
        lines.append(f"{_ICON[r['status']]} <b>{escape_html(r['title'])}</b>\n    {escape_html(r['detail'])}")
    if skipped:
        groups: Dict[Tuple[str, str], int] = {}
        for r in skipped:
            key = (r.get("proto") or "прочие", r.get("detail") or "проверка недоступна")
            groups[key] = groups.get(key, 0) + 1
        lines.append(f"⚪ <b>Не проверяются ({len(skipped)})</b>")
        for (proto, reason), n in groups.items():
            lines.append(f"    {escape_html(proto)} ×{n}: {escape_html(reason)}")
    bad = sum(1 for r in results if r["status"] == "bad")
    ok = sum(1 for r in results if r["status"] == "ok")
    lines.append("")
    lines.append(f"Всего: {len(results)}, работает: {ok}, не работает: {bad}, не проверялись: {len(skipped)}")
    if report.get("xray"):
        lines.append("Режим: реальный тест через Xray + проверка порта.")
    else:
        lines.append("Режим: только проверка порта и TLS. Реальный тест недоступен: Xray не найден на сервере бота.")
    lines.append("")
    lines.append("<i>Проверка идёт с сервера бота, а не из сети клиента: она не покажет блокировку у конкретного оператора.</i>")
    return "\n".join(lines)
