"""
Автонастройка резервного домена сайта: DNS (если Cloudflare) + nginx + SSL.

Провайдер-специфична только СОЗДАНИЕ DNS-записи (сейчас поддержан
Cloudflare через API-токен). Ожидание распространения DNS, генерация и
безопасное применение конфига nginx, выпуск SSL-сертификата и финальная
проверка HTTPS — универсальны для любого регистратора, поэтому даже без
Cloudflare человеку остаётся только одно ручное действие (добавить
A-запись), а не четыре.

Каждый шаг перед изменением ЧЕГО-ЛИБО в системе (nginx-конфиг) проверяется
(`nginx -t`) — если проверка не проходит, ничего не применяется и не
перезагружается, старый рабочий конфиг остаётся как был.
"""
import asyncio
import logging
import os
import socket
import shutil
from pathlib import Path
from typing import AsyncGenerator, Optional, Tuple

import aiohttp

logger = logging.getLogger(__name__)

NGINX_SITES_AVAILABLE = Path("/etc/nginx/sites-available")
NGINX_SITES_ENABLED = Path("/etc/nginx/sites-enabled")
WEBAPP_LOCAL_PORT = 3000  # см. bot/webapp/server.py — "WebApp started on http://127.0.0.1:3000"


class ProvisionError(Exception):
    """Ошибка на одном из шагов автонастройки — с понятным для админа текстом."""


