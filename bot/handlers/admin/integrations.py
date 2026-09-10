"""
Router of the "Интеграции" section — domain, AI key, OAuth providers.

Lets the admin configure these directly from the bot if they weren't
provided (or need changing) after installation, instead of editing
config.py/secrets.env by hand on the server.
"""
import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
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
    is_start_import_buttons_enabled, set_start_import_buttons_enabled,
    is_start_balance_button_enabled, set_start_balance_button_enabled,
    is_welcome_page_enabled, set_welcome_page_enabled,
    WELCOME_TEMPLATES, get_welcome_template_id, set_welcome_template_id,
    CABINET_THEMES, get_cabinet_theme_id, set_cabinet_theme_id,
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
    """Показывает категории интеграций (детали — внутри каждой)."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.set_state(AdminStates.integrations_menu)

    await safe_edit_or_send(
        callback.message,
        "🌐 <b>Интеграции</b>\n\n"
        "Выбери раздел — статус каждой настройки виден прямо на кнопке "
        "внутри соответствующего раздела.\n\n"
        "Изменения домена/AI применяются сразу. Для OAuth и AI-ключа "
        "может понадобиться перезапуск соответствующего сервиса — "
        "спросите поддержку, если что-то не заработает сразу.",
        reply_markup=integrations_menu_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin_integrations_site")
async def show_integrations_site_menu(callback: CallbackQuery):
    """Подменю «Сайт и витрина»."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    from bot.keyboards.admin_settings import integrations_site_menu_kb
    await safe_edit_or_send(callback.message, "🌐 <b>Сайт и витрина</b>", reply_markup=integrations_site_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "admin_integrations_auth")
async def show_integrations_auth_menu(callback: CallbackQuery):
    """Подменю «Способы входа на сайт»."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    from bot.keyboards.admin_settings import integrations_auth_menu_kb
    await safe_edit_or_send(
        callback.message,
        "🔐 <b>Способы входа на сайт</b>\n\n"
        "Переключатель работает независимо от того, настроены ли учётные "
        "данные — можно временно скрыть способ, не удаляя сами ключи.",
        reply_markup=integrations_auth_menu_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin_integrations_zvonok")
async def show_integrations_zvonok_menu(callback: CallbackQuery):
    """Подменю «Верификация телефона (Zvonok)»."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    from bot.keyboards.admin_settings import integrations_zvonok_menu_kb
    await safe_edit_or_send(
        callback.message,
        "📞 <b>Верификация телефона (Zvonok)</b>\n\n"
        "Нужна для входа по номеру телефона на сайте — регистрация на "
        "zvonok.com, раздел «Подтверждение номера» → «Звонок на "
        "проверочный номер».",
        reply_markup=integrations_zvonok_menu_kb(),
    )
    await callback.answer()


@router.callback_query(F.data == "admin_integrations_apikeys")
async def show_integrations_apikeys_menu(callback: CallbackQuery):
    """Подменю «Внешние ключи и API»."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    from bot.keyboards.admin_settings import integrations_apikeys_menu_kb
    await safe_edit_or_send(callback.message, "🔑 <b>Внешние ключи и API</b>", reply_markup=integrations_apikeys_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "admin_integrations_limits")
async def show_integrations_limits_menu(callback: CallbackQuery):
    """Подменю «Ограничения устройств»."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    from bot.keyboards.admin_settings import integrations_limits_menu_kb
    await safe_edit_or_send(callback.message, "⚙️ <b>Ограничения устройств</b>", reply_markup=integrations_limits_menu_kb())
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
        "<b>Зачем это нужно:</b> этот адрес используется в нескольких местах бота — "
        "открытие WebApp (личный кабинет со ссылкой на подписку и оплатой), публичная "
        "страница магазина (её AI-помощник и посты в канале дают клиентам), страницы "
        "быстрого импорта подписки в Happ/INCY, и ссылка на магазин в автоматическом "
        "подвале постов маркетингового канала. Если домен не задан или неверен — эти "
        "функции будут либо недоступны, либо поведут клиента не туда.\n\n"
        "<b>Что нужно перед тем, как менять:</b>\n"
        "1. Домен куплен и его DNS (A-запись) указывает на IP этого сервера\n"
        "2. На сервере настроен nginx, проксирующий этот домен на бота/WebApp\n"
        "3. Выпущен и подключён SSL-сертификат (например, через <code>certbot</code>) — "
        "без валидного HTTPS клиенты будут видеть ошибку безопасности в браузере\n\n"
        "Если это уже готово — отправьте новый адрес, например:\n<code>https://мой-домен.ru</code>\n\n"
        "(без слэша на конце — если добавите, он всё равно уберётся автоматически)",
        reply_markup=integrations_edit_cancel_kb(),
    )
    await callback.answer()


