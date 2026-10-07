"""v1.218: бесплатная рулетка 🎰 (слот-машина Telegram).

Результат барабана определяет сам Telegram (send_dice), бот его не выбирает.
Приз зависит от комбинации. Одна попытка за период, нужен активный ключ.
Админ управляет в «📣 Маркетинг» → «🎰 Рулетка». По умолчанию рулетка выключена."""
import asyncio
import json
import logging

from aiogram import Router, F
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.types import CallbackQuery, Message

logger = logging.getLogger(__name__)

router = Router()

# Символы барабана Telegram (🎰): 0 = BAR, 1 = виноград, 2 = лимон, 3 = семёрка.
# Значение кубика 1..64: value-1 = левый + 4*средний + 16*правый.
TIERS = ("jackpot", "triple", "sevens", "pair", "none")
TIER_CHANCE = {"jackpot": 1, "triple": 3, "sevens": 9, "pair": 27, "none": 24}  # из 64
TIER_NAME = {
    "jackpot": "Три семёрки 7️⃣7️⃣7️⃣",
    "triple": "Три одинаковых",
    "sevens": "Две семёрки",
    "pair": "Пара одинаковых",
    "none": "Без совпадений",
}
DEFAULT_PRIZES = {
    "jackpot": {"type": "days", "amount": 30},
    "triple": {"type": "days", "amount": 7},
    "sevens": {"type": "days", "amount": 3},
    "pair": {"type": "rub", "amount": 10},
    "none": {"type": "none", "amount": 0},
}
ANIMATION_SECONDS = 3.6
MAX_DAYS = 365
MAX_RUB = 10000


def classify(value: int) -> str:
    d = int(value) - 1
    reels = (d % 4, (d // 4) % 4, d // 16)
    sevens = reels.count(3)
    if sevens == 3:
        return "jackpot"
    if len(set(reels)) == 1:
        return "triple"
    if sevens == 2:
        return "sevens"
    if len(set(reels)) == 2:
        return "pair"
    return "none"


def _setting(key: str, default: str) -> str:
    from database.requests import get_setting
    value = get_setting(key, default)
    return default if value is None else str(value)


def is_licensed() -> bool:
    from bot.services.license import is_feature_available
    return is_feature_available("roulette")


def is_enabled() -> bool:
    return is_licensed() and _setting("roulette_enabled", "0") == "1"


def period_days() -> int:
    try:
        return min(365, max(1, int(_setting("roulette_period_days", "7"))))
    except ValueError:
        return 7


def get_prizes() -> dict:
    prizes = {k: dict(v) for k, v in DEFAULT_PRIZES.items()}
    try:
        saved = json.loads(_setting("roulette_prizes", "{}") or "{}")
    except ValueError:
        saved = {}
    for tier in TIERS:
        item = saved.get(tier) if isinstance(saved, dict) else None
        if isinstance(item, dict) and item.get("type") in ("days", "rub", "none"):
            try:
                amount = int(item.get("amount", 0))
            except (TypeError, ValueError):
                continue
            if item["type"] == "none":
                prizes[tier] = {"type": "none", "amount": 0}
            elif amount > 0:
                prizes[tier] = {"type": item["type"], "amount": amount}
    return prizes


def _plural_days(n: int) -> str:
    if 11 <= n % 100 <= 14:
        return "дней"
    return {1: "день", 2: "дня", 3: "дня", 4: "дня"}.get(n % 10, "дней")


def prize_text(prize: dict) -> str:
    if prize["type"] == "days":
        return f"+{prize['amount']} {_plural_days(prize['amount'])} к подписке"
    if prize["type"] == "rub":
        return f"+{prize['amount']} ₽ на баланс"
    return "без приза"


def _wait_text(seconds: int) -> str:
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    parts = []
    if days:
        parts.append(f"{days} д")
    if hours:
        parts.append(f"{hours} ч")
    if not days and minutes:
        parts.append(f"{minutes} мин")
    return " ".join(parts) or "1 мин"


def _home_kb(close_dice_id: int = 0, via_button: bool = False):
    """Кнопка «На главную». Если рулетку открыли кнопкой с главной страницы —
    сообщения рулетки просто закрываются и пользователь остаётся на прежней
    странице. Если вызвали командой /spin — открывается главная страница."""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    cb = f"roulette_close:{int(close_dice_id)}" if via_button else "start"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🈴 На главную", callback_data=cb)],
    ])


