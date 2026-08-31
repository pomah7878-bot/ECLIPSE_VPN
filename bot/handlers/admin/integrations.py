"""
Router of the "Интеграции" section — domain, AI key, OAuth providers.

Lets the admin configure these directly from the bot if they weren't
provided (or need changing) after installation, instead of editing
config.py/secrets.env by hand on the server.
"""
import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext

from database.requests import (
    get_effective_webapp_url, set_webapp_url,
    get_effective_groq_api_key, set_groq_api_key,
    get_effective_gemini_api_key, set_gemini_api_key,
    get_effective_tavily_api_key, set_tavily_api_key,
    get_effective_oauth_credentials, set_oauth_credentials,
    get_effective_brand_name, set_brand_name,
    get_effective_own_app_name, set_own_app_name,
    get_effective_own_app_url, set_own_app_url,
)
from bot.states.admin_states import AdminStates
from bot.utils.admin import is_admin
from bot.utils.text import get_message_text_for_storage, safe_edit_or_send
from bot.keyboards.admin import integrations_menu_kb, integrations_edit_cancel_kb, back_and_home_kb

logger = logging.getLogger(__name__)
router = Router()

_PROVIDER_NAMES = {"google": "Google", "yandex": "Яндекс", "vk": "VK"}


def _mask_secret(value: str) -> str:
    """Маскирует секрет для отображения — показывает только последние 4 символа."""
    if not value:
        return "не задан"
    if len(value) <= 8:
        return "•" * len(value)
    return "•" * (len(value) - 4) + value[-4:]


