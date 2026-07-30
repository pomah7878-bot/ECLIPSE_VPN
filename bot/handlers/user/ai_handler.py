"""
AI Support Handler - полноценная интеграция AI помощника в бот
Команда: /ai вопрос
"""
import logging
import base64
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.handlers.user.ai_support_integration import get_ai_response, send_ai_feedback, get_ai_stats
from bot.utils.admin import is_admin
from bot.utils.text import escape_html
import re

logger = logging.getLogger(__name__)

router = Router()

_URL_RE = re.compile(r'https?://\S+')
_TAG_RE = re.compile(r'<[^>]+>')


def _format_ai_reply_html(text: str) -> str:
    """
    ВАЖНО: бэкенд AI (_TelegramHTMLSanitizer в ai_support_main.py) уже
    возвращает готовый, безопасный Telegram HTML (<b>, <i>, <code>,
    <a href="">) — здесь его нужно оставить как есть, а НЕ экранировать
    (раньше, когда ответ AI был обычным текстом без разметки, экранирование
    было оправдано). Единственное, что здесь ещё нужно — аккуратно найти и
    красиво оформить ГОЛЫЕ ссылки (которые модель не обернула в <a>) как
    копируемый блок <code> с иконкой 🔗 на отдельной строке. Ссылки, уже
    находящиеся внутри готового <a href="">...</a>, не трогаем.
    """
    segments = []
    last_end = 0
    for m in _TAG_RE.finditer(text):
        segments.append(('text', text[last_end:m.start()]))
        segments.append(('tag', m.group(0)))
        last_end = m.end()
    segments.append(('text', text[last_end:]))

    result_parts = []
    in_anchor = False
    for kind, chunk in segments:
        if kind == 'tag':
            if chunk.lower().startswith('<a '):
                in_anchor = True
            elif chunk.lower() == '</a>':
                in_anchor = False
            result_parts.append(chunk)
            continue
        if in_anchor:
            result_parts.append(chunk)
            continue
        piece_parts = []
        piece_last = 0
        for um in _URL_RE.finditer(chunk):
            prefix = chunk[piece_last:um.start()].rstrip(' ')
            piece_parts.append(prefix)
            raw_url = um.group(0)
            trailing = ''
            while raw_url and raw_url[-1] in '.,;:!?)':
                trailing = raw_url[-1] + trailing
                raw_url = raw_url[:-1]
            piece_parts.append(f"\n\n🔗 <code>{raw_url}</code>{trailing}\n\n")
            piece_last = um.end()
            if piece_last < len(chunk) and chunk[piece_last] == ' ':
                piece_last += 1
        piece_parts.append(chunk[piece_last:])
        result_parts.append(''.join(piece_parts))

    result = ''.join(result_parts)
    result = re.sub(r'\n{3,}', '\n\n', result)
    return result.strip()
def _build_ai_reply_keyboard(response_id: str | None, reply_text: str = "", escalate: bool = False) -> InlineKeyboardBuilder:
    """Клавиатура под ответом AI: стандартные кнопки + фидбек 👍/👎,
    если у ответа есть response_id (т.е. это настоящий ответ AI, а не
    служебное сообщение вроде отказа незарегистрированному пользователю).
    Если AI сам посоветовал перейти в «Мои ключи» (и не эскалировал диалог) —
    добавляет настоящую кнопку перехода, чтобы пользователь не печатал текст
    вручную и не зацикливался в режиме диалога с AI."""
    builder = InlineKeyboardBuilder()
    if response_id:
        builder.row(
            InlineKeyboardButton(text="👍", callback_data=f"ai_fb:up:{response_id}"),
            InlineKeyboardButton(text="👎", callback_data=f"ai_fb:down:{response_id}"),
        )
    if not escalate and "мои ключи" in reply_text.lower():
        builder.row(InlineKeyboardButton(text="🔑 Мои ключи", callback_data="my_keys"))
    builder.row(
        InlineKeyboardButton(text="❓ Ещё", callback_data="ai_ask_more"),
        InlineKeyboardButton(text="🏠 Меню", callback_data="back_to_menu")
    )
    return builder


