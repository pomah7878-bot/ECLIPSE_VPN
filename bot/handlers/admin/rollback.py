"""Админка → Обновление → «🛟 Откат и восстановление»: откат версии бота и восстановление базы из бэкапа."""
import asyncio
import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.keyboards.admin_misc import back_button, home_button
from bot.services import rollback as rb
from bot.utils.admin import is_admin
from bot.utils.text import escape_html, safe_edit_or_send

logger = logging.getLogger(__name__)
router = Router()


def _nav(back: str = 'admin_rb'):
    return [back_button(back), home_button()]


def _kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime('%d.%m.%Y %H:%M') if ts else '—'


async def _deny(callback: CallbackQuery) -> bool:
    if is_admin(callback.from_user.id):
        return False
    await callback.answer('⛔ Доступ запрещён', show_alert=True)
    return True


@router.callback_query(F.data == 'admin_rb')
async def cb_menu(callback: CallbackQuery):
    if await _deny(callback):
        return
    await safe_edit_or_send(
        callback.message,
        "🛟 <b>Откат и восстановление</b>\n\n"
        "<b>↩️ Откатить версию бота</b>\n"
        "• вернёт код на одну из трёх прежних версий (запоминаются при обновлении)\n"
        "• база не меняется, перед откатом делается её копия\n\n"
        "<b>🗄 Восстановить базу</b>\n"
        "• заменит текущую базу бота выбранной копией (ежедневные бэкапы)\n"
        "• все изменения после копии пропадут: платежи, ключи, пользователи\n"
        "• перед заменой делается копия текущей базы\n\n"
        "<i>После любого действия бот перезапускается.</i>",
        reply_markup=_kb([
            [InlineKeyboardButton(text='↩️ Откатить версию бота', callback_data='admin_rb_code')],
            [InlineKeyboardButton(text='🗄 Восстановить базу', callback_data='admin_rb_db')],
            _nav('admin_update_bot'),
        ]),
    )
    await callback.answer()


@router.callback_query(F.data == 'admin_rb_code')
async def cb_code_list(callback: CallbackQuery):
    if await _deny(callback):
        return
    points = await asyncio.to_thread(rb.list_points)
    if not points:
        text = "↩️ <b>Откат версии</b>\n\nПрежних версий пока нет: они запоминаются при следующем обновлении."
        rows = []
    else:
        text = "↩️ <b>Откат версии</b>\n\nВыберите версию, на которую вернуться:"
        rows = [[InlineKeyboardButton(
            text=f"{p['commit'][:7]} · {_fmt(p['ts'])}", callback_data=f"admin_rb_c:{p['commit'][:12]}")]
            for p in points]
        text += "\n\n" + "\n".join(f"• <code>{p['commit'][:7]}</code> {escape_html(p['subject'][:80])}" for p in points)
    await safe_edit_or_send(callback.message, text, reply_markup=_kb(rows + [_nav()]))
    await callback.answer()


async def _find_point(prefix: str):
    points = await asyncio.to_thread(rb.list_points)
    return next((p for p in points if p['commit'].startswith(prefix)), None)


