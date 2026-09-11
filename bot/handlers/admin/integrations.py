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


@router.callback_query(F.data == "admin_zvonok_postback_info")
async def show_zvonok_postback_info(callback: CallbackQuery):
    """Показывает готовые к вставке ссылки постбека с РЕАЛЬНЫМ доменом
    этой инсталляции — админу остаётся только скопировать (тап по коду
    в Telegram копирует его целиком) и вставить в личном кабинете
    zvonok.com."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    from database.requests import get_effective_webapp_url
    webapp_url = (get_effective_webapp_url() or "").rstrip("/")

    if not webapp_url:
        text = (
            "📡 <b>Постбек (мгновенное подтверждение)</b>\n\n"
            "⚠️ Сначала задай домен сайта (Интеграции → 🌐 Сайт и витрина → "
            "Домен сайта) — без него нельзя построить рабочие ссылки."
        )
    else:
        success_url = f"{webapp_url}/api/public/zvonok/postback?call_id={{ct_call_id}}&amp;result=ok"
        no_answer_url = f"{webapp_url}/api/public/zvonok/postback?call_id={{ct_call_id}}&amp;result=no_answer"
        text = (
            "📡 <b>Постбек (мгновенное подтверждение)</b>\n\n"
            "Без постбека подтверждение звонка приходит с задержкой (мы "
            "сами периодически спрашиваем у Zvonok — успел ли клиент "
            "позвонить). С постбеком Zvonok сам мгновенно сообщает нам "
            "результат сразу после звонка — клиент почти не ждёт.\n\n"
            "<b>Как включить:</b> личный кабинет zvonok.com → "
            "«Подтверждение номера» → выбери свою кампанию → «Постбеки» "
            "→ вставь ссылки ниже в соответствующие поля (тапни, чтобы "
            "скопировать):\n\n"
            "<b>Успешный дозвон:</b>\n"
            f"<code>{success_url}</code>\n\n"
            "<b>Нет ответа на звонок:</b>\n"
            f"<code>{no_answer_url}</code>\n\n"
            "Метод запроса — GET (уже стоит по умолчанию у Zvonok). "
            "Если не настроить постбек — всё продолжит работать как "
            "раньше, просто чуть медленнее (через периодический опрос)."
        )

    from bot.keyboards.admin_settings import integrations_zvonok_menu_kb
    await safe_edit_or_send(callback.message, text, reply_markup=integrations_zvonok_menu_kb())
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


@router.callback_query(F.data == "admin_integrations_happ")
async def show_integrations_happ_menu(callback: CallbackQuery):
    """Подменю «Happ»."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    from bot.keyboards.admin_settings import integrations_client_app_menu_kb
    await safe_edit_or_send(
        callback.message,
        "📱 <b>Happ</b>\n\n"
        "Provider ID открывает доступ к расширенным параметрам приложения "
        "(в т.ч. автовыбор сервера и уведомления об истечении подписки). "
        "Общий с INCY, но переключатели ниже — только для Happ (INCY "
        "настраивается отдельно, у него свой раздел).",
        reply_markup=integrations_client_app_menu_kb('happ'),
    )
    await callback.answer()