async def get_server_public_ip() -> str:
    """Публичный IP этого сервера (для DNS A-записи)."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
        async with session.get("https://api.ipify.org") as resp:
            resp.raise_for_status()
            return (await resp.text()).strip()


# ============================================================
# DNS: Cloudflare (опционально — если у админа настроен API-токен)
# ============================================================

async def cloudflare_create_or_update_a_record(api_token: str, domain: str, ip: str) -> Tuple[bool, str]:
    """Находит зону Cloudflare, к которой принадлежит домен, и создаёт
    (или обновляет, если уже есть) A-запись, указывающую на ip.

    domain может быть как самим доменом зоны ("example.com"), так и
    поддоменом ("sub.example.com") — зона определяется автоматически по
    самому длинному совпадающему суффиксу среди зон аккаунта.
    """
    headers = {"Authorization": f"Bearer {api_token}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), headers=headers) as session:
        async def _json(resp):
            try:
                return await resp.json()
            except Exception:
                text = (await resp.text())[:200]
                return {"success": False, "errors": [{"message": f"HTTP {resp.status}: {text}"}]}

        # 1. Ищем зону, которой принадлежит домен (перебираем суффиксы)
        parts = domain.split(".")
        zone_id = None
        zone_name = None
        for i in range(len(parts) - 1):
            candidate = ".".join(parts[i:])
            async with session.get(
                "https://api.cloudflare.com/client/v4/zones",
                params={"name": candidate},
            ) as resp:
                data = await _json(resp)
            if not data.get("success") and data.get("errors"):
                errors = "; ".join(e.get("message", "?") for e in data.get("errors", []))
                return False, f"Cloudflare отклонил запрос (проверьте токен): {errors}"
            if data.get("success") and data.get("result"):
                zone_id = data["result"][0]["id"]
                zone_name = candidate
                break
        if not zone_id:
            return False, f"Не нашёл зону Cloudflare для домена {domain} — проверьте, что домен добавлен в ваш аккаунт Cloudflare."

        # 2. Ищем существующую A-запись с этим именем
        async with session.get(
            f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records",
            params={"type": "A", "name": domain},
        ) as resp:
            data = await _json(resp)
        existing = data.get("result") or []

        payload = {"type": "A", "name": domain, "content": ip, "ttl": 300, "proxied": False}
        if existing:
            record_id = existing[0]["id"]
            async with session.put(
                f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records/{record_id}",
                json=payload,
            ) as resp:
                data = await _json(resp)
        else:
            async with session.post(
                f"https://api.cloudflare.com/client/v4/zones/{zone_id}/dns_records",
                json=payload,
            ) as resp:
                data = await _json(resp)

        if not data.get("success"):
            errors = "; ".join(e.get("message", "?") for e in data.get("errors", []))
            return False, f"Cloudflare отклонил запись: {errors or 'неизвестная ошибка'}"
        return True, f"A-запись {domain} → {ip} создана в зоне {zone_name}"


# ============================================================
# Ожидание распространения DNS — универсально, без привязки к провайдеру
# ============================================================

async def wait_for_dns_propagation(domain: str, expected_ip: str, timeout: float = 240, interval: float = 5) -> bool:
    """Опрашивает резолвинг домена, пока он не начнёт указывать на
    expected_ip, либо пока не истечёт timeout секунд. Резолвинг выполняется
    в отдельном потоке (socket.gethostbyname блокирующий)."""
    loop = asyncio.get_event_loop()
    elapsed = 0.0
    while elapsed < timeout:
        try:
            resolved = await loop.run_in_executor(None, socket.gethostbyname, domain)
            if resolved == expected_ip:
                return True
        except socket.gaierror:
            pass
        await asyncio.sleep(interval)
        elapsed += interval
    return False


# ============================================================
# nginx: генерация и БЕЗОПАСНОЕ применение конфига
# ============================================================

def generate_nginx_config(domain: str) -> str:
    """HTTP-конфиг (порт 80) — certbot сам допишет 443/SSL при выпуске
    сертификата (certbot --nginx делает это автоматически)."""
    return f"""server {{
    listen 80;
    listen [::]:80;
    server_name {domain};

    location / {{
        proxy_pass http://127.0.0.1:{WEBAPP_LOCAL_PORT};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
}}
"""


async def _run(*args: str) -> Tuple[int, str, str]:
    """Запускает системную команду без участия shell (без риска инъекций),
    возвращает (код_возврата, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def install_and_reload_nginx(domain: str) -> Tuple[bool, str]:
    """Пишет конфиг для domain, проверяет его (`nginx -t`) ДО перезагрузки
    — если проверка не прошла, ничего не трогает и откатывает файл.
    Существующие конфиги других доменов не затрагиваются вообще."""
    config_path = NGINX_SITES_AVAILABLE / f"{domain}.conf"
    enabled_path = NGINX_SITES_ENABLED / f"{domain}.conf"

    if config_path.exists():
        return False, f"Конфиг {config_path} уже существует — не перезаписываю, разберитесь вручную."

    try:
        config_path.write_text(generate_nginx_config(domain), encoding="utf-8")
        if not enabled_path.exists():
            enabled_path.symlink_to(config_path)

        code, out, err = await _run("nginx", "-t")
        if code != 0:
            # Проверка не прошла — откатываем, ничего не перезагружаем
            try:
                enabled_path.unlink(missing_ok=True)
                config_path.unlink(missing_ok=True)
            except Exception:
                pass
            return False, f"nginx -t не прошёл, конфиг откачен, действующий nginx не тронут:\n{err.strip() or out.strip()}"

        code, out, err = await _run("systemctl", "reload", "nginx")
        if code != 0:
            return False, f"nginx -t прошёл, но перезагрузка сервиса не удалась:\n{err.strip() or out.strip()}"

        return True, f"nginx настроен и перезагружен для {domain}"
    except PermissionError:
        return False, "Нет прав на запись в /etc/nginx/ — бот должен быть запущен от root (как обычно и есть на этом сервере)."
    except Exception as e:
        return False, f"Ошибка настройки nginx: {e}"


# ============================================================
# SSL: certbot
# ============================================================

async def run_certbot(domain: str, email: Optional[str] = None) -> Tuple[bool, str]:
    if not shutil.which("certbot"):
        return False, "certbot не установлен на сервере (sudo apt install certbot python3-certbot-nginx)."

    args = ["certbot", "--nginx", "-d", domain, "--non-interactive", "--agree-tos", "--redirect"]
    args += ["-m", email] if email else ["--register-unsafely-without-email"]

    code, out, err = await _run(*args)
    if code != 0:
        return False, f"certbot не смог выпустить сертификат:\n{(err or out).strip()[-800:]}"
    return True, f"SSL-сертификат для {domain} выпущен"


async def verify_https(domain: str) -> Tuple[bool, str]:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.get(f"https://{domain}/", ssl=True) as resp:
                if resp.status < 500:
                    return True, f"https://{domain} отвечает (HTTP {resp.status})"
                return False, f"https://{domain} отвечает с ошибкой сервера (HTTP {resp.status})"
    except Exception as e:
        return False, f"https://{domain} пока не отвечает: {e}"


# ============================================================
# Оркестратор — вызывается из обработчика в боте
# ============================================================

async def provision_backup_domain(
    domain: str,
    cloudflare_api_token: Optional[str] = None,
    dns_wait_timeout: float = 240,
) -> AsyncGenerator[str, None]:
    """Асинхронный генератор — yield'ит текст прогресса на каждом шаге,
    чтобы обработчик в боте мог обновлять сообщение админу вживую.
    Последний yield начинается с "✅" при полном успехе, либо с "❌" — на
    том шаге, где всё остановилось (дальнейшие шаги не выполняются).

    Любое непредвиденное исключение на любом шаге ловится и превращается
    в аккуратное "❌"-сообщение — обработчик в боте никогда не должен
    упасть из-за этой функции."""
    try:
        async for step in _provision_backup_domain_impl(domain, cloudflare_api_token, dns_wait_timeout):
            yield step
    except Exception as e:
        logger.exception(f"provision_backup_domain: непредвиденная ошибка для {domain}")
        yield f"❌ Непредвиденная ошибка автонастройки: {e}"


async def _provision_backup_domain_impl(
    domain: str,
    cloudflare_api_token: Optional[str],
    dns_wait_timeout: float,
) -> AsyncGenerator[str, None]:
    domain = domain.strip().lower().rstrip(".")

    yield f"🔎 Узнаю публичный IP этого сервера..."
    try:
        server_ip = await get_server_public_ip()
    except Exception as e:
        yield f"❌ Не удалось узнать IP сервера: {e}"
        return
    yield f"✅ IP сервера: {server_ip}"

    if cloudflare_api_token:
        yield f"☁️ Создаю A-запись в Cloudflare: {domain} → {server_ip}..."
        ok, msg = await cloudflare_create_or_update_a_record(cloudflare_api_token, domain, server_ip)
        if not ok:
            yield f"❌ {msg}"
            return
        yield f"✅ {msg}"
    else:
        yield (
            f"📋 Cloudflare-токен не настроен. Добавьте вручную у вашего регистратора:\n\n"
            f"Тип: A\nИмя: {domain}\nЗначение: {server_ip}\n\n⏳ Жду, пока запись распространится..."
        )

    yield f"⏳ Проверяю распространение DNS (до {int(dns_wait_timeout)} сек)..."
    propagated = await wait_for_dns_propagation(domain, server_ip, timeout=dns_wait_timeout)
    if not propagated:
        yield (
            f"❌ Домен {domain} за отведённое время так и не стал указывать на {server_ip}. "
            f"Проверьте DNS-запись и попробуйте ещё раз — остальные шаги (nginx, SSL) не выполнялись."
        )
        return
    yield f"✅ DNS распространился: {domain} → {server_ip}"

    yield f"⚙️ Настраиваю nginx для {domain}..."
    ok, msg = await install_and_reload_nginx(domain)
    if not ok:
        yield f"❌ {msg}"
        return
    yield f"✅ {msg}"

    yield f"🔒 Выпускаю SSL-сертификат ({domain})..."
    ok, msg = await run_certbot(domain)
    if not ok:
        yield f"❌ {msg}\n\n(nginx уже настроен на порту 80 — можно повторить попытку выпуска SSL позже: certbot --nginx -d {domain})"
        return
    yield f"✅ {msg}"

    yield f"🔍 Проверяю, что https://{domain} реально отвечает..."
    ok, msg = await verify_https(domain)
    if not ok:
        yield f"❌ {msg}\n\nВсе шаги настройки выполнены, но итоговая проверка не прошла — возможно, нужно немного подождать."
        return
    yield f"✅ {msg}"

    yield f"✅ Готово! https://{domain} полностью настроен и готов быть резервным доменом."
