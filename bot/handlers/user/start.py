import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramForbiddenError
from config import ADMIN_IDS
from database.requests import get_or_create_user, is_user_banned, get_setting, is_referral_enabled, get_user_by_referral_code, set_user_referrer
from bot.states.user_states import StartStates
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot.handlers.user.payments.base import PAYMENT_DEEPLINK_PREFIX
from bot.utils.page_flow import (
    build_page_flow_context,
    parse_registry_names,
    run_page_guards,
    run_page_hooks,
)
from bot.utils.text import escape_html, safe_edit_or_send
from bot.utils.user_pages import render_access_blocked_page
from bot.handlers.user.support import _start_support_dialog

logger = logging.getLogger(__name__)

router = Router()


def _build_tariff_text() -> str:
    """Generates a block of tariffs for the tariff list placeholder.
    
    Returns:
        HTML text with a list of tariffs and prices, or an empty line if there are no tariffs
    """
    from bot.utils.page_dynamic_data import build_tariff_text

    return build_tariff_text()


SHOW_ID_PAGE_KEY = 'show_id'


async def _render_show_id_page(target, force_new: bool = False):
    """Renders the Telegram ID display page via pages."""
    from bot.utils.page_renderer import render_page

    await render_page(target, page_key=SHOW_ID_PAGE_KEY, force_new=force_new)


async def _show_start_payment_status(
    message: Message,
    *,
    title_html: str,
    body_html: str | None = None,
    body_text: str | None = None,
    reply_markup=None,
) -> None:
    """Shows page-backed payment processing status /start."""
    from bot.handlers.user.payments.status_page import show_payment_status_message

    await show_payment_status_message(
        message,
        title_html=title_html,
        body_html=body_html,
        body_text=body_text,
        payment_provider_title='Crypto',
        reply_markup=reply_markup,
        force_new=True,
    )


async def _show_main_page_guard_denied(target, message: str, *, show_alert: bool) -> None:
    if isinstance(target, CallbackQuery):
        await target.answer(message, show_alert=show_alert)
        return

    await safe_edit_or_send(target, message, force_new=True)


def _merge_main_append_buttons(
    hook_append_buttons: list[list[InlineKeyboardButton]] | None,
    admin_append_buttons: list[list[InlineKeyboardButton]] | None,
) -> list[list[InlineKeyboardButton]] | None:
    append_buttons = []
    if hook_append_buttons:
        append_buttons.extend(hook_append_buttons)
    if admin_append_buttons:
        append_buttons.extend(admin_append_buttons)
    return append_buttons or None