@router.message(AdminStates.edit_webapp_url)
async def edit_webapp_url_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    value = get_message_text_for_storage(message, "plain").strip()
    if not value.startswith("https://"):
        await safe_edit_or_send(
            message,
            "❌ Адрес обязательно должен начинаться с <code>https://</code> (не http://).\n\n"
            "Telegram категорически не позволяет открывать WebApp по обычному HTTP — "
            "с таким адресом кнопки «Проверить домен» и любые другие WebApp-кнопки в боте "
            "перестанут работать. Убедитесь, что SSL-сертификат уже настроен, и попробуйте ещё раз.",
        )
        return

    try:
        await message.delete()
    except Exception:
        pass

    set_webapp_url(value)
    await state.set_state(AdminStates.integrations_menu)

    check_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Проверить домен", web_app=WebAppInfo(url=value))],
    ])
    await message.answer(
        f"✅ Домен сохранён: <code>{value}</code>\n\n"
        "Нажми кнопку ниже, чтобы сразу открыть личный кабинет и убедиться, что "
        "всё работает — Telegram передаст туда твои реальные данные (подписку, "
        "ключи, баланс), точно так же, как увидит любой другой пользователь бота.\n\n"
        "Если вместо кабинета увидишь ошибку или белый экран — значит на "
        "сервере что-то не так с nginx/SSL, домен ещё не готов.",
        parse_mode="HTML", reply_markup=check_kb,
    )
    await message.answer("Меню интеграций:", reply_markup=integrations_menu_kb())


@router.callback_query(F.data == "admin_edit_panel_cleanup_days")
async def edit_panel_cleanup_days_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    from bot.services.panel_only_cleanup import get_panel_cleanup_delay_days

    await state.set_state(AdminStates.edit_panel_cleanup_days)
    current = get_panel_cleanup_delay_days()
    await safe_edit_or_send(
        callback.message,
        f"🧹 <b>Удаление неактивных ключей с панели</b>\n\nТекущее значение: <code>{current}</code> дней\n\n"
        "Через сколько дней после истечения подписки клиент удаляется именно с "
        "VPN-панели 3x-ui (освобождает место в панели). <b>По умолчанию 0</b> — "
        "удаляется сразу же.\n\n"
        "⚠️ <b>Не путать</b> с полным удалением ключа из самого бота (отдельная "
        "настройка, по умолчанию 30 дней) — даже после удаления с панели клиент "
        "по-прежнему может продлить подписку в боте в обычный срок. После "
        "продления восстановится та же самая ссылка подписки, клиенту ничего "
        "менять не нужно.\n\n"
        "Отправьте число дней (0 — удалять сразу):",
        reply_markup=integrations_edit_cancel_kb(),
    )
    await callback.answer()


@router.message(AdminStates.edit_panel_cleanup_days)
async def edit_panel_cleanup_days_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    value = get_message_text_for_storage(message, "plain").strip()
    try:
        days = int(value)
        if days < 0:
            raise ValueError
    except ValueError:
        await safe_edit_or_send(message, "❌ Введите целое число дней (0 или больше). Попробуйте ещё раз.")
        return

    try:
        await message.delete()
    except Exception:
        pass

    from bot.services.panel_only_cleanup import set_panel_cleanup_delay_days
    set_panel_cleanup_delay_days(days)
    await state.set_state(AdminStates.integrations_menu)

    await message.answer(f"✅ Сохранено: клиент будет удаляться с панели через {days} дней после истечения подписки.")
    await message.answer("Меню интеграций:", reply_markup=integrations_menu_kb())


@router.callback_query(F.data == "admin_edit_happ_provider_id")
async def edit_happ_provider_id_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    from database.requests import get_happ_provider_id

    await state.set_state(AdminStates.edit_happ_provider_id)
    current = get_happ_provider_id()
    current_text = current if current else "не задан"
    await safe_edit_or_send(
        callback.message,
        f"🆔 <b>Happ Provider ID</b>\n\nТекущее значение: <code>{current_text}</code>\n\n"
        "Открывает доступ к «Advanced parameters» у Happ — в частности, к тем "
        "самым уведомлениям об истечении подписки (sub-info/sub-expire), которые "
        "мы отправляем. Без него они официально не гарантированы к работе на "
        "всех платформах.\n\n"
        "Получить: зарегистрируйся на <b>happ-proxy.com</b> — ID будет виден в "
        "правом верхнем углу/профиле.\n\n"
        "Отправь свой Provider ID:",
        reply_markup=integrations_edit_cancel_kb(),
    )
    await callback.answer()