async def _escalate_to_admins(message: Message, user_id: int, question: str, reply_text: str) -> None:
    """Создаёт тред поддержки и уведомляет админов, когда AI просит эскалацию."""
    try:
        from database.requests import get_or_create_user, create_support_thread, record_support_message
        from bot.services.support import send_ai_escalation_to_admins

        user, _ = get_or_create_user(
            user_id,
            message.from_user.username,
            first_name=getattr(message.from_user, "first_name", None),
            last_name=getattr(message.from_user, "last_name", None),
        )
        thread = create_support_thread(user_id, initiator_type="user")
        if not thread:
            logger.warning(f"Не удалось создать support-тред для эскалации AI (user {user_id})")
            return

        record_support_message(
            thread["id"],
            sender_type="user",
            sender_telegram_id=user_id,
            recipient_telegram_id=thread.get("assigned_admin_id"),
            text_html=escape_html(question),
            media_type="text",
            media_file_id=None,
            source_chat_id=message.chat.id,
            source_message_id=message.message_id,
        )

        result = await send_ai_escalation_to_admins(
            message.bot,
            thread=thread,
            user=user,
            question=question,
            ai_reply=reply_text,
        )
        if result.get("sent", 0) > 0:
            await message.answer("📨 Я передал ваш вопрос администратору — он ответит здесь в этом чате.")
    except Exception as e:
        logger.error(f"Не удалось эскалировать AI-диалог админам: {e}")


class AIChatStates(StatesGroup):
    waiting_for_question = State()


@router.message(Command("ai"))
async def ai_command(message: Message, command: CommandObject, state: FSMContext):
    """Handle /ai command"""
    user_id = message.from_user.id
    
    if command.args:
        question = command.args
        await message.answer("🤔 Обрабатываю...")
        
        reply_text, escalate, response_id = await get_ai_response(user_id, question)
        
        builder = _build_ai_reply_keyboard(response_id, reply_text, escalate)
        
        await message.answer(
            f"<b>💬 AI:</b>\n\n{_format_ai_reply_html(reply_text)}",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )

        if escalate:
            await _escalate_to_admins(message, user_id, question, reply_text)
    else:
        await state.set_state(AIChatStates.waiting_for_question)
        await message.answer("🤖 <b>AI Помощник</b>\n\nЗадайте вопрос:", parse_mode="HTML")


def _not_other_command(message: Message) -> bool:
    """Пропускает обычный текст и /cancel в AI-обработчик, но НЕ перехватывает
    другие команды (/start, /ai и т.п.) — иначе, если пользователь застрял в
    состоянии ожидания вопроса, любая команда молча уходила бы в AI как вопрос
    вместо своего настоящего обработчика."""
    text = message.text or ""
    if not text.startswith("/"):
        return True
    first_word = text.split()[0].lower().split("@")[0]  # /cancel@botname -> /cancel
    return first_word == "/cancel"


@router.message(AIChatStates.waiting_for_question, F.photo)
async def handle_ai_screenshot(message: Message, state: FSMContext):
    """Обработка скриншота ошибки в AI-диалоге — анализирует через
    vision-модель (единственную в цепочке, умеющую работать с картинками)."""
    user_id = message.from_user.id
    caption = message.caption or ""

    processing = await message.answer("🔍 Анализирую скриншот...")

    try:
        photo = message.photo[-1]  # самое большое доступное разрешение
        file = await message.bot.get_file(photo.file_id)

        # Защита от неожиданно огромных файлов
        if file.file_size and file.file_size > 8 * 1024 * 1024:
            await processing.edit_text("⚠️ Файл слишком большой. Пришлите скриншот поменьше или опишите проблему текстом.")
            return

        file_io = await message.bot.download_file(file.file_path)
        image_b64 = base64.b64encode(file_io.read()).decode("ascii")
        image_data_url = f"data:image/jpeg;base64,{image_b64}"

        reply_text, escalate, response_id = await get_ai_response(user_id, caption, image_base64=image_data_url)

        builder = _build_ai_reply_keyboard(response_id, reply_text, escalate)

        await processing.edit_text(
            f"<b>💬 AI:</b>\n\n{_format_ai_reply_html(reply_text)}",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )

        if escalate:
            await _escalate_to_admins(message, user_id, caption or "[прислал скриншот]", reply_text)
    except Exception as e:
        logger.error(f"AI screenshot error: {e}")
        await processing.edit_text("⚠️ Не удалось обработать скриншот. Попробуйте ещё раз или опишите проблему текстом.")


