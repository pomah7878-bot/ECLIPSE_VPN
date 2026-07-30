"""
OAuth-вход в личный кабинет на публичном сайте (Google / Яндекс / VK) —
альтернатива входу по коду доступа. Учётные записи хранятся в
site_accounts, не связаны с Telegram напрямую.

Credentials берутся из переменных окружения (secrets.env):
  GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET
  YANDEX_OAUTH_CLIENT_ID / YANDEX_OAUTH_CLIENT_SECRET
  VK_OAUTH_CLIENT_ID / VK_OAUTH_CLIENT_SECRET
Если для провайдера credentials не заданы — он считается отключённым.
"""
import os
import logging

logger = logging.getLogger(__name__)

OAUTH_PROVIDERS = {
    "google": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "userinfo_url": "https://www.googleapis.com/oauth2/v2/userinfo",
        "scope": "openid email profile",
        "client_id_env": "GOOGLE_OAUTH_CLIENT_ID",
        "client_secret_env": "GOOGLE_OAUTH_CLIENT_SECRET",
    },
    "yandex": {
        "authorize_url": "https://oauth.yandex.ru/authorize",
        "token_url": "https://oauth.yandex.ru/token",
        "userinfo_url": "https://login.yandex.ru/info?format=json",
        "scope": "login:email login:info",
        "client_id_env": "YANDEX_OAUTH_CLIENT_ID",
        "client_secret_env": "YANDEX_OAUTH_CLIENT_SECRET",
    },
    "vk": {
        "authorize_url": "https://oauth.vk.com/authorize",
        "token_url": "https://oauth.vk.com/access_token",
        "userinfo_url": None,  # VK отдаёт email прямо в ответе на token_url
        "scope": "email",
        "client_id_env": "VK_OAUTH_CLIENT_ID",
        "client_secret_env": "VK_OAUTH_CLIENT_SECRET",
        "extra_authorize_params": {"v": "5.199"},
        "extra_token_params": {"v": "5.199"},
    },
}


def is_provider_configured(provider: str) -> bool:
    cfg = OAUTH_PROVIDERS.get(provider)
    if not cfg:
        return False
    from database.requests import get_effective_oauth_credentials
    client_id, client_secret = get_effective_oauth_credentials(provider)
    return bool(client_id) and bool(client_secret)


def get_configured_providers() -> list[str]:
    return [p for p in OAUTH_PROVIDERS if is_provider_configured(p)]


def build_authorize_url(provider: str, redirect_uri: str, state: str) -> str:
    cfg = OAUTH_PROVIDERS[provider]
    from database.requests import get_effective_oauth_credentials
    client_id, _ = get_effective_oauth_credentials(provider)
    from urllib.parse import urlencode


    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": cfg["scope"],
        "state": state,
    }
    params.update(cfg.get("extra_authorize_params", {}))
    return f"{cfg['authorize_url']}?{urlencode(params)}"


async def exchange_code_for_user_info(provider: str, code: str, redirect_uri: str) -> dict:
    """
    Обменивает authorization code на данные пользователя.

    Returns:
        dict: {"provider_user_id": str, "email": str | None, "display_name": str | None}

    Raises:
        RuntimeError: если обмен не удался
    """
    cfg = OAUTH_PROVIDERS[provider]
    from database.requests import get_effective_oauth_credentials
    client_id, client_secret = get_effective_oauth_credentials(provider)

    import aiohttp

    token_params = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    token_params.update(cfg.get("extra_token_params", {}))

    async with aiohttp.ClientSession() as session:
        async with session.post(
            cfg["token_url"], data=token_params,
            headers={"Accept": "application/json"},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            token_data = await resp.json(content_type=None)
            if resp.status != 200 or "access_token" not in token_data:
                logger.error(f"OAuth {provider}: не удалось получить access_token: {token_data}")
                raise RuntimeError(f"Не удалось авторизоваться через {provider}")

        access_token = token_data["access_token"]

        # VK — особый случай: email и user_id уже есть в ответе на token_url
        if provider == "vk":
            return {
                "provider_user_id": str(token_data.get("user_id", "")),
                "email": token_data.get("email"),
                "display_name": None,
            }

        userinfo_url = cfg["userinfo_url"]
        headers = {"Authorization": f"Bearer {access_token}"}
        async with session.get(userinfo_url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            info = await resp.json(content_type=None)
            if resp.status != 200:
                logger.error(f"OAuth {provider}: не удалось получить userinfo: {info}")
                raise RuntimeError(f"Не удалось получить данные профиля от {provider}")

    if provider == "google":
        return {
            "provider_user_id": str(info.get("id", "")),
            "email": info.get("email"),
            "display_name": info.get("name"),
        }
    if provider == "yandex":
        return {
            "provider_user_id": str(info.get("id", "")),
            "email": info.get("default_email"),
            "display_name": info.get("real_name") or info.get("display_name"),
        }
    raise RuntimeError(f"Неизвестный провайдер: {provider}")