async def _say_base(bot, chat_id: int, text: str, dice_id: int = 0, via_button: bool = False) -> None:
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML",
                               reply_markup=_home_kb(dice_id, via_button))
    except Exception as e:
        logger.warning("Рулетка: не удалось отправить сообщение %s: %s", chat_id, e)


async def run_spin(bot, chat_id: int, telegram_id: int, via_button: bool = False) -> None:
    from bot.services.user_locks import user_locks
    from database.requests import (
        get_first_active_key_for_user, get_user_internal_id,
        reserve_roulette_spin, claim_roulette_spin, finish_roulette_spin, fail_roulette_spin,
    )

    dice_id = 0

    async def _say(bot, chat_id, text):  # noqa: F811 — локальная обёртка с контекстом
        await _say_base(bot, chat_id, text, dice_id, via_button)

    if not is_enabled():
        await _say(bot, chat_id, "🎰 <b>Рулетка</b>\n\nСейчас рулетка недоступна.")
        return
    user_id = get_user_internal_id(telegram_id)
    if not user_id:
        await _say(bot, chat_id, "🎰 <b>Рулетка</b>\n\nСначала нажмите /start.")
        return
    if not get_first_active_key_for_user(user_id):
        await _say(bot, chat_id, "🎰 <b>Рулетка</b>\n\nДля участия нужен активный ключ.")
        return

    period = period_days()
    async with user_locks[user_id]:
        reservation = reserve_roulette_spin(user_id, telegram_id, period)
    if "wait_seconds" in reservation:
        await _say(
            bot, chat_id,
            "🎰 <b>Рулетка</b>\n\n"
            "Вы уже крутили барабан.\n"
            f"⏳ Следующая попытка — через <b>{_wait_text(reservation['wait_seconds'])}</b>.",
        )
        return
    spin_id = reservation["spin_id"]

    try:
        dice_msg = await bot.send_dice(chat_id, emoji="🎰")
        value = int(dice_msg.dice.value)
        dice_id = dice_msg.message_id
    except Exception as e:
        logger.warning("Рулетка: не удалось запустить барабан user=%s: %s", telegram_id, e)
        fail_roulette_spin(spin_id)
        await _say(bot, chat_id, "🎰 Не удалось запустить барабан. Попробуйте ещё раз чуть позже.")
        return

    await asyncio.sleep(ANIMATION_SECONDS)

    tier = classify(value)
    prize = get_prizes()[tier]
    if not claim_roulette_spin(spin_id, value):
        return

    ok = True
    try:
        reason = f"Приз рулетки: {TIER_NAME[tier]}"
        if prize["type"] == "days":
            from bot.services.rewards import grant_days_to_first_active_key
            result = await grant_days_to_first_active_key(
                user_id, prize["amount"], source="roulette", reason=reason,
                reference_type="roulette_spin", reference_id=str(spin_id),
                metadata={"dice_value": value, "tier": tier},
            )
            ok = bool(result.get("ok"))
        elif prize["type"] == "rub":
            from bot.services.balance import credit_user_balance
            result = await credit_user_balance(
                user_id, prize["amount"] * 100, source="roulette", reason=reason,
                reference_type="roulette_spin", reference_id=str(spin_id),
                metadata={"dice_value": value, "tier": tier},
            )
            ok = bool(result.get("ok"))
    except Exception as e:
        logger.exception("Рулетка: ошибка начисления spin=%s user=%s: %s", spin_id, telegram_id, e)
        ok = False

    finish_roulette_spin(spin_id, "done" if ok else "failed", tier, prize["type"], prize["amount"])
    next_try = f"⏳ Следующая попытка — через <b>{period} {_plural_days(period)}</b>."
    if not ok:
        await _say(bot, chat_id,
                   "🎰 <b>Рулетка</b>\n\n"
                   f"Комбинация: <b>{TIER_NAME[tier]}</b>\n"
                   "⚠️ Приз не удалось начислить автоматически. Напишите в поддержку — мы начислим вручную.")
    elif prize["type"] == "none":
        await _say(bot, chat_id,
                   "🎰 <b>Не повезло</b>\n\n"
                   f"Комбинация: <b>{TIER_NAME[tier]}</b>\n"
                   f"{next_try}")
    else:
        await _say(bot, chat_id,
                   "🎉 <b>Победа!</b>\n\n"
                   f"Комбинация: <b>{TIER_NAME[tier]}</b>\n"
                   f"🎁 Приз: <b>{prize_text(prize)}</b>\n\n"
                   f"{next_try}")


