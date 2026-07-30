"""
Router of the “Referral system” section for users.

Displaying referral links and statistics by level.
"""
import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery

from database.requests import (
    is_referral_enabled,
    get_user_internal_id,
)
from bot.utils.page_dynamic_data import build_referral_stats_text

logger = logging.getLogger(__name__)

router = Router()


def format_price_compact(cents: int) -> str:
    """Formats kopecks into a compact ruble string."""
    from bot.utils.page_dynamic_data import format_price_compact as _format_price_compact

    return _format_price_compact(cents)


def _build_stats_text(user_internal_id: int) -> str:
    """Generates a statistics block for the referral statistics placeholder.
    
    Shows only enabled levels and (if reward_type='balance') balance.
    
    Args:
        user_internal_id: Internal user ID
    
    Returns:
        HTML text of the statistics block
    """
    return build_referral_stats_text(user_internal_id)


@router.callback_query(F.data == "referral_leaderboard")
async def show_referral_leaderboard(callback: CallbackQuery):
    """Показывает топ-10 пользователей по количеству приглашённых рефералов."""
    from database.requests import get_referral_leaderboard
    from bot.utils.text import escape_html

    top = get_referral_leaderboard(limit=10)

    if not top:
        await callback.answer("Пока никто не пригласил ни одного реферала — станьте первым! 🚀", show_alert=True)
        return

    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 <b>Топ рефереров</b>", ""]
    for i, row in enumerate(top):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        name = row.get("username") and f"@{escape_html(row['username'])}" or escape_html(row.get("first_name") or "Пользователь")
        lines.append(f"{prefix} {name} — {row['referrals_count']} 👥")

    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="referral_system"))

    from bot.utils.text import safe_edit_or_send
    await safe_edit_or_send(callback.message, "\n".join(lines), reply_markup=builder.as_markup())
    await callback.answer()


@router.callback_query(F.data == "referral_system")
async def show_referral_system(callback: CallbackQuery):
    """Shows the referral system section."""
    from bot.utils.page_renderer import render_page

    telegram_id = callback.from_user.id

    if not is_referral_enabled():
        await callback.answer("❌ Реферальная система недоступна", show_alert=True)
        return

    user_internal_id = get_user_internal_id(telegram_id)
    if not user_internal_id:
        await callback.answer("❌ Ошибка пользователя", show_alert=True)
        return

    await render_page(
        callback,
        page_key='referral',
    )
    await callback.answer()