@router.callback_query(F.data == "admin_integrations")
async def show_integrations_menu(callback: CallbackQuery, state: FSMContext):
    """Показывает статус всех интеграций."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.set_state(AdminStates.integrations_menu)

    webapp_url = get_effective_webapp_url()
    brand_name = get_effective_brand_name()
    own_app_name = get_effective_own_app_name()
    groq_key = get_effective_groq_api_key()
    gemini_key = get_effective_gemini_api_key()
    tavily_key = get_effective_tavily_api_key()

    lines = [
        "🌐 <b>Интеграции</b>\n",
        f"🌐 Домен сайта: <code>{webapp_url or 'не задан'}</code>",
        f"🏷 Название бренда (для AI): <code>{brand_name}</code>",
        f"📱 Своё приложение: <code>{own_app_name or 'не рекомендуется (только Happ/INCY)'}</code>",
        f"🤖 Ключ AI (Groq): <code>{_mask_secret(groq_key)}</code>",
        f"✨ Ключ AI (Gemini): <code>{_mask_secret(gemini_key)}</code>",
        f"🔍 Ключ веб-поиска (Tavily): <code>{_mask_secret(tavily_key)}</code>",
        "",
    ]
    for provider, name in _PROVIDER_NAMES.items():
        client_id, client_secret = get_effective_oauth_credentials(provider)
        status = "🟢 настроен" if (client_id and client_secret) else "⚪ не настроен"
        lines.append(f"{name} OAuth: {status}")

    lines.append(
        "\nИзменения домена/AI применяются сразу. Для OAuth и AI-ключа "
        "может понадобиться перезапуск соответствующего сервиса — "
        "спросите поддержку, если что-то не заработает сразу."
    )

    await safe_edit_or_send(callback.message, "\n".join(lines), reply_markup=integrations_menu_kb())
    await callback.answer()


# ============================================================
# Домен сайта
# ============================================================

@router.callback_query(F.data == "admin_edit_webapp_url")
async def edit_webapp_url_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.set_state(AdminStates.edit_webapp_url)
    current = get_effective_webapp_url()
    await safe_edit_or_send(
        callback.message,
        f"🌐 <b>Домен сайта</b>\n\nТекущий: <code>{current or 'не задан'}</code>\n\n"
        "Отправьте новый адрес, например:\n<code>https://мой-домен.ru</code>\n\n"
        "Домен должен быть уже настроен с SSL (nginx + сертификат) и указывать на этот сервер.",
        reply_markup=integrations_edit_cancel_kb(),
    )
    await callback.answer()


@router.message(AdminStates.edit_webapp_url)
async def edit_webapp_url_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    value = get_message_text_for_storage(message, "plain").strip()
    if not (value.startswith("https://") or value.startswith("http://")):
        await safe_edit_or_send(message, "❌ Адрес должен начинаться с https:// или http://. Попробуйте ещё раз.")
        return

    try:
        await message.delete()
    except Exception:
        pass

    set_webapp_url(value)
    await state.set_state(AdminStates.integrations_menu)
    await message.answer(f"✅ Домен сохранён: <code>{value}</code>", parse_mode="HTML", reply_markup=integrations_menu_kb())


# ============================================================
# Название бренда (для текстов AI-помощника)
# ============================================================

@router.callback_query(F.data == "admin_edit_brand_name")
async def edit_brand_name_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.set_state(AdminStates.edit_brand_name)
    current = get_effective_brand_name()
    await safe_edit_or_send(
        callback.message,
        f"🏷 <b>Название бренда</b>\n\nТекущее: <code>{current}</code>\n\n"
        "Как AI-помощник должен называть ваш сервис, отвечая клиентам "
        "(например: <code>EDITION</code>, <code>MyVPN</code>). Отправьте новое название.\n\n"
        "⚠️ После сохранения перезапустите AI-сервис на сервере: <code>systemctl restart eclipse-ai</code>",
        reply_markup=integrations_edit_cancel_kb(),
    )
    await callback.answer()


@router.message(AdminStates.edit_brand_name)
async def edit_brand_name_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    value = get_message_text_for_storage(message, "plain").strip()
    if not value or len(value) > 64:
        await safe_edit_or_send(message, "❌ Название должно быть непустым и короче 64 символов. Попробуйте ещё раз.")
        return

    try:
        await message.delete()
    except Exception:
        pass

    set_brand_name(value)
    await state.set_state(AdminStates.integrations_menu)
    await message.answer(
        f"✅ Название бренда сохранено: <code>{value}</code>\n\n"
        "⚠️ Не забудьте: <code>systemctl restart eclipse-ai</code>",
        parse_mode="HTML", reply_markup=integrations_menu_kb(),
    )


# ============================================================
# Своё приложение (AI рекомендует его в первую очередь, если задано)
# ============================================================

@router.callback_query(F.data == "admin_edit_own_app")
async def edit_own_app_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.set_state(AdminStates.edit_own_app_name)
    current_name = get_effective_own_app_name()
    current_url = get_effective_own_app_url()
    current_text = f"{current_name} — {current_url}" if current_name else "не задано (рекомендуются только Happ/INCY)"
    await safe_edit_or_send(
        callback.message,
        f"📱 <b>Своё приложение</b>\n\nТекущее: <code>{current_text}</code>\n\n"
        "Если у вас есть собственный VPN-клиент — AI-помощник будет рекомендовать "
        "именно его в первую очередь. Отправьте название и ссылку в двух строках:\n"
        "<code>Название\nhttps://ссылка-на-приложение</code>\n\n"
        "Чтобы убрать (рекомендовать только сторонние клиенты) — отправьте одно тире: <code>-</code>\n\n"
        "⚠️ После сохранения перезапустите AI-сервис на сервере: <code>systemctl restart eclipse-ai</code>",
        reply_markup=integrations_edit_cancel_kb(),
    )
    await callback.answer()


@router.message(AdminStates.edit_own_app_name)
async def edit_own_app_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    raw = get_message_text_for_storage(message, "plain").strip()

    try:
        await message.delete()
    except Exception:
        pass

    if raw == "-":
        set_own_app_name("")
        set_own_app_url("")
        await state.set_state(AdminStates.integrations_menu)
        await message.answer(
            "✅ Своё приложение убрано — AI будет рекомендовать только сторонние клиенты (Happ, INCY).\n\n"
            "⚠️ Не забудьте: <code>systemctl restart eclipse-ai</code>",
            parse_mode="HTML", reply_markup=integrations_menu_kb(),
        )
        return

    lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
    if len(lines) != 2 or not (lines[1].startswith("https://") or lines[1].startswith("http://")):
        await safe_edit_or_send(
            message,
            "❌ Нужно ровно две строки: название на первой, ссылка (начинающаяся с https://) на второй. "
            "Или отправьте <code>-</code>, чтобы убрать рекомендацию своего приложения.",
        )
        return

    name, url = lines
    set_own_app_name(name)
    set_own_app_url(url)
    await state.set_state(AdminStates.integrations_menu)
    await message.answer(
        f"✅ Своё приложение сохранено: <code>{name}</code> — <code>{url}</code>\n\n"
        "⚠️ Не забудьте: <code>systemctl restart eclipse-ai</code>",
        parse_mode="HTML", reply_markup=integrations_menu_kb(),
    )


# ============================================================
# Ключ AI (Groq)
# ============================================================

@router.callback_query(F.data == "admin_edit_groq_key")
async def edit_groq_key_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.set_state(AdminStates.edit_groq_key)
    current = get_effective_groq_api_key()
    await safe_edit_or_send(
        callback.message,
        f"🤖 <b>Ключ AI (Groq)</b>\n\nТекущий: <code>{_mask_secret(current)}</code>\n\n"
        "Получите бесплатный ключ на console.groq.com → API Keys, затем отправьте его сюда.\n\n"
        "⚠️ После сохранения перезапустите AI-сервис на сервере: <code>systemctl restart eclipse-ai</code>",
        reply_markup=integrations_edit_cancel_kb(),
    )
    await callback.answer()


@router.message(AdminStates.edit_groq_key)
async def edit_groq_key_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    value = get_message_text_for_storage(message, "plain").strip()
    if len(value) < 10:
        await safe_edit_or_send(message, "❌ Слишком короткое значение. Проверьте, что скопировали ключ целиком.")
        return

    try:
        await message.delete()
    except Exception:
        pass

    set_groq_api_key(value)
    await state.set_state(AdminStates.integrations_menu)
    await message.answer(
        f"✅ Ключ сохранён: <code>{_mask_secret(value)}</code>\n\n"
        "⚠️ Не забудьте: <code>systemctl restart eclipse-ai</code>",
        parse_mode="HTML", reply_markup=integrations_menu_kb(),
    )


# ============================================================
# Ключ AI (Gemini) — резервный лейн
# ============================================================

@router.callback_query(F.data == "admin_edit_gemini_key")
async def edit_gemini_key_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.set_state(AdminStates.edit_gemini_key)
    current = get_effective_gemini_api_key()
    await safe_edit_or_send(
        callback.message,
        f"✨ <b>Ключ AI (Gemini)</b>\n\nТекущий: <code>{_mask_secret(current)}</code>\n\n"
        "Резервный лейн — используется, когда все модели Groq недоступны. "
        "Получите бесплатный ключ на aistudio.google.com → Get API key, затем отправьте его сюда.\n\n"
        "⚠️ После сохранения перезапустите AI-сервис на сервере: <code>systemctl restart eclipse-ai</code>",
        reply_markup=integrations_edit_cancel_kb(),
    )
    await callback.answer()


@router.message(AdminStates.edit_gemini_key)
async def edit_gemini_key_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    value = get_message_text_for_storage(message, "plain").strip()
    if len(value) < 10:
        await safe_edit_or_send(message, "❌ Слишком короткое значение. Проверьте, что скопировали ключ целиком.")
        return

    try:
        await message.delete()
    except Exception:
        pass

    set_gemini_api_key(value)
    await state.set_state(AdminStates.integrations_menu)
    await message.answer(
        f"✅ Ключ сохранён: <code>{_mask_secret(value)}</code>\n\n"
        "⚠️ Не забудьте: <code>systemctl restart eclipse-ai</code>",
        parse_mode="HTML", reply_markup=integrations_menu_kb(),
    )


# ============================================================
# Ключ веб-поиска (Tavily)
# ============================================================

@router.callback_query(F.data == "admin_edit_tavily_key")
async def edit_tavily_key_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.set_state(AdminStates.edit_tavily_key)
    current = get_effective_tavily_api_key()
    await safe_edit_or_send(
        callback.message,
        f"🔍 <b>Ключ веб-поиска (Tavily)</b>\n\nТекущий: <code>{_mask_secret(current)}</code>\n\n"
        "Используется AI-консультантом для поиска актуальной информации в интернете.\n"
        "Получите бесплатный ключ на tavily.com → API Keys, затем отправьте его сюда.\n\n"
        "⚠️ После сохранения перезапустите AI-сервис на сервере: <code>systemctl restart eclipse-ai</code>",
        reply_markup=integrations_edit_cancel_kb(),
    )
    await callback.answer()


@router.message(AdminStates.edit_tavily_key)
async def edit_tavily_key_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    value = get_message_text_for_storage(message, "plain").strip()
    if len(value) < 10:
        await safe_edit_or_send(message, "❌ Слишком короткое значение. Проверьте, что скопировали ключ целиком.")
        return

    try:
        await message.delete()
    except Exception:
        pass

    set_tavily_api_key(value)
    await state.set_state(AdminStates.integrations_menu)
    await message.answer(
        f"✅ Ключ сохранён: <code>{_mask_secret(value)}</code>\n\n"
        "⚠️ Не забудьте: <code>systemctl restart eclipse-ai</code>",
        parse_mode="HTML", reply_markup=integrations_menu_kb(),
    )


# ============================================================
# OAuth-провайдеры (Google / Яндекс / VK)
# ============================================================

@router.callback_query(F.data.startswith("admin_edit_oauth:"))
async def edit_oauth_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    provider = callback.data.split(":")[1]
    if provider not in _PROVIDER_NAMES:
        await callback.answer("Неизвестный провайдер", show_alert=True)
        return

    await state.update_data(oauth_provider=provider)
    await state.set_state(AdminStates.edit_oauth_client_id)

    client_id, client_secret = get_effective_oauth_credentials(provider)
    name = _PROVIDER_NAMES[provider]
    await safe_edit_or_send(
        callback.message,
        f"ℹ️ Redirect URI для регистрации приложения {name} (потребуется на сайте {name}, не сюда):\n"
        f"<code>{get_effective_webapp_url()}/auth/{provider}/callback</code>\n\n"
        f"— — —\n\n"
        f"{name} OAuth — <b>Client ID</b>\n\n"
        f"Текущий: <code>{client_id or 'не задан'}</code>\n\n"
        f"Теперь отправьте сюда Client ID из консоли разработчика {name} (не ссылку выше).",
        reply_markup=integrations_edit_cancel_kb(),
    )
    await callback.answer()


@router.message(AdminStates.edit_oauth_client_id)
async def edit_oauth_client_id_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    value = get_message_text_for_storage(message, "plain").strip()
    if len(value) < 3:
        await safe_edit_or_send(message, "❌ Слишком короткое значение. Попробуйте ещё раз.")
        return
    if value.startswith("http://") or value.startswith("https://"):
        await safe_edit_or_send(
            message,
            "❌ Это похоже на ссылку (например, Redirect URI), а не на Client ID.\n"
            "Client ID выглядит как набор цифр/букв из консоли разработчика. Попробуйте ещё раз.",
        )
        return

    try:
        await message.delete()
    except Exception:
        pass

    await state.update_data(oauth_client_id=value)
    await state.set_state(AdminStates.edit_oauth_client_secret)

    data = await state.get_data()
    provider = data.get("oauth_provider")
    name = _PROVIDER_NAMES.get(provider, provider)
    await message.answer(
        f"{name} OAuth — <b>Client Secret</b>\n\nТеперь отправьте Client Secret.",
        parse_mode="HTML", reply_markup=integrations_edit_cancel_kb(),
    )


@router.message(AdminStates.edit_oauth_client_secret)
async def edit_oauth_client_secret_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    value = get_message_text_for_storage(message, "plain").strip()
    if len(value) < 3:
        await safe_edit_or_send(message, "❌ Слишком короткое значение. Попробуйте ещё раз.")
        return
    if value.startswith("http://") or value.startswith("https://"):
        await safe_edit_or_send(
            message,
            "❌ Это похоже на ссылку (например, Redirect URI), а не на Client Secret.\n"
            "Client Secret — это отдельная строка-пароль из консоли разработчика, не ссылка. Попробуйте ещё раз.",
        )
        return

    try:
        await message.delete()
    except Exception:
        pass

    data = await state.get_data()
    provider = data.get("oauth_provider")
    client_id = data.get("oauth_client_id")
    if not provider or not client_id:
        await message.answer("❌ Данные сессии потеряны, начните заново через меню интеграций.")
        await state.set_state(AdminStates.integrations_menu)
        return

    set_oauth_credentials(provider, client_id, value)
    name = _PROVIDER_NAMES.get(provider, provider)
    await state.set_state(AdminStates.integrations_menu)
    await message.answer(
        f"✅ {name} OAuth сохранён: <code>{_mask_secret(value)}</code>",
        parse_mode="HTML", reply_markup=integrations_menu_kb(),
    )