@router.message(Command("spin"), StateFilter("*"))
async def cmd_spin(message: Message):
    await run_spin(message.bot, message.chat.id, message.from_user.id)


@router.callback_query(F.data == "roulette_spin")
async def cb_spin(callback: CallbackQuery):
    await callback.answer()
    await run_spin(callback.message.bot, callback.message.chat.id, callback.from_user.id, via_button=True)


@router.callback_query(F.data.startswith("roulette_close"))
async def cb_close(callback: CallbackQuery):
    """Убирает сообщения рулетки — под ними остаётся прежняя страница."""
    await callback.answer()
    chat_id = callback.message.chat.id
    try:
        await callback.message.delete()
    except Exception:
        pass
    parts = callback.data.split(":")
    if len(parts) == 2 and parts[1].isdigit() and int(parts[1]) > 0:
        try:
            await callback.message.bot.delete_message(chat_id, int(parts[1]))
        except Exception:
            pass


# ===== Управление для админа: «📣 Маркетинг» → «🎰 Рулетка» =====

PERIOD_CHOICES = (1, 3, 7, 14, 30)
AMOUNT_PRESETS = {
    "days": (1, 3, 7, 14, 30, 60, 90),
    "rub": (5, 10, 20, 50, 100, 200, 500),
}
TYPE_LABEL = {"days": "📅 Дни подписки", "rub": "💰 Рубли на баланс", "none": "🚫 Без приза"}


def _chance_pct(tier: str) -> str:
    return f"{TIER_CHANCE[tier] / 64 * 100:.1f}%".replace(".", ",")


def _is_admin(user_id: int) -> bool:
    from bot.utils.admin import is_admin
    return is_admin(user_id)


def _status_text() -> str:
    prizes = get_prizes()
    exp_days = sum(TIER_CHANCE[t] * p["amount"] for t, p in prizes.items() if p["type"] == "days") / 64
    exp_rub = sum(TIER_CHANCE[t] * p["amount"] for t, p in prizes.items() if p["type"] == "rub") / 64
    lines = [
        "🎰 <b>Рулетка</b>",
        "",
        f"Статус: {'✅ включена' if is_enabled() else '⛔ выключена'}",
        f"Период: 1 прокрутка в <b>{period_days()} {_plural_days(period_days())}</b>",
        "Условие: активный ключ. Клиенты крутят командой /spin",
        "",
        "<b>Призы</b> (шанс · комбинация):",
    ]
    for t in TIERS:
        lines.append(f"• {_chance_pct(t)} · {TIER_NAME[t]}: <b>{prize_text(prizes[t])}</b>")
    lines += ["", f"В среднем за прокрутку: ≈ {exp_days:.2f} дн. и ≈ {exp_rub:.2f} ₽"]
    return "\n".join(lines)


def _main_kb():
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    from bot.keyboards.admin_misc import back_button, home_button
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(
        text="⛔ Выключить" if is_enabled() else "✅ Включить", callback_data="adm_rl_toggle"))
    b.row(InlineKeyboardButton(text="⏱ Период", callback_data="adm_rl_period"),
          InlineKeyboardButton(text="🎁 Призы", callback_data="adm_rl_prizes"))
    b.row(InlineKeyboardButton(text="📊 Статистика", callback_data="adm_rl_stats"))
    b.row(back_button("admin_marketing"), home_button())
    return b.as_markup()