@router.callback_query(F.data.startswith('admin_rb_c:'))
async def cb_code_confirm(callback: CallbackQuery):
    if await _deny(callback):
        return
    p = await _find_point(callback.data.split(':', 1)[1])
    if not p:
        await callback.answer('Версия не найдена, откройте список заново', show_alert=True)
        return
    await safe_edit_or_send(
        callback.message,
        f"↩️ <b>Откатить на <code>{p['commit'][:7]}</code>?</b>\n\n{escape_html(p['subject'][:120])}\n\n"
        "• копия базы будет сделана\n• бот перезапустится\n• если старая версия не запустится, останется текущая",
        reply_markup=_kb([
            [InlineKeyboardButton(text='✅ Откатить и перезапустить', callback_data=f"admin_rb_cy:{p['commit'][:12]}")],
            _nav('admin_rb_code'),
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith('admin_rb_cy:'))
async def cb_code_do(callback: CallbackQuery):
    if await _deny(callback):
        return
    p = await _find_point(callback.data.split(':', 1)[1])
    if not p:
        await callback.answer('Версия не найдена', show_alert=True)
        return
    await callback.answer()
    await safe_edit_or_send(callback.message, "⏳ <b>Откат...</b>\n\nДелаю копию базы и проверяю старую версию.")
    ok, msg = await asyncio.to_thread(rb.rollback_code, p['commit'])
    if not ok:
        logger.error('Откат не выполнен: %s', msg)
        await safe_edit_or_send(callback.message, f"❌ <b>Откат не выполнен</b>\n\n{escape_html(msg)}",
                                reply_markup=_kb([_nav()]))
        return
    from bot.utils.git_utils import install_requirements, restart_bot
    await asyncio.to_thread(install_requirements)
    logger.info('Откат кода на %s выполнил админ %s', p['commit'][:7], callback.from_user.id)
    await safe_edit_or_send(callback.message, f"✅ Версия <code>{p['commit'][:7]}</code> установлена. Перезапускаю бота...")
    restart_bot(notify_admin_id=callback.from_user.id)


@router.callback_query(F.data == 'admin_rb_db')
async def cb_db_list(callback: CallbackQuery):
    if await _deny(callback):
        return
    items = await asyncio.to_thread(rb.list_db_backups)
    if not items:
        text = "🗄 <b>Восстановление базы</b>\n\nЛокальных копий не найдено."
        rows = []
    else:
        text = "🗄 <b>Восстановление базы</b>\n\nВыберите копию. Всё, что изменилось после неё, пропадёт."
        rows = [[InlineKeyboardButton(
            text=f"{_fmt(i['mtime'])} · {i['size'] // 1024} КБ", callback_data=f"admin_rb_d:{i['id']}")]
            for i in items]
    await safe_edit_or_send(callback.message, text, reply_markup=_kb(rows + [_nav()]))
    await callback.answer()


async def _find_db(bid: str):
    items = await asyncio.to_thread(rb.list_db_backups)
    return next((i for i in items if i['id'] == bid), None)


@router.callback_query(F.data.startswith('admin_rb_d:'))
async def cb_db_confirm(callback: CallbackQuery):
    if await _deny(callback):
        return
    i = await _find_db(callback.data.split(':', 1)[1])
    if not i:
        await callback.answer('Копия не найдена, откройте список заново', show_alert=True)
        return
    await safe_edit_or_send(
        callback.message,
        f"🗄 <b>Восстановить базу от {_fmt(i['mtime'])}?</b>\n\n"
        "⚠️ Платежи, ключи и пользователи, появившиеся после этой копии, пропадут.\n"
        "• текущая база будет сохранена в backup/\n• бот перезапустится",
        reply_markup=_kb([
            [InlineKeyboardButton(text='⚠️ Да, восстановить', callback_data=f"admin_rb_dy:{i['id']}")],
            _nav('admin_rb_db'),
        ]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith('admin_rb_dy:'))
async def cb_db_do(callback: CallbackQuery):
    if await _deny(callback):
        return
    bid = callback.data.split(':', 1)[1]
    await callback.answer()
    await safe_edit_or_send(callback.message, "⏳ <b>Восстановление...</b>\n\nСохраняю текущую базу и проверяю копию.")
    ok, msg = await asyncio.to_thread(rb.restore_db, bid)
    if not ok:
        logger.error('Восстановление базы не выполнено: %s', msg)
        await safe_edit_or_send(callback.message, f"❌ <b>База не восстановлена</b>\n\n{escape_html(msg)}\n\nТекущая база не тронута.",
                                reply_markup=_kb([_nav('admin_rb_db')]))
        return
    from bot.utils.git_utils import restart_bot
    logger.info('База восстановлена из копии %s, админ %s', bid, callback.from_user.id)
    await safe_edit_or_send(callback.message, "✅ База восстановлена. Перезапускаю бота...")
    restart_bot(notify_admin_id=callback.from_user.id)