@router.message(AdminStates.edit_happ_provider_id, F.text, ~F.text.startswith('/'))
async def edit_happ_provider_id_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    value = get_message_text_for_storage(message, "plain").strip()
    if not value or value.startswith("/"):
        await safe_edit_or_send(message, "❌ Похоже, это команда, а не значение (например, ты случайно отправил /start). Отправь именно свой Provider ID ещё раз.")
        return

    try:
        await message.delete()
    except Exception:
        pass

    from database.requests import set_happ_provider_id
    set_happ_provider_id(value)
    await state.set_state(AdminStates.integrations_menu)

    await message.answer(f"✅ Happ Provider ID сохранён: <code>{value}</code>", parse_mode="HTML")
    await message.answer("Меню интеграций:", reply_markup=integrations_menu_kb())


@router.callback_query(F.data == "admin_edit_zvonok_public_key")
async def edit_zvonok_public_key_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    from database.requests import get_zvonok_public_key

    await state.set_state(AdminStates.edit_zvonok_public_key)
    current = get_zvonok_public_key()
    current_text = current if current else "не задан"
    await safe_edit_or_send(
        callback.message,
        f"🔑 <b>Zvonok API Public Key</b>\n\nТекущее значение: <code>{current_text}</code>\n\n"
        "Нужен для верификации номера телефона перед выдачей пробного периода "
        "на сайте (защита от повторного получения через сайт после бота).\n\n"
        "Получить: зарегистрируйся на <b>zvonok.com</b> → Настройки профиля → "
        "«API Public Key» → «Сгенерировать».\n\n"
        "Отправь ключ:",
        reply_markup=integrations_edit_cancel_kb(),
    )
    await callback.answer()


@router.message(AdminStates.edit_zvonok_public_key, F.text, ~F.text.startswith('/'))
async def edit_zvonok_public_key_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    value = get_message_text_for_storage(message, "plain").strip()
    if not value or value.startswith("/"):
        await safe_edit_or_send(message, "❌ Похоже, это команда, а не значение (например, ты случайно отправил /start). Отправь именно ключ ещё раз.")
        return

    try:
        await message.delete()
    except Exception:
        pass

    from database.requests import set_zvonok_public_key
    set_zvonok_public_key(value)
    await state.set_state(AdminStates.integrations_menu)

    await message.answer(f"✅ Zvonok API Public Key сохранён: <code>{value}</code>", parse_mode="HTML")
    await message.answer("Меню интеграций:", reply_markup=integrations_menu_kb())


@router.callback_query(F.data == "admin_edit_zvonok_campaign_id")
async def edit_zvonok_campaign_id_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    from database.requests import get_zvonok_campaign_id

    await state.set_state(AdminStates.edit_zvonok_campaign_id)
    current = get_zvonok_campaign_id()
    current_text = current if current else "не задан"
    await safe_edit_or_send(
        callback.message,
        f"📞 <b>Zvonok Campaign ID</b>\n\nТекущее значение: <code>{current_text}</code>\n\n"
        "ID кампании типа «Звонок на проверочный номер» в личном кабинете "
        "zvonok.com (раздел «📱 Подтверждение номера» → создать кампанию с "
        "этим типом → ID виден в адресной строке страницы кампании).\n\n"
        "Отправь ID кампании:",
        reply_markup=integrations_edit_cancel_kb(),
    )
    await callback.answer()


@router.message(AdminStates.edit_zvonok_campaign_id, F.text, ~F.text.startswith('/'))
async def edit_zvonok_campaign_id_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    value = get_message_text_for_storage(message, "plain").strip()
    if not value or value.startswith("/"):
        await safe_edit_or_send(message, "❌ Похоже, это команда, а не значение (например, ты случайно отправил /start). Отправь именно ID кампании ещё раз.")
        return

    try:
        await message.delete()
    except Exception:
        pass

    from database.requests import set_zvonok_campaign_id
    set_zvonok_campaign_id(value)
    await state.set_state(AdminStates.integrations_menu)

    await message.answer(f"✅ Zvonok Campaign ID сохранён: <code>{value}</code>", parse_mode="HTML")
    await message.answer("Меню интеграций:", reply_markup=integrations_menu_kb())