async def _guard(callback: CallbackQuery) -> bool:
    """Только админ и только при наличии лицензии на функцию."""
    if not _is_admin(callback.from_user.id):
        await callback.answer("⛔ Доступ запрещён", show_alert=True)
        return False
    if not is_licensed():
        await callback.answer("🎰 Рулетка доступна только по лицензии", show_alert=True)
        return False
    return True


async def _show_main(callback: CallbackQuery) -> None:
    from bot.utils.text import safe_edit_or_send
    await safe_edit_or_send(callback.message, _status_text(), reply_markup=_main_kb())


@router.callback_query(F.data == "admin_roulette")
async def cb_admin_roulette(callback: CallbackQuery):
    if not await _guard(callback):
        return
    await _show_main(callback)
    await callback.answer()


@router.callback_query(F.data == "adm_rl_toggle")
async def cb_rl_toggle(callback: CallbackQuery):
    if not await _guard(callback):
        return
    from database.requests import set_setting
    set_setting("roulette_enabled", "0" if is_enabled() else "1")
    await _show_main(callback)
    await callback.answer("Готово")


@router.callback_query(F.data == "adm_rl_period")
async def cb_rl_period(callback: CallbackQuery):
    if not await _guard(callback):
        return
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    from bot.keyboards.admin_misc import back_button, home_button
    from bot.utils.text import safe_edit_or_send
    cur = period_days()
    b = InlineKeyboardBuilder()
    b.row(*[InlineKeyboardButton(text=("✅ " if n == cur else "") + f"{n} дн.",
                                 callback_data=f"adm_rl_per:{n}") for n in PERIOD_CHOICES])
    b.row(back_button("admin_roulette"), home_button())
    await safe_edit_or_send(
        callback.message,
        "⏱ <b>Период рулетки</b>\n\nКак часто один клиент может крутить барабан:",
        reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("adm_rl_per:"))
async def cb_rl_set_period(callback: CallbackQuery):
    if not await _guard(callback):
        return
    try:
        n = int(callback.data.split(":")[1])
    except ValueError:
        await callback.answer()
        return
    if n not in PERIOD_CHOICES:
        await callback.answer()
        return
    from database.requests import set_setting
    set_setting("roulette_period_days", str(n))
    await _show_main(callback)
    await callback.answer("Сохранено")


@router.callback_query(F.data == "adm_rl_prizes")
async def cb_rl_prizes(callback: CallbackQuery):
    if not await _guard(callback):
        return
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    from bot.keyboards.admin_misc import back_button, home_button
    from bot.utils.text import safe_edit_or_send
    prizes = get_prizes()
    b = InlineKeyboardBuilder()
    for t in TIERS:
        b.row(InlineKeyboardButton(
            text=f"{_chance_pct(t)} {TIER_NAME[t]} — {prize_text(prizes[t])}",
            callback_data=f"adm_rl_t:{t}"))
    b.row(back_button("admin_roulette"), home_button())
    await safe_edit_or_send(
        callback.message,
        "🎁 <b>Призы рулетки</b>\n\nВыберите комбинацию, чтобы изменить приз:",
        reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("adm_rl_t:"))
async def cb_rl_tier(callback: CallbackQuery):
    if not await _guard(callback):
        return
    tier = callback.data.split(":")[1]
    if tier not in TIERS:
        await callback.answer()
        return
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    from bot.keyboards.admin_misc import back_button, home_button
    from bot.utils.text import safe_edit_or_send
    b = InlineKeyboardBuilder()
    for ptype in ("days", "rub", "none"):
        b.row(InlineKeyboardButton(text=TYPE_LABEL[ptype], callback_data=f"adm_rl_y:{tier}:{ptype}"))
    b.row(back_button("adm_rl_prizes"), home_button())
    await safe_edit_or_send(
        callback.message,
        f"🎁 <b>{TIER_NAME[tier]}</b> · шанс {_chance_pct(tier)}\n"
        f"Сейчас: <b>{prize_text(get_prizes()[tier])}</b>\n\nЧто выдавать:",
        reply_markup=b.as_markup())
    await callback.answer()