@router.message(AIChatStates.waiting_for_question, _not_other_command)
async def handle_ai_question(message: Message, state: FSMContext):
    """Handle questions in AI chat"""
    user_id = message.from_user.id
    question = message.text

    if question is None:
        await message.answer("🤔 Я понимаю только текст или скриншот. Опишите вопрос словами или пришлите фото ошибки.")
        return

    if question == "/cancel":
        await state.clear()
        await message.answer("❌ Вышли")
        return
    
    processing = await message.answer("🤔 Обрабатываю...")
    
    try:
        reply_text, escalate, response_id = await get_ai_response(user_id, question)
        
        builder = _build_ai_reply_keyboard(response_id, reply_text, escalate)
        
        await processing.edit_text(
            f"<b>💬 AI:</b>\n\n{_format_ai_reply_html(reply_text)}",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )

        if escalate:
            await _escalate_to_admins(message, user_id, question, reply_text)
    except Exception as e:
        logger.error(f"AI error: {e}")
        await processing.edit_text("⚠️ Ошибка. Попробуйте позже.")


@router.callback_query(F.data.startswith("ai_fb:"))
async def ai_feedback_handler(query: CallbackQuery):
    """Обработка фидбека 👍/👎 под ответом AI."""
    try:
        _, rating_raw, response_id = query.data.split(":", 2)
    except ValueError:
        await query.answer()
        return

    rating = "up" if rating_raw == "up" else "down"
    ok = await send_ai_feedback(response_id, rating)

    await query.answer("Спасибо за отзыв!" if ok else "Не удалось сохранить отзыв, но спасибо!")

    # Убираем кнопки фидбека из клавиатуры, чтобы нельзя было проголосовать повторно
    if query.message and query.message.reply_markup:
        new_builder = InlineKeyboardBuilder()
        for row in query.message.reply_markup.inline_keyboard:
            kept = [btn for btn in row if not (btn.callback_data or "").startswith("ai_fb:")]
            if kept:
                new_builder.row(*kept)
        try:
            await query.message.edit_reply_markup(reply_markup=new_builder.as_markup())
        except Exception as e:
            logger.warning(f"Не удалось обновить клавиатуру после фидбека: {e}")


@router.callback_query(F.data == "ai_support_open")
async def ai_support_open_handler(query: CallbackQuery, state: FSMContext):
    """Открыть AI-помощника по кнопке главного меню."""
    await state.set_state(AIChatStates.waiting_for_question)
    await query.answer()
    await query.message.answer(
        "🤖 <b>AI Помощник</b>\n\nЗадайте вопрос — я вижу вашу подписку, ключи и историю платежей, отвечу конкретно по вашей ситуации.",
        parse_mode="HTML"
    )


@router.callback_query(F.data == "ai_ask_more")
async def ai_ask_more(query: CallbackQuery, state: FSMContext):
    """Ask more questions"""
    await state.set_state(AIChatStates.waiting_for_question)
    await query.answer()
    await query.message.answer("💬 Следующий вопрос:")


@router.callback_query(F.data == "back_to_menu")
async def back_to_menu_from_ai(query: CallbackQuery, state: FSMContext):
    """Возврат в главное меню — тот же рендер, что и у /start."""
    from bot.handlers.user.start import _render_main_page

    await state.clear()
    await query.answer()
    await _render_main_page(query)


def _format_ai_stats(data: dict) -> str:
    """Форматирует статистику AI в читаемый текст для админа."""
    def _block(title: str, w: dict) -> str:
        return (
            f"<b>{title}</b>\n"
            f"👥 Уникальных пользователей: {w['unique_users']}\n"
            f"💬 Всего ответов: {w['total_responses']}\n"
            f"🆘 Эскалаций: {w['escalations']} ({w['escalation_rate_percent']}%)\n"
            f"👍 {w['feedback_up']}  👎 {w['feedback_down']}"
        )
    return (
        "📊 <b>Статистика AI-консультанта</b>\n\n"
        + _block("За последние 24 часа", data["last_24h"])
        + "\n\n"
        + _block("За всё время", data["all_time"])
    )


@router.message(Command("ai_stats"))
async def ai_stats_command(message: Message):
    """Статистика работы AI-консультанта — только для админов."""
    if not is_admin(message.from_user.id):
        return

    data = await get_ai_stats()
    if data is None:
        await message.answer("⚠️ Не удалось получить статистику AI. Проверьте, что AI-сервис запущен.")
        return

    await message.answer(_format_ai_stats(data), parse_mode="HTML")