@router.callback_query(F.data == "admin_toggle_start_import_buttons")
async def toggle_start_import_buttons(callback: CallbackQuery, state: FSMContext):
    """Включает/выключает кнопки быстрого импорта (Happ/INCY) на главной странице."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    current = is_start_import_buttons_enabled()
    set_start_import_buttons_enabled(not current)
    await callback.answer("✅ Кнопки включены" if not current else "⚪ Кнопки выключены")
    await show_integrations_menu(callback, state)


@router.callback_query(F.data == "admin_toggle_start_balance_button")
async def toggle_start_balance_button(callback: CallbackQuery, state: FSMContext):
    """Включает/выключает кнопку пополнения баланса на главной странице."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    current = is_start_balance_button_enabled()
    set_start_balance_button_enabled(not current)
    await callback.answer("✅ Кнопка включена" if not current else "⚪ Кнопка выключена")
    await show_integrations_menu(callback, state)


@router.callback_query(F.data == "admin_toggle_welcome_page")
async def toggle_welcome_page(callback: CallbackQuery, state: FSMContext):
    """Включает/выключает публичную страницу-витрину /welcome для новых
    посетителей (описание сервиса + тарифы, без входа в бота)."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    current = is_welcome_page_enabled()
    set_welcome_page_enabled(not current)
    webapp_url = get_effective_webapp_url()
    if not current and webapp_url:
        await callback.answer(f"✅ Витрина включена: {webapp_url}/welcome", show_alert=True)
    else:
        await callback.answer("✅ Витрина включена" if not current else "⚪ Витрина выключена (адрес теперь отдаёт 404)")
    await show_integrations_menu(callback, state)


@router.callback_query(F.data == "admin_welcome_template_menu")
async def show_welcome_template_menu(callback: CallbackQuery, state: FSMContext):
    """Показывает список доступных шаблонов страницы /welcome с описанием."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    current_id = get_welcome_template_id()
    lines = ["🎨 <b>Шаблон витрины /welcome</b>\n", "Выберите дизайн — применится сразу, без обновления бота:\n"]
    for tid, info in WELCOME_TEMPLATES.items():
        mark = "✅ " if tid == current_id else ""
        lines.append(f"{mark}<b>{info['label']}</b>\n{info['description']}")

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    builder = InlineKeyboardBuilder()
    for tid, info in WELCOME_TEMPLATES.items():
        mark = "✅ " if tid == current_id else ""
        builder.row(InlineKeyboardButton(text=f"{mark}{info['label']}", callback_data=f"admin_set_welcome_template:{tid}"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_integrations_site"))

    await safe_edit_or_send(callback.message, "\n\n".join(lines), reply_markup=builder.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("admin_set_welcome_template:"))
async def set_welcome_template_handler(callback: CallbackQuery, state: FSMContext):
    """Сохраняет выбранный шаблон и возвращает в это же подменю (обновлённое)."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    template_id = callback.data.split(":", 1)[1]
    try:
        set_welcome_template_id(template_id)
        await callback.answer(f"✅ Применён шаблон: {WELCOME_TEMPLATES[template_id]['label']}")
    except ValueError:
        await callback.answer("❌ Неизвестный шаблон", show_alert=True)
        return

    await show_welcome_template_menu(callback, state)


@router.callback_query(F.data == "admin_cabinet_theme_menu")
async def show_cabinet_theme_menu(callback: CallbackQuery, state: FSMContext):
    """Показывает список доступных тем личного кабинета (index.html)."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    current_id = get_cabinet_theme_id()
    lines = ["🎭 <b>Тема личного кабинета</b>\n", "Применяется сразу, без обновления бота:\n"]
    for tid, info in CABINET_THEMES.items():
        mark = "✅ " if tid == current_id else ""
        lines.append(f"{mark}<b>{info['label']}</b>\n{info['description']}")

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    builder = InlineKeyboardBuilder()
    for tid, info in CABINET_THEMES.items():
        mark = "✅ " if tid == current_id else ""
        builder.row(InlineKeyboardButton(text=f"{mark}{info['label']}", callback_data=f"admin_set_cabinet_theme:{tid}"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_integrations_site"))

    await safe_edit_or_send(callback.message, "\n\n".join(lines), reply_markup=builder.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("admin_set_cabinet_theme:"))
async def set_cabinet_theme_handler(callback: CallbackQuery, state: FSMContext):
    """Сохраняет выбранную тему кабинета и возвращает в это же подменю."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    theme_id = callback.data.split(":", 1)[1]
    try:
        set_cabinet_theme_id(theme_id)
        await callback.answer(f"✅ Применена тема: {CABINET_THEMES[theme_id]['label']}")
    except ValueError:
        await callback.answer("❌ Неизвестная тема", show_alert=True)
        return

    await show_cabinet_theme_menu(callback, state)


@router.callback_query(F.data == "admin_device_limit_type_menu")
async def show_device_limit_type_menu(callback: CallbackQuery, state: FSMContext):
    """Показывает выбор типа ограничения количества устройств на ключ."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    from database.requests import DEVICE_LIMIT_TYPES, get_device_limit_type

    current_id = get_device_limit_type()
    lines = ["📱 <b>Ограничение устройств на ключ</b>\n", "Общая настройка для всех тарифов и ключей:\n"]
    for tid, info in DEVICE_LIMIT_TYPES.items():
        mark = "✅ " if tid == current_id else ""
        lines.append(f"{mark}<b>{info['label']}</b>\n{info['description']}")

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    builder = InlineKeyboardBuilder()
    for tid, info in DEVICE_LIMIT_TYPES.items():
        mark = "✅ " if tid == current_id else ""
        builder.row(InlineKeyboardButton(text=f"{mark}{info['label']}", callback_data=f"admin_set_device_limit_type:{tid}"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_integrations_limits"))

    await safe_edit_or_send(callback.message, "\n\n".join(lines), reply_markup=builder.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("admin_set_device_limit_type:"))
async def set_device_limit_type_handler(callback: CallbackQuery, state: FSMContext):
    """Сохраняет выбранный тип ограничения устройств."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    from database.requests import DEVICE_LIMIT_TYPES, set_device_limit_type

    type_id = callback.data.split(":", 1)[1]
    try:
        set_device_limit_type(type_id)
        await callback.answer(f"✅ Применено: {DEVICE_LIMIT_TYPES[type_id]['label']}")
    except ValueError:
        await callback.answer("❌ Неизвестный тип", show_alert=True)
        return

    await show_device_limit_type_menu(callback, state)


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

@router.callback_query(F.data == "admin_noop")
async def admin_noop_handler(callback: CallbackQuery):
    """Кнопка-подпись без действия — просто гасит спиннер загрузки."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data.startswith("admin_toggle_auth_method:"))
async def toggle_auth_method(callback: CallbackQuery):
    """Включает/выключает конкретный способ входа на сайте (OAuth или
    вход по коду из бота) — независимо от того, настроены ли для него
    учётные данные."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    from database.requests import (
        SITE_AUTH_METHODS, is_site_auth_method_enabled, set_site_auth_method_enabled,
    )

    method = callback.data.split(":", 1)[1]
    if method not in SITE_AUTH_METHODS:
        await callback.answer("Неизвестный способ входа", show_alert=True)
        return

    new_value = not is_site_auth_method_enabled(method)
    set_site_auth_method_enabled(method, new_value)

    label = SITE_AUTH_METHODS[method]
    await callback.answer(f"{'✅ Включено' if new_value else '❌ Выключено'}: {label}")
    await safe_edit_or_send(callback.message, "🌐 Меню интеграций:", reply_markup=integrations_menu_kb())


@router.callback_query(F.data == "admin_toggle_happ_autoconnect")
async def toggle_happ_autoconnect(callback: CallbackQuery):
    """Включает/выключает автоподключение к самому быстрому серверу в
    Happ/INCY (subscription-autoconnect-type=lowestdelay) — приложение
    само измеряет отклик каждого сервера и подключается к лучшему при
    запуске. Требует настроенный Happ Provider ID."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    from database.requests import get_happ_provider_id, is_happ_autoconnect_enabled, set_happ_autoconnect_enabled

    new_value = not is_happ_autoconnect_enabled()
    set_happ_autoconnect_enabled(new_value)

    warning = ""
    if new_value and not get_happ_provider_id():
        warning = " ⚠️ Задай ещё Happ Provider ID выше — без него это официально не гарантированно работает."
    await callback.answer(f"{'✅ Включено' if new_value else '❌ Выключено'}: автовыбор быстрого сервера.{warning}", show_alert=bool(warning))

    from bot.keyboards.admin_settings import integrations_apikeys_menu_kb
    await safe_edit_or_send(callback.message, "🔑 <b>Внешние ключи и API</b>", reply_markup=integrations_apikeys_menu_kb())


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
