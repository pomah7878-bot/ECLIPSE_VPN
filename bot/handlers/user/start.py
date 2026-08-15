import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramForbiddenError
from config import ADMIN_IDS
from database.requests import get_or_create_user, is_user_banned, get_setting, is_referral_enabled, get_user_by_referral_code, set_user_referrer
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

    visibility = {
        'btn_trial': show_trial,
        'btn_referral': show_referral,
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
    try:
        from database.requests import count_unread_channel_posts, mark_channel_posts_seen, get_max_sent_post_id
        unread_count = count_unread_channel_posts(user_id)
        news_label = f'📰 Новости ({unread_count})' if unread_count > 0 else '📰 Новости ECLIPSE Unlimited'
        news_button_row = [InlineKeyboardButton(text=news_label, url='https://t.me/eclipse_unlimited_news')]
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
    if args:
        try:
            from bot.handlers.user.payments.base import handle_payment_deeplink
            if await handle_payment_deeplink(
                message, state, args,
                user_internal_id=user['id'],
                telegram_id=message.from_user.id,
            ):
                return
        except Exception as e:
            logger.exception(f'Ошибка обработки платёжного deep-link: {e}')
            await _show_start_payment_status(
                message,
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
                telegram_id=message.from_user.id,
                start_param=args,
            )
            await render_promo_status_page(
                message,
                title_html="🎟 <b>Промокод сохранён</b>",
                body_html=(
                    f"Код <b>{escape_html(promo['code'])}</b> "
                    "будет учтён при следующей оплате."
                ),
                force_new=True,
            )
        else:
            await render_promo_status_page(
                message,
                title_html="⚠️ <b>Промо-ссылка недоступна</b>",
                body_text=promo_result['message'],
                force_new=True,
            )

    if args and args.startswith('claim_'):
        from bot.services.anonymous_purchase import claim_anonymous_purchase
        from bot.keyboards.admin import home_only_kb

        code = args[6:].strip()
        claim_result = await claim_anonymous_purchase(
            code, message.from_user.id, username=message.from_user.username,
        )
        if claim_result['ok']:
            await message.answer(
                f"✅ <b>Ключ привязан к вашему аккаунту!</b>\n\n{escape_html(claim_result['message'])}\n\n"
                "Откройте раздел «Мои ключи», чтобы увидеть его.",
                parse_mode='HTML',
                reply_markup=home_only_kb(),
            )
        else:
            await message.answer(
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
                logger.info(f"User {user_id} привязан к рефереру {referrer['telegram_id']}")
                try:
                    from bot.services.notifications import notify_referrers_new_referral
                    await notify_referrers_new_referral(message.bot, user['id'])
                except Exception as notify_err:
                    logger.warning(f'Ошибка уведомления о новом реферале: {notify_err}')

    
    # Обработка deep-link параметров для покупки и замены ключей
    if args == 'buy':
        from bot.handlers.user.tariffs import _render_buy_page
        try:
            await _render_buy_page(message)
        except Exception as e:
            logger.exception(f'Ошибка открытия страницы покупки: {e}')
        return

    if args == 'trial':
        from database.requests import is_trial_enabled, get_trial_tariff_id, has_used_trial
        from bot.utils.page_renderer import render_page
        try:
            if not is_trial_enabled():
                await message.answer('❌ Пробная подписка сейчас недоступна')
            elif get_trial_tariff_id() is None:
                await message.answer('❌ Пробный тариф не настроен')
            elif has_used_trial(message.from_user.id):
                await message.answer('ℹ️ Вы уже использовали пробный период')
            else:
                await render_page(message, page_key='trial')
        except Exception as e:
            logger.exception(f'Ошибка открытия страницы триала: {e}')
        return
    

    if args == 'support':
        try:
            await _start_support_dialog(message, state)
        except Exception as e:
            logger.exception(f'Ошибка открытия поддержки: {e}')
        return

    if args and (args.startswith('replace_') or args.startswith('renew_')):
        from bot.handlers.user.keys import show_key_details
        try:
            key_id = int(args.split('_', 1)[1])
        except (IndexError, ValueError):
            key_id = None
        if key_id is not None:
            try:
                await show_key_details(message.from_user.id, key_id, message, is_callback=False)
            except Exception as e:
                logger.exception(f'Ошибка открытия карточки ключа: {e}')
            return
    
    try:
        await _render_main_page(message, force_new=True)
    except TelegramForbiddenError:
        logger.warning(f'User {user_id} blocked the bot during /start')
    except Exception as e:
        logger.error(f'Error sending start message to {user_id}: {e}')


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