@router.callback_query(F.data == "admin_integrations_incy")
async def show_integrations_incy_menu(callback: CallbackQuery):
    """Подменю «INCY»."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return
    from bot.keyboards.admin_settings import integrations_client_app_menu_kb
    await safe_edit_or_send(
        callback.message,
        "⚡ <b>INCY</b>\n\n"
        "Provider ID открывает доступ к расширенным параметрам приложения "
        "(в т.ч. автовыбор сервера и уведомления об истечении подписки). "
        "Общий с Happ, но переключатели ниже — только для INCY (Happ "
        "настраивается отдельно, у него свой раздел).",
        reply_markup=integrations_client_app_menu_kb('incy'),
    )
    await callback.answer()


_CLIENT_SETTING_INFO = {
    'happ': {
        'autoconnect': (
            "⚡ Автовыбор быстрого сервера\n\n"
            "Приложение само измеряет отклик (пинг) каждого сервера в подписке "
            "и подключается к самому быстрому при запуске — не нужно выбирать "
            "сервер вручную."
        ),
        'sort_ping': (
            "📊 Автосортировка по пингу\n\n"
            "Сервера в списке подписки показываются в порядке от самого "
            "быстрого к самому медленному (недоступные — в конце), вместо "
            "порядка, в котором они пришли с панели."
        ),
        'notify_expire': (
            "🔔 Родные уведомления об истечении\n\n"
            "Само приложение (не бот) присылает системное push-уведомление "
            "за 3 дня до окончания подписки — по одному в день. Дополняет "
            "наш собственный баннер внутри приложения, не заменяет его."
        ),
        'auto_update': (
            "🔄 Глобальное автообновление подписок\n\n"
            "Приложение само обновляет ВСЕ подписки при запуске — не только "
            "нашу, а любые другие, добавленные в это же приложение."
        ),
        'hide_settings': (
            "🔒 Скрыть настройки серверов\n\n"
            "Клиент не сможет просматривать, редактировать или передать "
            "другим людям конфигурации серверов внутри приложения — полезно, "
            "если важно, чтобы подписку не могли легко скопировать/перепродать."
        ),
    },
    'incy': {
        'sort_ping': (
            "📊 Автосортировка по пингу\n\n"
            "Сервера в списке подписки показываются в порядке от самого "
            "быстрого к самому медленному, вместо порядка, в котором они "
            "пришли с панели."
        ),
        'hide_url': (
            "🔗 Скрыть ссылку подписки\n\n"
            "Клиент не сможет скопировать, поделиться или увидеть в QR-коде "
            "саму ссылку на подписку. ВАЖНО: это НЕ то же самое, что "
            "«скрыть настройки серверов» у Happ — сами конфиги серверов "
            "внутри приложения при этом остаются видимыми и редактируемыми, "
            "скрывается только исходная ссылка."
        ),
    },
}


@router.callback_query(F.data.startswith("admin_happ_info:"))
async def show_happ_setting_info(callback: CallbackQuery):
    """Показывает всплывающее пояснение при тапе на название настройки
    — в Telegram нет наведения мышкой, поэтому это ближайший аналог
    подсказки. Пояснения РАЗНЫЕ для Happ и INCY, т.к. у них не всегда
    одинаковое реальное поведение."""
    _, app, key = callback.data.split(":", 2)
    text = _CLIENT_SETTING_INFO.get(app, {}).get(key, "Пояснение недоступно.")
    await callback.answer(text, show_alert=True)


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


@router.callback_query(F.data == "admin_edit_logo")
async def edit_logo_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    await state.set_state(AdminStates.edit_logo)
    await safe_edit_or_send(
        callback.message,
        "🖼 <b>Логотип</b>\n\n"
        "Пришли одним сообщением фото (не файлом-документом) — оно "
        "сразу заменит:\n"
        "• логотип на сайте (страница /shop)\n"
        "• картинку в приветственном сообщении бота (/start)\n\n"
        "Рекомендуется квадратное изображение, например 512×512.",
        reply_markup=integrations_edit_cancel_kb('admin_integrations_site'),
    )
    await callback.answer()


@router.message(AdminStates.edit_logo, F.photo)
async def edit_logo_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    photo = message.photo[-1]  # самое большое доступное разрешение
    file = await message.bot.get_file(photo.file_id)
    if file.file_size and file.file_size > 8 * 1024 * 1024:
        await safe_edit_or_send(message, "❌ Файл слишком большой (максимум 8 МБ). Пришли изображение поменьше.")
        return

    try:
        import os
        file_io = await message.bot.download_file(file.file_path)
        static_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "webapp", "static")
        logo_path = os.path.join(static_dir, "logo.png")
        os.makedirs(static_dir, exist_ok=True)
        with open(logo_path, "wb") as f:
            f.write(file_io.read())
    except Exception as e:
        logger.error(f"Не удалось сохранить загруженный логотип: {e}")
        await safe_edit_or_send(message, "❌ Не удалось сохранить файл логотипа на сервере. Попробуй ещё раз.")
        return

    from database.db_pages import update_page_custom
    update_page_custom('main', image=photo.file_id, media_type='photo')

    from bot.keyboards.admin_settings import integrations_site_menu_kb
    await state.clear()
    await safe_edit_or_send(
        message,
        "✅ Логотип обновлён — и на сайте, и в приветственном сообщении бота.\n\n"
        "Нажми /start, чтобы сразу увидеть новую картинку в боте.",
        reply_markup=integrations_site_menu_kb(),
    )


@router.message(AdminStates.edit_logo)
async def edit_logo_wrong_type(message: Message):
    """Пользователь прислал не фото (текст, документ и т.п.)."""
    if not is_admin(message.from_user.id):
        return
    if (message.text or '').strip() == '❌ Отмена':
        return  # обработается отдельным хендлером отмены, если он есть
    await safe_edit_or_send(message, "Пришли именно фото (не документом) — или нажми «Отмена» ниже, чтобы выйти без изменений.")


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
    lines = [
        "📱 <b>Ограничение устройств на ключ</b>\n",
        "Общая настройка для всех тарифов и ключей — выбирает СПОСОБ подсчёта устройств:\n",
    ]
    for tid, info in DEVICE_LIMIT_TYPES.items():
        mark = "✅ " if tid == current_id else ""
        lines.append(f"{mark}<b>{info['label']}</b>\n{info['description']}")
    lines.append(
        "\n💡 Само ЧИСЛО разрешённых устройств настраивается не здесь, а "
        "отдельно у каждого тарифа: ⚙️ Настройки → 💳 Тарифы → выбери тариф "
        "→ пункт «Устройств»."
    )

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


@router.callback_query(F.data.startswith("admin_toggle_client_setting:"))
async def toggle_client_setting(callback: CallbackQuery):
    """Включает/выключает конкретную настройку для КОНКРЕТНОГО
    приложения (Happ или INCY по отдельности) — обнаружено на практике,
    что эти два приложения по-разному трактуют одни и те же 'Advanced
    parameter' заголовки, поэтому настраиваются раздельно."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    from database.requests import (
        get_happ_provider_id, is_client_toggle_enabled, set_client_toggle_enabled,
        CLIENT_APPS, CLIENT_TOGGLE_LABELS_BY_APP,
    )

    _, app, toggle = callback.data.split(":", 2)
    if app not in CLIENT_APPS or toggle not in CLIENT_TOGGLE_LABELS_BY_APP.get(app, {}):
        await callback.answer("Неизвестная настройка", show_alert=True)
        return

    new_value = not is_client_toggle_enabled(app, toggle)
    set_client_toggle_enabled(app, toggle, new_value)

    warning = ""
    if app == "happ" and new_value and not get_happ_provider_id():
        warning = " ⚠️ Задай ещё Provider ID выше — без него это официально не гарантированно работает."
    label = CLIENT_TOGGLE_LABELS_BY_APP[app][toggle]
    app_label = CLIENT_APPS[app]
    await callback.answer(f"{'✅ Включено' if new_value else '❌ Выключено'} для {app_label}: {label}.{warning}", show_alert=bool(warning))

    from bot.keyboards.admin_settings import integrations_client_app_menu_kb
    await safe_edit_or_send(callback.message, f"{'📱' if app == 'happ' else '⚡'} <b>{app_label}</b>", reply_markup=integrations_client_app_menu_kb(app))


