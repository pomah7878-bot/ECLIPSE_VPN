"""
Письма с кодом входа в личный кабинет сайта и приложения.

Настройки берутся из переменных окружения (secrets.env):
  SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SMTP_FROM
  SMTP_PORT — необязательно: 465 (SSL, по умолчанию) или 587 (STARTTLS)
Если они не заданы — вход по Email считается отключённым.
"""
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from typing import Optional

_SMTP_TIMEOUT_SECONDS = 15
# Только ASCII: smtplib не отправит на адрес с кириллицей без SMTPUTF8
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]{1,64}@(?:[A-Za-z0-9-]+\.)+[A-Za-z0-9-]{2,}")


def normalize_email(raw) -> Optional[str]:
    """Адрес в нижнем регистре или None, если это не похоже на email."""
    if not isinstance(raw, str):
        return None
    email = raw.strip().lower()
    return email if len(email) <= 254 and _EMAIL_RE.fullmatch(email) else None


def is_email_configured() -> bool:
    return all(os.getenv(name) for name in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"))


def send_login_code(to_email: str, code: str, brand_name: str) -> None:
    """Отправляет код входа. Блокирующая: вызывать через asyncio.to_thread."""
    msg = EmailMessage()
    msg["Subject"] = f"Код входа в {brand_name}: {code}"
    msg["From"] = os.environ["SMTP_FROM"]
    msg["To"] = to_email
    msg.set_content(
        f"Код входа в {brand_name}: {code}\n\n"
        "• Действует 10 минут\n"
        "• Никому его не сообщайте\n\n"
        "Если вы не запрашивали код, просто удалите это письмо.\n"
    )

    host, port = os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT") or 465)
    context = ssl.create_default_context()
    if port == 465:
        smtp = smtplib.SMTP_SSL(host, port, timeout=_SMTP_TIMEOUT_SECONDS, context=context)
    else:
        smtp = smtplib.SMTP(host, port, timeout=_SMTP_TIMEOUT_SECONDS)
    with smtp:
        if port != 465:
            smtp.starttls(context=context)
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(msg)