async def _save_prize(callback: CallbackQuery, tier: str, ptype: str, amount: int) -> None:
    from database.requests import set_setting
    prizes = get_prizes()
    prizes[tier] = {"type": ptype, "amount": amount}
    set_setting("roulette_prizes", json.dumps(prizes, ensure_ascii=False))


@router.callback_query(F.data.startswith("adm_rl_y:"))
async def cb_rl_type(callback: CallbackQuery):
    if not await _guard(callback):
        return
    parts = callback.data.split(":")
    if len(parts) != 3 or parts[1] not in TIERS or parts[2] not in ("days", "rub", "none"):
        await callback.answer()
        return
    tier, ptype = parts[1], parts[2]
    if ptype == "none":
        await _save_prize(callback, tier, "none", 0)
        await callback.answer("Сохранено")
        await cb_rl_prizes(callback)
        return
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from aiogram.types import InlineKeyboardButton
    from bot.keyboards.admin_misc import back_button, home_button
    from bot.utils.text import safe_edit_or_send
    unit = "дней" if ptype == "days" else "₽"
    b = InlineKeyboardBuilder()
    btns = [InlineKeyboardButton(text=f"{n} {unit}", callback_data=f"adm_rl_a:{tier}:{ptype}:{n}")
            for n in AMOUNT_PRESETS[ptype]]
    for i in range(0, len(btns), 3):
        b.row(*btns[i:i + 3])
    b.row(back_button(f"adm_rl_t:{tier}"), home_button())
    await safe_edit_or_send(
        callback.message,
        f"🎁 <b>{TIER_NAME[tier]}</b>\n{TYPE_LABEL[ptype]} — выберите размер приза:",
        reply_markup=b.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("adm_rl_a:"))
async def cb_rl_amount(callback: CallbackQuery):
    if not await _guard(callback):
        return
    parts = callback.data.split(":")
    if len(parts) != 4 or parts[1] not in TIERS or parts[2] not in AMOUNT_PRESETS:
        await callback.answer()
        return
    try:
        amount = int(parts[3])
    except ValueError:
        await callback.answer()
        return
    if amount not in AMOUNT_PRESETS[parts[2]]:
        await callback.answer()
        return
    await _save_prize(callback, parts[1], parts[2], amount)
    await callback.answer("Сохранено")
    await cb_rl_prizes(callback)


@router.callback_query(F.data == "adm_rl_stats")
async def cb_rl_stats(callback: CallbackQuery):
    if not await _guard(callback):
        return
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    from bot.keyboards.admin_misc import back_button, home_button
    from bot.utils.text import safe_edit_or_send
    from database.requests import get_roulette_stats
    s = get_roulette_stats()
    st, tiers = s["by_status"], s["by_tier"]
    text = (
        "📊 <b>Статистика рулетки</b>\n\n"
        f"Участников: <b>{s['users']}</b>\n"
        f"Прокруток за 7 дней: <b>{s['week']}</b>\n"
        f"Выдано: <b>{s['days']}</b> дн. и <b>{s['rub']}</b> ₽\n\n"
        f"Успешных: {st.get('done', 0)}\n"
        f"Ошибок начисления: {st.get('failed', 0)}\n"
        f"Незавершённых: {st.get('pending', 0) + st.get('granting', 0)}\n\n"
        "<b>По комбинациям</b>\n"
        + "\n".join(f"• {TIER_NAME[t]}: {tiers.get(t, 0)}" for t in TIERS)
    )
    b = InlineKeyboardBuilder()
    b.row(back_button("admin_roulette"), home_button())
    await safe_edit_or_send(callback.message, text, reply_markup=b.as_markup())
    await callback.answer()


@router.message(Command("roulette"), StateFilter("*"))
async def cmd_roulette(message: Message):
    """Короткий путь в панель (основной вход — «📣 Маркетинг» → «🎰 Рулетка»)."""
    if not _is_admin(message.from_user.id):
        return
    if not is_licensed():
        from bot.services.license import FEATURE_UPGRADE_MESSAGE
        await message.answer("🎰 <b>Рулетка</b>\n\n" + FEATURE_UPGRADE_MESSAGE, parse_mode="HTML")
        return
    await message.answer(_status_text(), parse_mode="HTML", reply_markup=_main_kb())