@router.callback_query(F.data == "admin_edit_incy_update_interval")
async def edit_incy_update_interval_start(callback: CallbackQuery, state: FSMContext):
    """Запрашивает интервал автообновления подписки в INCY — в отличие
    от Happ, это ЧИСЛО часов (profile-update-interval), а не простой
    переключатель."""
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return

    from database.requests import get_incy_update_interval_hours
    await state.set_state(AdminStates.edit_incy_update_interval)
    current = get_incy_update_interval_hours()
    current_text = f"{current} ч." if current else "не задан"
    await safe_edit_or_send(
        callback.message,
        f"⏱ <b>Интервал автообновления подписки в INCY</b>\n\nТекущее значение: {current_text}\n\n"
        "Пришли число — сколько часов между автоматическими обновлениями подписки в приложении "
        "(целое число, кратное 1 часу). Пришли <code>0</code>, чтобы сбросить и не отправлять "
        "этот параметр вообще (тогда приложение использует своё поведение по умолчанию).",
        reply_markup=integrations_edit_cancel_kb('admin_integrations_incy'),
    )
    await callback.answer()


@router.message(AdminStates.edit_incy_update_interval, F.text)
async def edit_incy_update_interval_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    text = (message.text or '').strip()
    if not text.isdigit():
        await safe_edit_or_send(message, "❌ Пришли целое число (часы), например <code>6</code>, или <code>0</code> для сброса.")
        return

    hours = int(text)
    from database.requests import set_incy_update_interval_hours
    set_incy_update_interval_hours(hours if hours > 0 else None)

    await state.clear()
    from bot.keyboards.admin_settings import integrations_client_app_menu_kb
    status = f"{hours} ч." if hours > 0 else "сброшен (используется поведение приложения по умолчанию)"
    await safe_edit_or_send(
        message,
        f"✅ Интервал автообновления INCY: {status}",
        reply_markup=integrations_client_app_menu_kb('incy'),
    )


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