async def _render_main_page(target, force_new: bool = False) -> bool:
    """Renders the main page via render_page.
    
    Args:
        target: Message or CallbackQuery
        force_new: Force a new message to be sent
    """
    from bot.utils.page_renderer import render_page
    from database.requests import (
        get_page, is_trial_enabled, get_trial_tariff_id, has_used_trial,
        get_trial_mode, get_groups_with_trial, get_user_internal_id, get_eligible_trial_group_ids,
    )

    # Determining telegram_id
    if isinstance(target, CallbackQuery):
        user_id = target.from_user.id
    else:
        user_id = target.from_user.id if hasattr(target, 'from_user') and target.from_user else 0

    is_admin = user_id in ADMIN_IDS

    # Generating tariff text
    tariff_text = _build_tariff_text()

    # Dynamic visibility of buttons
    if not is_trial_enabled():
        show_trial = False
    elif get_trial_mode() == 'per_group':
        # В режиме "по группам" кнопка видна, если есть хотя бы одна группа
        # с настроенным пробником, который этот пользователь ещё не брал.
        groups_with_trial = get_groups_with_trial()
        if not groups_with_trial:
            show_trial = False
        else:
            internal_id = get_user_internal_id(user_id)
            if not internal_id:
                show_trial = False
            else:
                eligible = get_eligible_trial_group_ids(internal_id, [g['id'] for g in groups_with_trial])
                show_trial = bool(eligible)
    else:
        show_trial = get_trial_tariff_id() is not None and (not has_used_trial(user_id))
    show_referral = is_referral_enabled()

    from database.requests import is_start_import_buttons_enabled, is_start_balance_button_enabled
    show_start_import_buttons = is_start_import_buttons_enabled()
    show_balance_button = is_start_balance_button_enabled()

    visibility = {
        'btn_trial': show_trial,
        'btn_referral': show_referral,
        'btn_start_import_happ': show_start_import_buttons,
        'btn_start_import_incy': show_start_import_buttons,
        'btn_balance_topup': show_balance_button,
    }

    # Substitution text
    text_replacements = {
        '%tariffs%': tariff_text,
        '%no_tariffs%': '',
        '%тарифы%': tariff_text,
        '%без_тарифов%': '',
    }

    # Admin Panel button for administrators
    admin_append_buttons = None
    if is_admin:
        admin_append_buttons = [
            [InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin_panel")]
        ]

    context = build_page_flow_context(target, telegram_id=user_id, page_key='main')
    prepend_buttons = None
    append_buttons = admin_append_buttons

    page = get_page('main')
    if page:
        guard_result = await run_page_guards(
            parse_registry_names(page.get('guard_names')),
            target,
            context,
        )
        if not guard_result.allowed:
            await _show_main_page_guard_denied(
                target,
                guard_result.message or "⚠️ Страница недоступна",
                show_alert=guard_result.show_alert,
            )
            return False

        hook_result = await run_page_hooks(
            parse_registry_names(page.get('hook_names')),
            target,
            context,
        )
        context.update(hook_result.context)
        visibility.update(hook_result.visibility)
        text_replacements.update(hook_result.text_replacements)
        prepend_buttons = hook_result.prepend_buttons
        append_buttons = _merge_main_append_buttons(hook_result.append_buttons, admin_append_buttons)

    # Кнопка новостного канала с динамическим счётчиком непрочитанных —
    # добавляется отдельной строкой сразу после AI-помощника (первой в
    # append_buttons), не как статичная кнопка со страницы. Пост
    # засчитывается "увиденным" в момент показа счётчика здесь, так как
    # Telegram не уведомляет бота о клике по внешней url-кнопке.
    #
    # Канал берётся из marketing_channel_id (та же настройка, что и в
    # разделе «📰 Группа» в админке) — у каждой инсталляции бота свой
    # канал, поэтому здесь нельзя хардкодить конкретный @username.
    # Кнопка просто не показывается, пока админ не настроит свой канал.
    try:
        from database.requests import (
            count_unread_channel_posts, mark_channel_posts_seen,
            get_max_sent_post_id, get_marketing_channel_id,
        )
        channel_id = get_marketing_channel_id()
        if channel_id:
            channel_username = channel_id.lstrip('@')
            unread_count = count_unread_channel_posts(user_id)
            news_label = f'📰 Новости ({unread_count})' if unread_count > 0 else '📰 Новости'
            news_button_row = [InlineKeyboardButton(text=news_label, url=f'https://t.me/{channel_username}')]
            append_buttons = [news_button_row] + (append_buttons or [])
            if unread_count > 0:
                mark_channel_posts_seen(user_id, get_max_sent_post_id())
    except Exception as e:
        logger.warning(f"Не удалось построить кнопку новостей: {e}")

    await render_page(
        target,
        page_key='main',
        context=context,
        visibility=visibility,
        text_replacements=text_replacements,
        prepend_buttons=prepend_buttons,
        append_buttons=append_buttons,
        force_new=force_new,
    )
    return True


@router.message(Command('start'), StateFilter('*'))
async def cmd_start(message: Message, state: FSMContext, command: CommandObject):
    """/start command handler."""
    user_id = message.from_user.id
    username = message.from_user.username
    logger.info(f'CMD_START: User {user_id} started bot')

    (user, is_new) = get_or_create_user(
        user_id,
        username,
        first_name=getattr(message.from_user, 'first_name', None),
        last_name=getattr(message.from_user, 'last_name', None),
    )
    if user.get('is_banned'):
        await render_access_blocked_page(message, force_new=True)
        return

    args = command.args

    if is_new:
        from bot.keyboards.user import language_choice_kb

        await state.set_state(StartStates.choosing_language)
        await state.update_data(pending_start_args=args)
        await message.answer(
            "🌍 Choose your language / Выберите язык:",
            reply_markup=language_choice_kb(),
        )
        return

    if not (args and args.startswith(PAYMENT_DEEPLINK_PREFIX)):
        if not await _check_channel_gate(message, user_id, state, args):
            return

    await _continue_start(message, state, user, args, is_new, user_id, username)


@router.callback_query(F.data.startswith("set_lang:"), StartStates.choosing_language)
async def on_language_chosen(callback: CallbackQuery, state: FSMContext):
    """Сохраняет выбранный язык нового пользователя и продолжает /start
    с теми параметрами (deep-link args), с которыми он изначально пришёл."""
    from database.requests import set_user_language

    lang = callback.data.split(":", 1)[1]
    telegram_id = callback.from_user.id
    username = callback.from_user.username

    set_user_language(telegram_id, lang)

    data = await state.get_data()
    args = data.get('pending_start_args')
    await state.clear()

    (user, _) = get_or_create_user(
        telegram_id,
        username,
        first_name=getattr(callback.from_user, 'first_name', None),
        last_name=getattr(callback.from_user, 'last_name', None),
    )

    try:
        await callback.message.delete()
    except Exception:
        pass

    if not (args and args.startswith(PAYMENT_DEEPLINK_PREFIX)):
        if not await _check_channel_gate(callback.message, telegram_id, state, args):
            await callback.answer()
            return

    await _continue_start(callback, state, user, args, True, telegram_id, username)
    await callback.answer()


async def _check_channel_gate(send_target, telegram_id: int, state: FSMContext, args: str | None) -> bool:
    """Проверяет обязательную подписку на канал, если она включена.

    Возвращает True, если пользователь может продолжить (гейт выключен,
    канал не настроен, проверка технически не удалась — fail-open чтобы не
    заблокировать всех пользователей из-за сбоя API, или подписка есть).
    Возвращает False и показывает экран "подпишитесь", если подписки нет —
    вызывающий код должен прекратить дальнейшую обработку /start. args
    сохраняется в FSM, чтобы после успешной подписки продолжить с тем же
    deep-link'ом, откуда пользователь начинал.
    """
    from database.requests import is_channel_gate_enabled, get_gate_channel_id

    if not is_channel_gate_enabled():
        return True

    channel_id = get_gate_channel_id()
    if not channel_id:
        return True

    from bot.services.telegram_membership import check_telegram_chat_member
    result = await check_telegram_chat_member(
        send_target.bot, chat_id=channel_id, telegram_id=telegram_id,
    )

    if not result['ok']:
        logger.warning(
            f"Проверка подписки на канал не удалась (fail-open, пропускаем): {result.get('reason')}"
        )
        return True

    if result['is_member']:
        return True

    await state.update_data(pending_gate_args=args)

    builder = InlineKeyboardBuilder()
    if channel_id.startswith('@'):
        builder.row(InlineKeyboardButton(
            text='📢 Перейти в канал',
            url=f'https://t.me/{channel_id.lstrip("@")}',
        ))
    builder.row(InlineKeyboardButton(text='✅ Я подписался', callback_data='recheck_channel_gate'))

    await send_target.answer(
        '🔒 <b>Требуется подписка</b>\n\n'
        'Чтобы пользоваться ботом, подпишитесь на наш канал и нажмите «Я подписался».',
        parse_mode='HTML',
        reply_markup=builder.as_markup(),
    )
    return False


@router.callback_query(F.data == 'recheck_channel_gate')
async def recheck_channel_gate(callback: CallbackQuery, state: FSMContext):
    """Повторная проверка подписки после нажатия «Я подписался»."""
    telegram_id = callback.from_user.id
    username = callback.from_user.username

    data = await state.get_data()
    args = data.get('pending_gate_args')

    if not await _check_channel_gate(callback.message, telegram_id, state, args):
        await callback.answer('⚠️ Подписка пока не найдена, попробуйте ещё раз через пару секунд', show_alert=True)
        return

    from database.requests import get_or_create_user
    (user, _) = get_or_create_user(
        telegram_id, username,
        first_name=getattr(callback.from_user, 'first_name', None),
        last_name=getattr(callback.from_user, 'last_name', None),
    )
    await state.clear()

    try:
        await callback.message.delete()
    except Exception:
        pass

    await _continue_start(callback, state, user, args, False, telegram_id, username)
    await callback.answer()


async def _continue_start(
    render_target: Message | CallbackQuery,
    state: FSMContext,
    user: dict,
    args: str | None,
    is_new: bool,
    telegram_id: int,
    username: str | None,
):
    """Продолжение /start после определения языка (для новых пользователей)
    или сразу (для существующих). render_target передаётся напрямую только
    в функции, явно поддерживающие Message|CallbackQuery; для остального
    используется send_target — гарантированно настоящий Message."""
    send_target = render_target.message if isinstance(render_target, CallbackQuery) else render_target

    if args:
        try:
            from bot.handlers.user.payments.base import handle_payment_deeplink
            if await handle_payment_deeplink(
                send_target, state, args,
                user_internal_id=user['id'],
                telegram_id=telegram_id,
            ):
                return
        except Exception as e:
            logger.exception(f'Ошибка обработки платёжного deep-link: {e}')
            await _show_start_payment_status(
                send_target,
                title_html='❌ <b>Ошибка проверки платежа</b>',
                body_text='Произошла ошибка при проверке платежа.',
            )
            return

    await state.clear()

    if args and args.startswith('pr_'):
        from bot.handlers.user.promo import render_promo_status_page
        from bot.services.promotions import activate_promo_code_for_user
        from database.requests import record_promo_link_visit

        code = args[3:].strip()
        promo_result = activate_promo_code_for_user(user['id'], code, allow_coupons=False)
        if promo_result['ok']:
            promo = promo_result['promo']
            record_promo_link_visit(
                promo_code_id=promo['id'],
                code=promo['code'],
                user_id=user['id'],
                telegram_id=telegram_id,
                start_param=args,
            )
            await render_promo_status_page(
                send_target,
                title_html="🎟 <b>Промокод сохранён</b>",
                body_html=(
                    f"Код <b>{escape_html(promo['code'])}</b> "
                    "будет учтён при следующей оплате."
                ),
                force_new=True,
            )
        else:
            await render_promo_status_page(
                send_target,
                title_html="⚠️ <b>Промо-ссылка недоступна</b>",
                body_text=promo_result['message'],
                force_new=True,
            )

    if args and args.startswith('claim_'):
        from bot.services.anonymous_purchase import claim_anonymous_purchase
        from bot.keyboards.admin import home_only_kb

        code = args[6:].strip()
        claim_result = await claim_anonymous_purchase(
            code, telegram_id, username=username,
        )
        if claim_result['ok']:
            await send_target.answer(
                f"✅ <b>Ключ привязан к вашему аккаунту!</b>\n\n{escape_html(claim_result['message'])}\n\n"
                "Откройте раздел «Мои ключи», чтобы увидеть его.",
                parse_mode='HTML',
                reply_markup=home_only_kb(),
            )
        else:
            await send_target.answer(
                f"⚠️ {escape_html(claim_result['message'])}",
                parse_mode='HTML',
                reply_markup=home_only_kb(),
            )
        return

    if is_new and args and args.startswith('ref_'):
        ref_code = args[4:]
        referrer = get_user_by_referral_code(ref_code)
        if referrer and referrer['id'] != user['id']:
            if set_user_referrer(user['id'], referrer['id']):
                logger.info(f"User {telegram_id} привязан к рефереру {referrer['telegram_id']}")
                try:
                    from bot.services.notifications import notify_referrers_new_referral
                    await notify_referrers_new_referral(render_target.bot, user['id'])
                except Exception as notify_err:
                    logger.warning(f'Ошибка уведомления о новом реферале: {notify_err}')

    
    # Обработка deep-link параметров для покупки и замены ключей
    if args == 'buy':
        from bot.handlers.user.tariffs import _render_buy_page
        try:
            await _render_buy_page(render_target)
        except Exception as e:
            logger.exception(f'Ошибка открытия страницы покупки: {e}')
        return

    if args == 'trial':
        from database.requests import is_trial_enabled, get_trial_tariff_id, has_used_trial
        from bot.utils.page_renderer import render_page
        try:
            if not is_trial_enabled():
                await send_target.answer('❌ Пробная подписка сейчас недоступна')
            elif get_trial_tariff_id() is None:
                await send_target.answer('❌ Пробный тариф не настроен')
            elif has_used_trial(telegram_id):
                await send_target.answer('ℹ️ Вы уже использовали пробный период')
            else:
                await render_page(send_target, page_key='trial')
        except Exception as e:
            logger.exception(f'Ошибка открытия страницы триала: {e}')
        return
    

    if args == 'support':
        try:
            await _start_support_dialog(render_target, state)
        except Exception as e:
            logger.exception(f'Ошибка открытия поддержки: {e}')
        return

    if args == 'ai_support':
        # Локальный импорт — избегаем циклической зависимости с ai_handler.py
        # (тот при обратном переходе в меню тоже импортирует start.py локально).
        from bot.handlers.user.ai_handler import AIChatStates
        try:
            await state.set_state(AIChatStates.waiting_for_question)
            await send_target.answer(
                "🤖 <b>AI Помощник</b>\n\nЗадайте вопрос — я вижу вашу подписку, ключи и историю платежей, отвечу конкретно по вашей ситуации.",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.exception(f'Ошибка открытия AI-помощника: {e}')
        return

    if args == 'buy_card':
        # ECLIPSE: повторяет логику pay_cards_select_tariff() из
        # bot/handlers/user/payments/yookassa.py, адаптированную под
        # Message вместо CallbackQuery — та функция делает
        # "if ':' in callback.data", а у Message такого атрибута нет,
        # поэтому вызвать её напрямую с send_target нельзя. order_id
        # здесь всегда None — это вход для НОВОЙ покупки, не продолжение
        # уже созданного заказа.
        from database.requests import get_all_tariffs
        from bot.keyboards.user import tariff_select_kb
        from bot.keyboards.admin import home_only_kb
        from bot.handlers.user.payments.tariff_select_page import (
            show_payment_tariff_select_page, build_payment_tariff_select_page_context,
        )
        try:
            tariffs = get_all_tariffs(include_hidden=False)
            if not tariffs:
                await show_payment_tariff_select_page(
                    send_target,
                    context=build_payment_tariff_select_page_context(
                        provider_title_html='💳 <b>TG payments</b>',
                        instruction_html='😔 Нет доступных тарифов.\n\nПопробуйте позже или обратитесь в поддержку.',
                    ),
                    runtime_markup=home_only_kb(),
                )
            else:
                await show_payment_tariff_select_page(
                    send_target,
                    context=build_payment_tariff_select_page_context(provider_title_html='💳 <b>TG payments</b>'),
                    runtime_markup=tariff_select_kb(tariffs, order_id=None, is_cards=True),
                )
        except Exception as e:
            logger.exception(f'Ошибка открытия оплаты картой: {e}')
        return

    if args and (args.startswith('replace_') or args.startswith('renew_')):
        from bot.handlers.user.keys import show_key_details
        try:
            key_id = int(args.split('_', 1)[1])
        except (IndexError, ValueError):
            key_id = None
        if key_id is not None:
            try:
                await show_key_details(telegram_id, key_id, send_target, is_callback=False)
            except Exception as e:
                logger.exception(f'Ошибка открытия карточки ключа: {e}')
            return
    
    try:
        await _render_main_page(render_target, force_new=True)
    except TelegramForbiddenError:
        logger.warning(f'User {telegram_id} blocked the bot during /start')
    except Exception as e:
        logger.error(f'Error sending start message to {telegram_id}: {e}')


@router.callback_query(F.data == 'start')
async def callback_start(callback: CallbackQuery, state: FSMContext):
    """Return to the main screen using the button."""
    user_id = callback.from_user.id
    if is_user_banned(user_id):
        await callback.answer('⛔ Доступ заблокирован', show_alert=True)
        return
    await state.clear()

    rendered = await _render_main_page(callback)
    if rendered:
        await callback.answer()


@router.message(Command('help'))
async def cmd_help(message: Message, state: FSMContext):
    """Command handler /help - calls the logic of the 'Help' button."""
    if is_user_banned(message.from_user.id):
        await render_access_blocked_page(message, force_new=True)
        return
    await state.clear()
    await _render_help_page(message)


@router.message(Command('id'))
async def cmd_id(message: Message):
    """Command handler /id - shows Telegram user ID."""
    await _render_show_id_page(message, force_new=True)


@router.callback_query(F.data == 'show_id')
async def show_id_handler(callback: CallbackQuery):
    """Shows Telegram user ID by page builder button."""
    if is_user_banned(callback.from_user.id):
        await callback.answer('⛔ Доступ заблокирован', show_alert=True)
        return

    await _render_show_id_page(callback)
    await callback.answer()


async def _render_help_page(target):
    """Renders a help page via render_page."""
    from bot.utils.page_renderer import render_page
    await render_page(target, page_key='help')


@router.callback_query(F.data == 'help')
async def help_handler(callback: CallbackQuery):
    """Shows help for a button."""
    await _render_help_page(callback)
    await callback.answer()


@router.callback_query(F.data == 'noop')
async def noop_handler(callback: CallbackQuery):
    """Stub: Clicking on the group header does nothing."""
    await callback.answer()


@router.callback_query(F.data == 'dismiss_msg')
async def dismiss_msg_handler(callback: CallbackQuery):
    """Deletes a message using the OK button."""
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()
