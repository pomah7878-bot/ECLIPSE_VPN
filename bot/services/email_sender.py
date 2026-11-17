"""
Письма с кодом входа в личный кабинет сайта и приложения.

Настройки берутся из админки бота (таблица settings) и, если поле там пустое,
из переменных окружения (secrets.env):
  SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SMTP_FROM
  SMTP_PORT — необязательно: 465 (SSL, по умолчанию) или 587 (STARTTLS)
Если не хватает хоть одного обязательного поля — вход по Email выключен.
"""
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from typing import Dict, Optional

_SMTP_TIMEOUT_SECONDS = 15
# Только ASCII: smtplib не отправит на адрес с кириллицей без SMTPUTF8
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]{1,64}@(?:[A-Za-z0-9-]+\.)+[A-Za-z0-9-]{2,}")

# поле в админке → (ключ настройки, переменная окружения)
SMTP_FIELDS = (
    ("host", "smtp_host", "SMTP_HOST"),
    ("port", "smtp_port", "SMTP_PORT"),
    ("user", "smtp_user", "SMTP_USER"),
    ("password", "smtp_password", "SMTP_PASSWORD"),
    ("from", "smtp_from", "SMTP_FROM"),
)

REQUIRED_FIELDS = ("host", "user", "password", "from")


def normalize_email(raw) -> Optional[str]:
    """Адрес в нижнем регистре или None, если это не похоже на email."""
    if not isinstance(raw, str):
        return None
    email = raw.strip().lower()
    return email if len(email) <= 254 and _EMAIL_RE.fullmatch(email) else None


def smtp_config() -> Dict[str, str]:
    """Действующие настройки SMTP: значение из админки важнее secrets.env.

    Отдельное поле можно оставить пустым — тогда возьмётся переменная
    окружения: админ может, например, поменять только пароль.
    """
    from database.db_settings import get_setting

    cfg: Dict[str, str] = {}
    for field, key, env_name in SMTP_FIELDS:
        value = ""
        try:
            value = str(get_setting(key, "") or "").strip()
        except Exception:  # noqa: BLE001 — до миграций settings может ещё не быть
            value = ""
        if not value:
            value = (os.getenv(env_name, "") or "").strip()
        cfg[field] = value
    return cfg


def is_email_configured() -> bool:
    cfg = smtp_config()
    return all(cfg.get(field) for field in REQUIRED_FIELDS)


def send_login_code(to_email: str, code: str, brand_name: str) -> None:
    """Отправляет код входа. Блокирующая: вызывать через asyncio.to_thread."""
    send_message(
        to_email,
        f"Код входа в {brand_name}: {code}",
        f"Код входа в {brand_name}: {code}\n\n"
        "• Действует 10 минут\n"
        "• Никому его не сообщайте\n\n"
        "Если вы не запрашивали код, просто удалите это письмо.\n",
    )


def send_test_message(to_email: str, brand_name: str) -> None:
    """Проверочное письмо из админки: подтверждает, что SMTP настроен верно."""
    send_message(
        to_email,
        f"{brand_name}: проверка почты",
        f"Это проверочное письмо {brand_name}.\n\n"
        "Если вы его видите, отправка почты настроена верно: коды входа и "
        "подтверждения смены почты будут доходить.\n",
    )


def send_message(to_email: str, subject: str, body: str) -> None:
    """Отправка письма с настройками из админки (или secrets.env). Блокирующая."""
    cfg = smtp_config()
    missing = [field for field in REQUIRED_FIELDS if not cfg.get(field)]
    if missing:
        raise RuntimeError("SMTP не настроен: " + ", ".join(missing))

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from"]
    msg["To"] = to_email
    msg.set_content(body)

    host = cfg["host"]
    try:
        port = int(cfg.get("port") or 465)
    except ValueError:
        port = 465
    context = ssl.create_default_context()
    if port == 465:
        smtp = smtplib.SMTP_SSL(host, port, timeout=_SMTP_TIMEOUT_SECONDS, context=context)
    else:
        smtp = smtplib.SMTP(host, port, timeout=_SMTP_TIMEOUT_SECONDS)
    with smtp:
        if port != 465:
            smtp.starttls(context=context)
        smtp.login(cfg["user"], cfg["password"])
        smtp.send_message(msg)
