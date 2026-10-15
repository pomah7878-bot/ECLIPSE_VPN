"""Совместные акции: тексты для клиентов и аналитика для админа."""
import datetime
import logging
from typing import Any, Dict, Optional, Tuple

from bot.utils.text import escape_html
from database import db_collabs

logger = logging.getLogger(__name__)


def _brand() -> str:
    try:
        from database.db_settings import get_effective_brand_name
        return (get_effective_brand_name() or "").strip() or "ECLIPSE Unlimited"
    except Exception:
        return "ECLIPSE Unlimited"


def _format_date(value: Any) -> str:
    if not value:
        return "без срока"
    try:
        return datetime.datetime.strptime(str(value)[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return str(value)


def get_active_campaign_for_promo(promo: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not promo or not promo.get("id"):
        return None
    try:
        campaign = db_collabs.get_campaign_by_promo_id(int(promo["id"]))
    except Exception as e:
        logger.debug("Не удалось получить акцию для промокода: %s", e)
        return None
    return campaign if campaign and campaign.get("is_active") else None


def client_promo_screen(promo: Optional[Dict[str, Any]]) -> Optional[Tuple[str, str]]:
    """(заголовок, текст) экрана клиента для промокода совместной акции, иначе None."""
    campaign = get_active_campaign_for_promo(promo)
    if not campaign:
        return None
    months = int(campaign.get("min_months") or 0)
    title = f"🤝 <b>Совместная акция {escape_html(_brand())} × {escape_html(campaign['title'])}</b>"

    lines = [f"Промокод <b>{escape_html(promo['code'])}</b> сохранён и будет учтён при оплате.", ""]
    lines.append("🎁 <b>Условия</b>")
    lines.append(f"• Скидка: <b>{int(promo.get('discount_percent') or 0)}%</b>")
    if months > 0:
        lines.append(f"• Действует на тарифы от <b>{months} мес.</b>")
    lines.append(f"• Акция до: <b>{escape_html(_format_date(promo.get('expires_at')))}</b>")
    description = (campaign.get("description") or "").strip()
    if description:
        lines += ["", escape_html(description)]
    lines += [
        "",
        "Выберите тариф"
        + (f" от {months} мес." if months > 0 else "")
        + " — скидка применится автоматически.",
    ]
    return title, "\n".join(lines)


def campaign_card_text(campaign: Dict[str, Any], bot_username: Optional[str] = None) -> str:
    """Карточка акции для админа."""
    from bot.utils.telegram_links import build_telegram_link

    status = "🟢 включена" if campaign.get("is_active") else "⚪ выключена"
    limit = campaign.get("activation_limit")
    months = int(campaign.get("min_months") or 0)
    lines = [
        f"🤝 <b>Совместная акция: {escape_html(campaign['title'])}</b>",
        "",
        f"Статус: <b>{status}</b>",
        "",
        "🎟 <b>Промокод для клиентов</b>",
        f"• Код: <code>{escape_html(str(campaign.get('promo_code') or '—'))}</code>",
        f"• Скидка: <b>{int(campaign.get('discount_percent') or 0)}%</b>"
        + (f" на тарифы от {months} мес." if months > 0 else " на любые тарифы"),
        f"• Действует до: <b>{escape_html(_format_date(campaign.get('promo_expires_at')))}</b>",
        f"• Лимит активаций: <b>{limit if limit else 'без лимита'}</b>",
        "",
        "🔑 <b>Ключи участникам партнёра</b>",
        f"• Безлимитный трафик, до <b>{int(campaign.get('member_devices') or 5)}</b> устройств",
        f"• Срок ключа: <b>{int(campaign.get('member_days') or 90)}</b> дн.",
    ]
    if bot_username and campaign.get("promo_code"):
        link = build_telegram_link(bot_username, f"pr_{campaign['promo_code']}")
        lines += ["", f"🔗 Промо-ссылка: <code>{escape_html(link)}</code>"]
    return "\n".join(lines)


def campaign_stats_text(campaign: Dict[str, Any]) -> str:
    """Аналитика акции для админа (структурированным списком)."""
    stats = db_collabs.get_stats(int(campaign["id"]))
    visits, unique = stats["visits"], stats["unique_visitors"]
    paid = stats["paid"]
    conversion = f"{paid * 100 / unique:.1f}%" if unique else "—"
    money = stats["money"]

    lines = [
        f"📊 <b>Аналитика: {escape_html(campaign['title'])}</b>",
        "",
        "👀 <b>Воронка</b>",
        f"• Переходов по промо-ссылке: <b>{visits}</b> (уникальных: <b>{unique}</b>)",
        f"• Сейчас держат промокод: <b>{stats['holders']}</b>",
        f"• Начали оплату, не завершили: <b>{stats['reserved']}</b>",
        f"• Оплатили со скидкой: <b>{paid}</b> (клиентов: <b>{stats['buyers']}</b>)",
        f"• Конверсия в оплату: <b>{conversion}</b>",
        "",
        "💰 <b>Деньги</b>",
        f"• Выручка по акции: <b>{escape_html(db_collabs.format_money_totals(money, 'final'))}</b>",
        f"• Выдано скидок: <b>{escape_html(db_collabs.format_money_totals(money, 'discount'))}</b>",
        f"• Цена без скидок: <b>{escape_html(db_collabs.format_money_totals(money, 'original'))}</b>",
        "",
        "🔑 <b>Участники партнёра</b>",
        f"• Выдано бесплатных ключей: <b>{stats['members']}</b>",
    ]
    if paid == 0 and visits == 0:
        lines += ["", "Пока данных нет: статистика появится, когда клиенты начнут переходить по ссылке."]
    return "\n".join(lines)


async def notify_member(bot, campaign_id: int, telegram_id: int) -> None:
    """Сообщает участнику партнёра, что ему открыт доступ по совместной акции."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    campaign = db_collabs.get_campaign(campaign_id)
    if not campaign:
        return
    text = (
        f"🤝 <b>Совместная акция {escape_html(_brand())} × {escape_html(campaign['title'])}</b>\n\n"
        "Вам открыт бесплатный доступ:\n"
        "• без ограничения трафика;\n"
        f"• до <b>{int(campaign['member_devices'])}</b> устройств одновременно;\n"
        f"• срок: <b>{int(campaign['member_days'])}</b> дн.\n\n"
        "Ключ — в разделе «🔑 Мои ключи»."
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔑 Мои ключи", callback_data="my_keys")],
        [InlineKeyboardButton(text="🈴 На главную", callback_data="start")],
    ])
    try:
        await bot.send_message(telegram_id, text, reply_markup=markup, parse_mode="HTML")
    except Exception as e:
        logger.info("Участник %s не получил уведомление об акции: %s", telegram_id, e)
