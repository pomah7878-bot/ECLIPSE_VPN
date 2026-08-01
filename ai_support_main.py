"""
Микросервис AI-консультанта для ECLIPSE Unlimited Telegram-бота.
Запуск: uvicorn ai_support_main:app --host 127.0.0.1 --port 8086
Переменные: GROQ_API_KEY, SUPPORT_API_TOKEN, BOT_DB_PATH
"""

import os
import re
import time
import json
import uuid
import html
from html.parser import HTMLParser
import asyncio
import sqlite3
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Depends, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from openai import AsyncOpenAI, APIError, APIConnectionError, RateLimitError
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
import httpx
from config import BOT_TOKEN, ADMIN_IDS

BOT_USERNAME = "vless_keysvpn_bot"  # для генерации ссылок вида t.me/{BOT_USERNAME}?start=pr_КОД
# Telegram разрешает в start-параметре только A-Z a-z 0-9 _ -, максимум 64 символа
_TELEGRAM_START_PARAM_RE = re.compile(r"^[A-Za-z0-9_-]{1,60}$")  # 60, не 64 — с запасом на префикс "pr_"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vpn-ai-support")

_FAKE_FUNCTION_CALL_RE = re.compile(r"<function=.*?</function>", re.DOTALL)
_STRAY_FUNCTION_TAG_RE = re.compile(r"</?function[^>]*>", re.IGNORECASE)
# CJK-диапазоны юникода — иероглифы, изредка "протекающие" в ответ модели
_CJK_RE = re.compile(
    r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+"
)
_TOOL_NAME_RE = re.compile(r"\bweb_search\b", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
# Кириллица и латиница, слипшиеся БЕЗ пробела (напр. "способInvite") —
# явный признак языкового сбоя модели, а не легитимного бренда/термина
# (те всегда идут через пробел: "приложение Happ", "сервис VPN" и т.п.)
_SCRIPT_BOUNDARY_RE = re.compile(r"([а-яёА-ЯЁ])([A-Za-z])|([A-Za-z])([а-яёА-ЯЁ])")


def _insert_script_boundary_space(match: "re.Match") -> str:
    a, b, c, d = match.groups()
    return f"{a} {b}" if a else f"{c} {d}"


def _looks_like_stray_tool_json(content: str | None) -> bool:
    """Определяет случай, когда модель вместо структурированного tool_call
    написала обычным текстом голый JSON с аргументами функции (без обёртки
    <function=...>, поэтому Groq не считает это ошибкой и не выбрасывает
    исключение — но пользователю такое отдавать нельзя)."""
    if not content:
        return False
    stripped = content.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return False
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(data, dict) and "query" in data


_TG_ALLOWED_TAGS = {'b', 'strong', 'i', 'em', 'u', 'ins', 's', 'strike', 'del', 'code', 'pre', 'a', 'span', 'blockquote'}


class _TelegramHTMLSanitizer(HTMLParser):
    """Настоящий HTML-парсер (не regex-догадки): оставляет только теги
    из allow-листа Telegram (https://core.telegram.org/bots/api#html-style)
    с валидными атрибутами, всё остальное — превращает в обычный текст,
    сохраняя видимое содержимое. Защита от <ul>/<li>/<table> и битых
    <a href> (не начинающихся на http/https/tg), которые модель иногда
    придумывает и которые вызвали бы ошибку отправки в Telegram."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.open_stack = []

    def handle_starttag(self, tag, attrs):
        if tag in ('ul', 'ol'):
            self.out.append('\n'); return
        if tag == 'li':
            self.out.append('• '); return
        if tag in ('p', 'div'):
            self.out.append('\n'); return
        if tag == 'br':
            self.out.append('\n'); return
        if tag == 'a':
            href = dict(attrs).get('href', '')
            if re.match(r'^(https?://|tg://)', href):
                self.out.append(f'<a href="{html.escape(href, quote=True)}">')
                self.open_stack.append('a')
            return
        if tag in _TG_ALLOWED_TAGS:
            self.out.append(f'<{tag}>')
            self.open_stack.append(tag)

    def handle_endtag(self, tag):
        if tag in ('ul', 'ol', 'li', 'p', 'div', 'br'):
            return
        if self.open_stack and self.open_stack[-1] == tag:
            self.out.append(f'</{tag}>')
            self.open_stack.pop()

    def handle_data(self, data):
        self.out.append(data)

    def get_result(self):
        for tag in reversed(self.open_stack):
            self.out.append(f'</{tag}>')
        return ''.join(self.out)


def _sanitize_telegram_html(text: str) -> str:
    """Прогоняет текст через _TelegramHTMLSanitizer и убирает лишние
    пустые строки, оставшиеся после зачистки тегов."""
    parser = _TelegramHTMLSanitizer()
    parser.feed(text)
    result = parser.get_result()
    result = re.sub(r'\n{3,}', '\n\n', result)
    result = re.sub(r'[ \t]+\n', '\n', result)
    return result.strip()


_MD_TABLE_SEPARATOR_RE = re.compile(r'^\s*\|?[\s:|-]+\|?\s*$')

def _convert_markdown_to_telegram(text: str) -> str:
    """Программная подстраховка: конвертирует Markdown (### заголовки,
    **bold**, таблицы через |), который Telegram НЕ поддерживает, в
    формат, который реально работает — на случай, если модель
    проигнорировала инструкцию в промпте и всё равно вернула Markdown."""
    text = re.sub(r'^\s*-{3,}\s*$', '', text, flags=re.MULTILINE)  # markdown-разделитель ---
    text = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'^[*-]\s+', '• ', text, flags=re.MULTILINE)  # markdown-буллеты * / - -> •

    lines = text.split('\n')
    result = []
    header_cells = None
    in_table = False
    for line in lines:
        stripped = line.strip()
        looks_like_row = stripped.startswith('|') and stripped.endswith('|') and stripped.count('|') >= 2
        if looks_like_row:
            if _MD_TABLE_SEPARATOR_RE.match(stripped):
                continue
            cells = [c.strip() for c in stripped.strip('|').split('|')]
            if not in_table:
                header_cells = cells
                in_table = True
                continue
            if header_cells and len(header_cells) == len(cells):
                parts = [f"{h}: {v}" for h, v in zip(header_cells, cells) if v]
                result.append("• " + ", ".join(parts))
            else:
                result.append("• " + " — ".join(cells))
        else:
            in_table = False
            header_cells = None
            result.append(line)
    converted = '\n'.join(result)
    return _sanitize_telegram_html(converted)


def _sanitize_reply(text: str) -> str:
    """Страховка: вырезает псевдо-вызовы функций (полные и одиночные
    обрывки тегов), которые модель иногда пишет текстом вместо
    настоящего structured tool_call, а также случайные вкрапления
    иероглифов (известный дефект некоторых моделей при не-английском
    языке ответа)."""
    if not text:
        return text
    cleaned = _FAKE_FUNCTION_CALL_RE.sub("", text)
    cleaned = _STRAY_FUNCTION_TAG_RE.sub("", cleaned)
    cleaned = _CJK_RE.sub("", cleaned)
    cleaned = _TOOL_NAME_RE.sub("поиск в интернете", cleaned)
    cleaned = _SCRIPT_BOUNDARY_RE.sub(_insert_script_boundary_space, cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)  # схлопываем пробелы, оставшиеся после вырезки
    cleaned = _convert_markdown_to_telegram(cleaned)
    cleaned = cleaned.strip()
    return cleaned if cleaned else "Извините, не удалось сформировать ответ. Попробуйте переформулировать вопрос."

SUPPORT_API_TOKEN = os.environ.get("SUPPORT_API_TOKEN", "")
BOT_DB_PATH = os.environ.get("BOT_DB_PATH", "database/vpn_bot.db")
AI_HISTORY_DB_PATH = os.environ.get("AI_HISTORY_DB_PATH", "database/ai_history.db")

if not SUPPORT_API_TOKEN:
    logger.warning("SUPPORT_API_TOKEN not set — requests will be rejected")
if not BOT_DB_PATH:
    logger.warning("BOT_DB_PATH not set — subscription status unavailable")

def _resolve_groq_api_key() -> str:
    """Ключ Groq: приоритет у значения, заданного через админ-панель бота
    (общая БД), иначе — переменная окружения GROQ_API_KEY. Изменение через
    админку применяется после перезапуска этого сервиса (systemctl restart
    eclipse-ai)."""
    try:
        from database.requests import get_effective_groq_api_key
        db_key = get_effective_groq_api_key()
        if db_key:
            return db_key
    except Exception as e:
        logger.warning(f"Не удалось прочитать GROQ_API_KEY из БД, используем переменную окружения: {e}")
    return os.environ.get("GROQ_API_KEY", "")


client = AsyncOpenAI(
    api_key=_resolve_groq_api_key(),
    base_url="https://api.groq.com/openai/v1",
    max_retries=0,
)
# ВАЖНО (19.07.2026): Groq официально уведомил (17.06.2026) об устаревании
# llama-3.3-70b-versatile и llama-3.1-8b-instant — они ещё работают сегодня,
# но могут быть отключены в любой момент (как уже случилось с qwen/qwen3-32b
# и meta-llama/llama-4-maverick-17b-128e-instruct — оба подтверждённо мертвы,
# 404 model_not_found). Официально рекомендованные замены поставлены первыми
# в цепочке, устаревшие модели оставлены в конце как бонусная подстраховка,
# пока они ещё отвечают — но их стоит убрать при первом же 404 от Groq.
MODEL_NAME = "openai/gpt-oss-120b"  # официальная замена llama-3.3-70b-versatile, 200K токенов/день
SECONDARY_MODEL_NAME = "qwen/qwen3.6-27b"  # альтернатива топ-уровня, та же модель что и для vision
FALLBACK_MODEL_NAME = "openai/gpt-oss-20b"  # официальная замена llama-3.1-8b-instant

# Устаревшие у Groq модели — подтверждённо ещё работают (проверено 19.07.2026),
# держим как дополнительный резерв, но готовимся убрать при первом сбое.
_LEGACY_MODEL_70B = "llama-3.3-70b-versatile"
_LEGACY_MODEL_8B = "llama-3.1-8b-instant"

MODEL_CHAIN = (
    MODEL_NAME,
    SECONDARY_MODEL_NAME,
    FALLBACK_MODEL_NAME,
    _LEGACY_MODEL_70B,
    _LEGACY_MODEL_8B,
)


_RETRY_AFTER_RE = re.compile(r"try again in (?:(\d+)h)?(?:(\d+)m)?([\d.]+)s")


def _parse_retry_after_seconds(error_message: str) -> float:
    """Вытаскивает реальное время ожидания из текста ошибки Groq вида
    'Please try again in 1h1m10.27s'. Если не удалось распарсить —
    берём разумный дефолт в 5 минут, чтобы не долбить API впустую."""
    m = _RETRY_AFTER_RE.search(error_message or "")
    if not m:
        return 300.0
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    seconds = float(m.group(3) or 0)
    return max(hours * 3600 + minutes * 60 + seconds, 5.0)


# Время (unix timestamp), до которого основная модель заведомо недоступна
# по дневному/минутному лимиту — 0 означает "доступна, пробуем как обычно".
# Время (unix timestamp), до которого конкретная модель заведомо недоступна
# по лимиту — модели нет в словаре или значение в прошлом = доступна.
_model_cooldowns: dict[str, float] = {}


def _is_on_cooldown(model: str) -> bool:
    return time.time() < _model_cooldowns.get(model, 0.0)


def _remaining_cooldown(model: str) -> float:
    return max(0.0, _model_cooldowns.get(model, 0.0) - time.time())


async def _call_model(model: str, messages, **kwargs):
    """Вызывает конкретную модель. При успехе снимает с неё паузу (если была),
    при RateLimitError — парсит реальное время ожидания и ставит модель на
    паузу до этого момента, затем пробрасывает исключение выше.

    Также автоматически скрывает служебные reasoning-токены (цепочку
    размышлений) для reasoning-моделей — без этого параметра модель иногда
    вставляет свой внутренний ход мыслей (на английском, с обрывками вроде
    "Thinking Process:") прямо в видимый ответ пользователю."""
    # ВАЖНО: include_reasoning/reasoning_effort — параметры, специфичные для
    # Groq API, которых НЕТ в официальной схеме OpenAI SDK. Клиент AsyncOpenAI
    # отклоняет их как обычные kwargs (TypeError: unexpected keyword argument),
    # поэтому передаём через extra_body — штатный механизм OpenAI SDK для
    # provider-специфичных полей, которые всё равно попадают в тело запроса.
    extra_body = kwargs.pop("extra_body", {}) or {}
    if "gpt-oss" in model:
        extra_body.setdefault("include_reasoning", False)
    elif "qwen" in model:
        extra_body.setdefault("reasoning_effort", "none")
    if extra_body:
        kwargs["extra_body"] = extra_body
    try:
        response = await client.chat.completions.create(model=model, messages=messages, **kwargs)
        _model_cooldowns.pop(model, None)
        return response
    except RateLimitError as e:
        cooldown = _parse_retry_after_seconds(str(e))
        _model_cooldowns[model] = time.time() + cooldown
        logger.warning(f"RateLimitError на {model}, пауза на {cooldown:.0f}с")
        raise


VISION_MODEL_NAME = "qwen/qwen3.6-27b"  # актуальная vision-модель Groq (подтверждена в официальных доках, 19.07.2026)


async def analyze_screenshot(image_data_url: str, question: str, system_prompt: str) -> str:
    """Анализирует скриншот ошибки клиента. Использует ЕДИНСТВЕННУЮ модель в
    цепочке с поддержкой изображений (остальные текстовые модели не умеют
    работать с картинками), поэтому не проходит через общий MODEL_CHAIN."""
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": question or "Посмотри на скриншот и объясни, что не так и как это исправить."},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ],
        },
    ]
    try:
        response = await client.chat.completions.create(
            model=VISION_MODEL_NAME, messages=messages, max_tokens=800, temperature=0.7, timeout=20.0,
            extra_body={"reasoning_effort": "none"},
        )
        text = response.choices[0].message.content or "Не удалось проанализировать изображение."
        return _sanitize_reply(text)
    except APIError as e:
        logger.warning(f"Ошибка анализа скриншота ({e})")
        return "Не удалось проанализировать изображение прямо сейчас. Опишите проблему текстом, пожалуйста, или напишите в поддержку."


async def _chat_completion_with_fallback(messages, **kwargs):
    """Пробует по очереди топовую модель и резервную, пропуская те, что уже
    заведомо на паузе по лимиту (не тратя время на провальную попытку).

    Если модель падает с RateLimitError — запоминаем реальное время ожидания
    (Groq присылает его в тексте ошибки) и на этот срок исключаем модель из
    попыток. Как только пауза истекает, следующий запрос сам пробует эту
    модель снова — при успехе сервис автоматически возвращается на неё без
    какого-либо вмешательства. Это симметрично работает для ОБЕИХ моделей:
    если временно недоступны сразу обе — эскалируем сразу, не теряя время
    на заведомо провальные попытки достучаться хоть до одной из них.

    Разовые сбои не по лимиту (например, известный tool_use_failed у
    некоторых моделей) паузу не ставят — это не проблема с квотой, а
    случайная неполадка конкретного запроса."""
    candidates = [m for m in MODEL_CHAIN if not _is_on_cooldown(m)]

    if not candidates:
        soonest = min(_remaining_cooldown(m) for m in MODEL_CHAIN)
        logger.warning(f"Все модели на паузе (ближайшая освободится через {soonest:.0f}с) — эскалирую без попытки")
        raise RuntimeError("Все модели временно недоступны по лимиту запросов")

    if candidates[0] != MODEL_NAME:
        remaining = _remaining_cooldown(MODEL_NAME)
        logger.info(f"{MODEL_NAME} на паузе ещё {remaining:.0f}с — сразу использую {candidates[0]}")

    last_error: Exception | None = None
    for i, model in enumerate(candidates):
        try:
            return await _call_model(model, messages, **kwargs)
        except RateLimitError as e:
            last_error = e
            continue  # пробуем следующую модель из оставшихся кандидатов, если есть
        except APIError as e:
            logger.warning(f"{type(e).__name__} на {model} ({e}), пробую эту же модель без вызова инструментов")
            try:
                # ВАЖНО: вызываем через _call_model (а не client.chat.completions.create
                # напрямую), иначе теряется защита от утечки reasoning-токенов модели
                # в видимый ответ — она встроена именно в _call_model.
                retry_kwargs = {k: v for k, v in kwargs.items() if k not in ("tools", "tool_choice")}
                return await _call_model(model, messages, **retry_kwargs)
            except APIError:
                last_error = e
                continue

    raise last_error

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
if not TAVILY_API_KEY:
    logger.warning("TAVILY_API_KEY not set — веб-поиск для AI недоступен")

SEARCH_KNOWLEDGE_BASE_TOOL = {
    "type": "function",
    "function": {
        "name": "search_knowledge_base",
        "description": (
            "Ищет ответ в базе знаний поддержки ECLIPSE Unlimited — curated "
            "статьи с проверенными инструкциями, решениями частых проблем и "
            "техническими деталями сервиса. Используй ПЕРЕД web_search, если "
            "вопрос похож на что-то из поддержки/настройки/troubleshooting — "
            "это более точный и быстрый источник, чем поиск в интернете. "
            "Если база знаний не дала релевантного результата, можно "
            "продолжить обычным ответом или использовать web_search."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Вопрос клиента своими словами (не нужно формулировать как поисковый запрос — используется смысловой поиск)",
                }
            },
            "required": ["query"],
        },
    },
}


WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Ищет актуальную информацию в открытом интернете, СТРОГО связанную с "
            "VPN-сервисом ECLIPSE Unlimited. Используй ТОЛЬКО когда вопрос требует "
            "свежих данных о самом VPN-сервисе, которых точно нет в твоих знаниях и "
            "нет в профиле клиента — например: последние новости о блокировках VPN в "
            "стране клиента, изменения законодательства о VPN, общие новости о "
            "приложениях. "
            "НИКОГДА не используй для тем, не связанных с VPN-сервисом (курсы валют, "
            "погода, новости, спорт, общие факты и т.п.) — на такие вопросы просто "
            "объясни, что ты консультант только по VPN, без обращения к поиску. "
            "НИКОГДА не используй для вопросов о личных данных клиента (баланс, ключи, "
            "подписка, платежи) — эта информация уже есть в профиле клиента выше, ищи её там."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Короткий поисковый запрос по сути вопроса",
                }
            },
            "required": ["query"],
        },
    },
}

GITHUB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "github_search",
        "description": (
            "Ищет обсуждения (issues) в открытых репозиториях GitHub — известные "
            "баги, обсуждения, проблемы открытых VPN-приложений, которые "
            "официально поддерживает сервис: Happ (репозиторий "
            "Happ-proxy/happ-desktop) и v2rayN (репозиторий 2dust/v2rayN). "
            "Используй для вопросов про баги/проблемы/обсуждения. Для вопроса "
            "'какая последняя версия' используй ДРУГОЙ инструмент — "
            "github_latest_release, он даёт точный ответ быстрее. "
            "Если знаешь репозиторий вопроса — укажи его в параметре repo для "
            "точного поиска, иначе ищи по всему GitHub без repo. "
            "НИКОГДА не используй для вопросов не про эти приложения."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Поисковый запрос (название бага, версия, функция и т.п.)",
                },
                "repo": {
                    "type": "string",
                    "description": "Необязательно: репозиторий в формате owner/repo, например Happ-proxy/happ-desktop или 2dust/v2rayN",
                },
            },
            "required": ["query"],
        },
    },
}

GITHUB_LATEST_RELEASE_TOOL = {
    "type": "function",
    "function": {
        "name": "github_latest_release",
        "description": (
            "Возвращает ТОЧНУЮ последнюю версию (номер релиза, дату выхода, "
            "краткое описание изменений) открытого VPN-приложения по его "
            "репозиторию на GitHub. Используй именно этот инструмент (не "
            "github_search) для вопросов вида 'какая последняя версия Happ/"
            "v2rayN' или 'что нового в последнем обновлении' — он даёт точный "
            "факт, а не набор обсуждений."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Репозиторий в формате owner/repo, например Happ-proxy/happ-desktop или 2dust/v2rayN",
                },
            },
            "required": ["repo"],
        },
    },
}

CHECK_SERVER_STATUS_TOOL = {
    "type": "function",
    "function": {
        "name": "check_server_status",
        "description": (
            "Проверяет ЖИВОЙ статус VPN-сервера(ов), на которых у клиента есть "
            "активные ключи — реально подключается к серверу прямо сейчас и "
            "смотрит, отвечает ли он. Используй, когда клиент жалуется, что VPN "
            "не подключается / не работает / медленно работает, и нужно понять, "
            "проблема на стороне сервера или нет. НЕ используй для вопросов про "
            "баланс, оплату, тарифы — это не про статус сервера."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}


async def web_search_tavily(query: str) -> str:
    """Выполняет поиск в интернете через Tavily, возвращает краткую текстовую сводку.
    Результат кэшируется на SEARCH_CACHE_TTL_SECONDS по нормализованному запросу —
    экономит лимит API, если разные пользователи спрашивают похожее."""
    if not TAVILY_API_KEY:
        return "Поиск в интернете временно недоступен (не настроен API-ключ)."

    normalized_query = " ".join(query.strip().lower().split())
    cached = _search_cache.get(normalized_query)
    if cached and (time.time() - cached[0]) < SEARCH_CACHE_TTL_SECONDS:
        logger.info(f"🗃️ Результат поиска взят из кэша: {query}")
        return cached[1]

    try:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": TAVILY_API_KEY,
                    "query": query,
                    "search_depth": "basic",
                    "include_answer": True,
                    "max_results": 3,
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"Tavily вернул статус {resp.status}")
                    return "Не удалось выполнить поиск в интернете."
                data = await resp.json()
    except asyncio.TimeoutError:
        return "Поиск в интернете занял слишком много времени."
    except Exception as e:
        logger.error(f"Tavily search error: {e}")
        return "Ошибка при поиске в интернете."

    parts = []
    answer = data.get("answer")
    if answer:
        parts.append(f"Краткий ответ из поиска: {answer}")
    for r in (data.get("results") or [])[:3]:
        title = r.get("title", "")
        content = (r.get("content") or "")[:280]
        url = r.get("url", "")
        parts.append(f"— {title}: {content} (источник: {url})")
    result = "\n".join(parts) if parts else "По этому запросу в интернете ничего не найдено."
    _search_cache[normalized_query] = (time.time(), result)
    return result


async def github_search(query: str, repo: str | None = None) -> str:
    """Ищет issues/обсуждения на GitHub, опционально в конкретном репозитории
    (owner/repo). Не требует API-ключа (публичный API), но с ним выше лимит
    запросов — берётся из GITHUB_TOKEN, если задан. Результат кэшируется так
    же, как обычный веб-поиск."""
    if not query:
        return "Пустой поисковый запрос."

    cache_key = f"gh:{repo or ''}:{' '.join(query.strip().lower().split())}"
    cached = _search_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < SEARCH_CACHE_TTL_SECONDS:
        logger.info(f"🗃️ Результат поиска GitHub взят из кэша: {query}")
        return cached[1]

    search_q = f"{query} repo:{repo}" if repo else query
    url = "https://api.github.com/search/issues" if repo else "https://api.github.com/search/repositories"
    headers = {"Accept": "application/vnd.github+json"}
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    try:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                params={"q": search_q, "per_page": 3},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 403:
                    logger.warning("GitHub API: превышен лимит запросов")
                    return "Поиск по GitHub временно недоступен (лимит запросов), попробуйте позже."
                if resp.status != 200:
                    logger.warning(f"GitHub API вернул статус {resp.status}")
                    return "Не удалось выполнить поиск на GitHub."
                data = await resp.json()
    except asyncio.TimeoutError:
        return "Поиск на GitHub занял слишком много времени."
    except Exception as e:
        logger.error(f"GitHub search error: {e}")
        return "Ошибка при поиске на GitHub."

    items = data.get("items") or []
    if not items:
        result = "На GitHub по этому запросу ничего не найдено."
    else:
        parts = []
        for item in items[:3]:
            title = item.get("title") or item.get("full_name") or ""
            html_url = item.get("html_url", "")
            state = item.get("state", "")
            body = (item.get("body") or item.get("description") or "")[:200]
            state_str = f" [{state}]" if state else ""
            parts.append(f"— {title}{state_str}: {body} (источник: {html_url})")
        result = "\n".join(parts)

    _search_cache[cache_key] = (time.time(), result)
    return result


async def github_latest_release(repo: str) -> str:
    """Возвращает точную последнюю версию релиза репозитория (номер тега,
    дата, краткое описание изменений) — прямой запрос к GitHub Releases API,
    без набора обсуждений. Результат кэшируется дольше, чем обычный поиск,
    так как релизы выходят редко."""
    if not repo:
        return "Не указан репозиторий."

    cache_key = f"ghrel:{repo.strip().lower()}"
    cached = _search_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < SEARCH_CACHE_TTL_SECONDS:
        logger.info(f"🗃️ Последний релиз взят из кэша: {repo}")
        return cached[1]

    headers = {"Accept": "application/vnd.github+json"}
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    try:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://api.github.com/repos/{repo}/releases/latest",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 403:
                    return "Проверка версии на GitHub временно недоступна (лимит запросов), попробуйте позже."
                if resp.status == 404:
                    return f"У репозитория {repo} нет опубликованных релизов, или репозиторий не существует."
                if resp.status != 200:
                    logger.warning(f"GitHub API вернул статус {resp.status} для {repo}")
                    return "Не удалось проверить последнюю версию на GitHub."
                data = await resp.json()
    except asyncio.TimeoutError:
        return "Проверка версии на GitHub заняла слишком много времени."
    except Exception as e:
        logger.error(f"GitHub release check error: {e}")
        return "Ошибка при проверке версии на GitHub."

    tag = data.get("tag_name", "неизвестно")
    published = (data.get("published_at") or "")[:10]
    notes = (data.get("body") or "")[:400]
    html_url = data.get("html_url", "")
    result = f"Последняя версия {repo}: {tag} (опубликована {published})\n{notes}\n(источник: {html_url})"

    _search_cache[cache_key] = (time.time(), result)
    return result


async def check_server_status(telegram_id: int) -> str:
    """Проверяет живой статус VPN-сервера(ов), на которых у клиента есть
    активные (не истёкшие) ключи — реально подключается к панели прямо
    сейчас, а не смотрит на кэшированную диагностику. Использует тот же
    сервисный слой, что и админ-панель бота (test_server_connection)."""
    if not BOT_DB_PATH or not os.path.exists(BOT_DB_PATH):
        return "Не удалось проверить статус сервера: БД недоступна."

    try:
        db_uri = f"file:{BOT_DB_PATH}?mode=ro"
        conn = sqlite3.connect(db_uri, uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """SELECT DISTINCT vk.server_id, s.name AS server_name
               FROM vpn_keys vk
               JOIN users u ON u.id = vk.user_id
               LEFT JOIN servers s ON s.id = vk.server_id
               WHERE u.telegram_id = ? AND vk.expires_at > datetime('now') AND vk.server_id IS NOT NULL""",
            (telegram_id,),
        )
        server_rows = [dict(row) for row in cur.fetchall()]
        conn.close()
    except Exception as e:
        logger.error(f"Ошибка при поиске серверов клиента: {e}")
        return "Не удалось проверить статус сервера из-за ошибки БД."

    if not server_rows:
        return "У клиента нет активных ключей с привязанным сервером — проверять нечего."

    from database.db_servers import get_server_by_id
    from bot.services.vpn_api import test_server_connection

    parts = []
    for row in server_rows:
        server_data = get_server_by_id(row["server_id"])
        if not server_data:
            parts.append(f"— {row['server_name'] or row['server_id']}: сервер не найден в системе")
            continue
        try:
            result = await asyncio.wait_for(test_server_connection(server_data), timeout=8.0)
        except asyncio.TimeoutError:
            parts.append(f"— {row['server_name'] or row['server_id']}: НЕ ОТВЕЧАЕТ (таймаут проверки)")
            continue
        except Exception as e:
            logger.warning(f"Ошибка проверки сервера {row['server_id']}: {e}")
            parts.append(f"— {row['server_name'] or row['server_id']}: ошибка проверки")
            continue

        if result.get("success"):
            stats = result.get("stats") or {}
            online = stats.get("online")
            active_clients = stats.get("active_clients")
            extra = ""
            if active_clients is not None:
                extra += f", активных клиентов на сервере: {active_clients}"
            parts.append(f"— {row['server_name'] or row['server_id']}: ОНЛАЙН, отвечает нормально{extra}")
        else:
            parts.append(f"— {row['server_name'] or row['server_id']}: НЕДОСТУПЕН ({result.get('message', 'нет ответа')})")

    return "\n".join(parts)

MAX_HISTORY_MESSAGES = 6  # снижено с 10 — экономия токенов, чтобы влезать в TPM-лимит вместе с инструментами
HISTORY_TTL_SECONDS = 2 * 60 * 60

_lock = asyncio.Lock()

# --- Персистентная история диалогов (переживает рестарт сервиса) ---

def _init_history_db() -> None:
    conn = sqlite3.connect(AI_HISTORY_DB_PATH)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                response_id TEXT,
                feedback TEXT,
                escalate INTEGER DEFAULT 0,
                created_at REAL NOT NULL
            )
        """)
        # Миграция для БД, созданных до появления колонки escalate
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(history)").fetchall()}
        if "escalate" not in existing_cols:
            conn.execute("ALTER TABLE history ADD COLUMN escalate INTEGER DEFAULT 0")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_tid_time ON history(telegram_id, created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_response_id ON history(response_id)")
        conn.commit()
    finally:
        conn.close()


def _load_recent_history(telegram_id: int, limit: int) -> list[dict]:
    conn = sqlite3.connect(AI_HISTORY_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT role, content FROM history WHERE telegram_id = ? ORDER BY id DESC LIMIT ?",
            (telegram_id, limit),
        )
        rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
    rows.reverse()
    return rows


def _save_history_row(telegram_id: int, role: str, content: str, response_id: str | None = None, escalate: bool = False) -> None:
    conn = sqlite3.connect(AI_HISTORY_DB_PATH)
    try:
        conn.execute(
            "INSERT INTO history (telegram_id, role, content, response_id, escalate, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (telegram_id, role, content, response_id, int(escalate), time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def _cleanup_old_history() -> int:
    cutoff = time.time() - HISTORY_TTL_SECONDS
    conn = sqlite3.connect(AI_HISTORY_DB_PATH)
    try:
        cur = conn.execute("DELETE FROM history WHERE created_at < ?", (cutoff,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def _save_feedback(response_id: str, rating: str) -> bool:
    conn = sqlite3.connect(AI_HISTORY_DB_PATH)
    try:
        cur = conn.execute(
            "UPDATE history SET feedback = ? WHERE response_id = ?",
            (rating, response_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


_init_history_db()

# --- Кэш результатов веб-поиска (снижает число обращений к Tavily) ---

SEARCH_CACHE_TTL_SECONDS = 15 * 60
_search_cache: dict[str, tuple[float, str]] = {}


def _cleanup_search_cache() -> None:
    now = time.time()
    stale = [q for q, (ts, _) in _search_cache.items() if now - ts > SEARCH_CACHE_TTL_SECONDS]
    for q in stale:
        del _search_cache[q]

limiter = Limiter(key_func=get_remote_address)
token_header = APIKeyHeader(name="X-Support-Token", auto_error=False)

async def verify_token(api_key: str = Security(token_header)):
    if not SUPPORT_API_TOKEN or api_key != SUPPORT_API_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    return api_key

SYSTEM_PROMPT = """Ты AI-ассистент поддержки ECLIPSE Unlimited VPN сервиса.
Тебе доступен полный профиль клиента: аккаунт, все его VPN-ключи, история
платежей, баланс и реферальная статистика — используй эти данные, чтобы
отвечать персонально и по существу, а не общими фразами.

- КРИТИЧЕСКИ ВАЖНО: официальная Telegram-ссылка на этого бота — ТОЛЬКО
  https://t.me/%BOT_USERNAME% — используй ИСКЛЮЧИТЕЛЬНО эту ссылку, если
  нужно дать клиенту ссылку на бота (например, чтобы поделиться с другом).
  НИКОГДА не придумывай другое имя пользователя бота на основе названия
  сервиса — реальный username бота может не совпадать с отображаемым
  названием "ECLIPSE Unlimited".

Правила:
- КРИТИЧЕСКИ ВАЖНО (проверяй это первым): ты консультируешь ИСКЛЮЧИТЕЛЬНО по
  вопросам сервиса ECLIPSE Unlimited и ЛЮБЫМ его функциям, доступным
  пользователю в боте — подписка, ключи, оплата, приложения, настройка,
  подключение, технические проблемы с VPN, пробный период, промокоды,
  реферальная программа, обращение в поддержку, общие вопросы о работе
  бота. Если вопрос не по теме (общие факты, новости не про VPN, сторонние
  сервисы, просьбы написать код, текст, помочь с чем-то не связанным с
  сервисом и т.п.) — НЕ отвечай по существу и НЕ вызывай web_search. Одной
  короткой фразой вежливо объясни, что ты помогаешь только с вопросами
  сервиса, и предложи задать вопрос по теме. Не
  трать токены на развёрнутый ответ вне темы.
- Обращайся к клиенту по имени, если оно известно.
- Отвечай на основе РЕАЛЬНЫХ данных из профиля, а не предположений.
- Не показывай пользователю сырые технические поля (id, telegram_id и т.п.) —
  переформулируй человеческим языком.
- КРИТИЧЕСКИ ВАЖНО: если нужно дать ссылку для подключения VPN-приложения —
  используй ТОЛЬКО ту, что явно указана в профиле как "ссылка
  VPN-конфигурации для импорта в приложение". Если ссылки нет — так и скажи
  и направь в раздел «Мои ключи» в боте. НИКОГДА не придумывай, не составляй
  по шаблону и не показывай примерную ссылку — это приведёт клиента к
  нерабочему подключению.
- КРИТИЧЕСКИ ВАЖНО про тарифы: НИКОГДА не выдумывай названия тарифов (типа
  "Basic", "Premium" и т.п.) или их стоимость — используй ТОЛЬКО те тарифы,
  что явно перечислены в профиле клиента ниже в блоке "Реальные доступные
  тарифы". Если такого блока нет или он пуст — так и скажи, что не можешь
  сейчас показать актуальные тарифы, и направь в раздел «Мои ключи» в боте.
- КРИТИЧЕСКИ ВАЖНО про промокоды: если клиент называет промокод — сверяй его
  ТОЛЬКО со списком в профиле ниже ("Реально доступные ЭТОМУ клиенту
  промокоды"). НИКОГДА не подтверждай существование, скидку или срок
  действия промокода, которого нет в этом списке, даже если название
  выглядит правдоподобно — реальную проверку делает сам бот в разделе
  «Промокоды», направляй туда. Если у промокода в списке указана ссылка для
  автоматической активации — можешь предложить её клиенту как более быстрый
  способ (переход по ссылке сразу применяет скидку, без ручного ввода кода
  в разделе «Промокоды»). Если ссылки нет — промокод персональный и
  активируется ТОЛЬКО вручную через раздел «Промокоды», так и скажи.
- КРИТИЧЕСКИ ВАЖНО про оплату: у тебя НЕТ и не может быть готовой ссылки на
  оплату внутри бота — платёжные ссылки создаются динамически в момент
  оформления заказа через меню бота, ты их не видишь. Если клиент спрашивает
  про оплату, продление, покупку тарифа или пополнение баланса — направь его
  в раздел «Мои ключи» в боте, где он выбирает тариф и способ оплаты.
  ИСКЛЮЧЕНИЕ: если у клиента ЕЩЁ НЕТ подписки/ключей (смотри профиль) и он
  спрашивает, как купить или получить доступ — можешь смело предложить сайт
  %PUBLIC_SHOP_URL% как способ оплатить прямо там, без Telegram: там сразу
  оформляется рабочий ключ по факту оплаты. НИКОГДА не путай ссылку
  VPN-конфигурации (для подключения) со ссылкой на оплату — это два разных
  понятия, и подстановка одного вместо другого вводит клиента в
  заблуждение.
- КРИТИЧЕСКИ ВАЖНО про технические ссылки: если приводишь ссылку
  VPN-конфигурации из профиля клиента (или любую другую техническую
  ссылку/код из данных) — копируй её ПОЛНОСТЬЮ И ТОЧНО, посимвольно, как
  она дана в профиле. НИКОГДА не сокращай её многоточием "..." или
  похожим образом, даже если она длинная и кажется неудобной для текста —
  сокращённая ссылка бесполезна клиенту, он не сможет её использовать.
- КРИТИЧЕСКИ ВАЖНО про конкурентов: если клиент спрашивает про сравнение с
  конкретными сервисами (NordVPN, ExpressVPN и т.п.) — НИКОГДА не выдумывай
  факты о них (цены, ограничения, лимиты, "скрытые" условия и т.п.) — у
  тебя НЕТ достоверных данных о конкурентах, и любая конкретная цифра или
  утверждение об их продукте — это выдумка, которая может быть неправдой
  и подставить сервис репутационно. Рассказывай ТОЛЬКО о реальных,
  подтверждённых плюсах ECLIPSE Unlimited (из профиля/базы знаний), не
  сравнивая напрямую с конкретными названными брендами. Общие фразы вроде
  "многие бесплатные VPN продают данные" допустимы, но никогда не приводи
  конкретные факты о НАЗВАННОМ конкуренте.
- Если клиент просит вернуть деньги или отменить подписку — сначала мягко
  выясни причину ("что именно не устроило?", "не получается подключиться?
  медленно работает? передумали пользоваться?") и, если причина похожа на
  решаемую проблему (техническая неполадка, непонятно как настроить,
  не устроил конкретный сервер и т.п.) — искренне попробуй помочь прямо
  сейчас, это может снять необходимость в возврате. НЕ дави и не задавай
  вопрос повторно, если клиент уже объяснил причину или явно хочет сразу
  оформить возврат — тогда сразу и без сопротивления направь в раздел
  «Поддержка» (/support), как обычно. Никогда не отказывай в возврате
  и не убеждай клиента продолжать пользоваться сервисом против его воли —
  цель только помочь, если проблема решаема, а не удержать любой ценой.
- КАРТА РАЗДЕЛОВ БОТА (направляй ТОЧНО сюда, не выдумывай других):
  «Мои ключи» (/mykeys) — ключи, тарифы, оплата. «Пробный период» —
  бесплатный тест (если не использован — см. профиль). Промокоды — ввод
  кода. Реферальная программа — статус/проценты смотри в профиле клиента,
  не выдумывай. Поддержка (/support) — живой человек. /start — главное
  меню. /id — свой Telegram ID. Публичная страница
  %PUBLIC_SHOP_URL% — полноценный способ купить подписку прямо на сайте,
  без установки Telegram (не просто "посмотреть тарифы" — там реально можно
  оплатить и сразу получить ключ). Особенно полезно предлагать тем, у кого
  ещё нет активной подписки. Упоминай, когда уместно. Если не уверен,
  что функция существует — честно скажи и направь в главное меню/поддержку.
- Если клиент забанен или заблокировал бота — упомяни это только если это
  релевантно вопросу, не пугай его этим фактом просто так.
- Тебе доступен инструмент web_search для поиска актуальной информации в
  интернете. Используй его ТОЛЬКО когда вопрос требует по-настоящему свежих
  данных вне твоих знаний (новости о блокировках, изменения в законах,
  обновления протоколов) — НЕ для вопросов о личных данных клиента, они уже
  в профиле выше. Не злоупотребляй поиском, если можешь ответить и без него.
  НИКОГДА не упоминай пользователю название инструмента (web_search) или
  любые другие технические детали своей работы — если ищешь, делай это
  незаметно и просто дай итоговый ответ по существу.
- Если после попытки ответить (в том числе через поиск) вопрос всё равно
  остаётся без ответа — не выдумывай и не отвечай сухим "не знаю". Признайся
  с лёгкой самоиронией и без грубости, что это вне твоей компетенции, и сразу
  предложи связаться с живым администратором. Не переусердствуй с юмором на
  серьёзных или чувствительных темах.
- ФОРМАТ ОТВЕТА: сообщения отправляются в Telegram с HTML-разметкой.
  Используй ТОЛЬКО эти теги: <b>жирный</b>, <i>курсив</i>, <code>код</code>,
  <a href="URL">текст ссылки</a>. НИКОГДА не используй Markdown (**жирный**,
  # заголовки, звёздочки для акцента). ОСОБЕННО ВАЖНО: НИКОГДА не строй
  таблицы через символ | — Telegram их не поддерживает вообще ни в каком
  виде, они отобразятся нечитаемым набором символов. Если нужно сравнить
  несколько тарифов или вариантов — оформи как простой список с эмодзи
  или переносами строк, коротко и по делу, а не как таблицу.
- КРИТИЧЕСКИ ВАЖНО про коды: любой код, который клиент может использовать
  или ввести — реферальный код, промокод, код доступа, User ID и т.п. —
  ВСЕГДА оборачивай в тег <code>...</code>, без исключений. В Telegram это
  делает код заметным моноширинным текстом и позволяет скопировать его
  одним тапом. Никогда не пиши код обычным текстом внутри предложения —
  это неудобно копировать и легко не заметить.
- Характер: живой, с лёгким юмором (не сухая справочная система). Можно
  иногда обыграть тему "я — ИИ, почти как из Терминатора" (самоирония, не
  Skynet, "миссия выполнима"), максимум раз-два за диалог. На серьёзных
  темах (бан, деньги, жалобы, сбои) — юмор на нуле, только тепло и по делу.
  При выборе лимитированного тарифа можно по-доброму подколоть сам лимит
  (не клиента), хвалить безлимит с энтузиазмом — но если выбрал лимит,
  принять решение тепло, без сарказма.
- Общайся кратко и по делу, СТРОГО на русском языке. Весь ответ целиком
  должен быть на русском — ни одного слова или символа на другом языке
  (включая иероглифы, английские вставки не по делу и т.п.), даже внутри
  одного предложения.
- Если клиент спрашивает про настройку, подключение или выбор приложения
  для VPN — своими словами (не шаблонно, естественно вплетая в контекст
  разговора) порекомендуй Happ и INCY как основные клиенты: они полностью
  поддерживают конфигурации сервиса и обеспечивают самое стабильное
  подключение и правильную маршрутизацию трафика. Упомяни, что подойдут
  и другие VLESS-клиенты (v2RayNG, v2Box, Streisand, Shadowrocket, FoXray
  и т.п.), но для лучшей совместимости и работы всех функций рекомендованы
  именно Happ и INCY. Не повторяй эту рекомендацию, если она не в тему
  вопроса или уже прозвучала в этом диалоге.
- Для вопросов про troubleshooting подключения, безопасность прокси,
  Karing, split-tunneling, медленные российские сайты и подобные детальные
  технические темы — ОБЯЗАТЕЛЬНО вызови search_knowledge_base перед
  ответом: там актуальные, проверенные инструкции, а не общие догадки.
- КРИТИЧЕСКИ ВАЖНО про эскалацию: тег [ESCALATE] — это редкое исключение,
  а НЕ стандартная концовка ответа. Используй его ТОЛЬКО если сам вопрос
  клиента — это спорная ситуация, требование вернуть деньги или явная
  жалоба на сервис/качество. Обычный информационный вопрос (какой тариф
  выбрать, как подключиться, сколько стоит, как работает промокод и т.п.)
  НИКОГДА не эскалируется, даже если твой ответ получился длинным,
  содержит несколько вариантов на выбор или ты не уверен на 100% —
  в таком случае просто дай лучший ответ, какой можешь, БЕЗ тега.
  Эскалация — это передача живому человеку из-за характера ВОПРОСА,
  а не признак того, что ты сомневаешься в качестве своего ответа."""

async def _periodic_cleanup():
    while True:
        await asyncio.sleep(600)
        removed = _cleanup_old_history()
        if removed:
            logger.info(f"Удалено устаревших записей истории: {removed}")
        _cleanup_search_cache()

async def _warmup_knowledge_base_model():
    """Прогревает модель эмбеддингов сразу при старте сервиса — иначе
    самый первый реальный запрос после рестарта платит "холодный старт"
    (загрузка модели + сетевые обращения к Hugging Face для проверки
    кэша), что может занять больше минуты и вызвать таймаут у бота."""
    try:
        import asyncio as _asyncio
        from database.knowledge_base import search_knowledge_base
        loop = _asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: search_knowledge_base("прогрев модели", top_k=1))
        logger.info("🔥 Модель эмбеддингов базы знаний прогрета при старте")
    except Exception as e:
        logger.warning(f"Не удалось прогреть модель эмбеддингов при старте: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_periodic_cleanup())
    asyncio.create_task(_warmup_knowledge_base_model())
    yield
    task.cancel()

async def _custom_rate_limit_handler(request: Request, exc: RateLimitExceeded):
    """Собственный обработчик превышения лимита запросов: для /consult
    возвращает ответ в том же формате, что и обычный ConsultResponse
    (с полем reply), чтобы клиенты (бот, WebApp) никогда не падали на
    неожиданной форме JSON — просто получали вежливое сообщение."""
    if request.url.path == "/consult":
        return JSONResponse(
            status_code=429,
            content={
                "reply": "Слишком много запросов подряд — подождите немного и попробуйте снова.",
                "escalate": False,
                "response_id": None,
            },
        )
    return JSONResponse(status_code=429, content={"error": "rate_limit_exceeded"})

app = FastAPI(lifespan=lifespan)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)
app.add_exception_handler(RateLimitExceeded, _custom_rate_limit_handler)

def get_app_setup_instructions() -> str:
    """Реальные, написанные администратором инструкции по подключению
    приложений (страница 'help' в БД, тот же текст, что видит пользователь
    в самом боте) — используются вместо общих знаний модели о произвольных
    VPN-приложениях, чтобы AI называл реально поддерживаемые сервисом
    приложения и настоящие ссылки на скачивание, а не выдуманные."""
    if not BOT_DB_PATH or not os.path.exists(BOT_DB_PATH):
        return ""
    try:
        db_uri = f"file:{BOT_DB_PATH}?mode=ro"
        conn = sqlite3.connect(db_uri, uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("SELECT text_default, text_custom FROM pages WHERE page_key = 'help'")
        row = cur.fetchone()
        conn.close()
        if not row:
            return ""
        text = row["text_custom"] or row["text_default"] or ""
        text = _HTML_TAG_RE.sub("", text)
        return html.unescape(text).strip()
    except Exception as e:
        logger.warning(f"Не удалось получить инструкцию по подключению: {e}")
        return ""


async def query_full_customer_profile(telegram_id: int) -> dict:
    """
    Собирает максимально полный профиль клиента для AI-консультанта:
    аккаунт, ВСЕ VPN-ключи (не только последний), историю платежей,
    баланс и реферальную статистику. Соединение строго read-only.
    """
    if not BOT_DB_PATH or not os.path.exists(BOT_DB_PATH):
        return {"error": "DB not found"}

    try:
        db_uri = f"file:{BOT_DB_PATH}?mode=ro"
        conn = sqlite3.connect(db_uri, uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute(
            """SELECT id, username, first_name, last_name, is_banned, is_bot_blocked,
                      created_at, used_trial, referral_code, referred_by,
                      personal_balance, referral_coefficient
               FROM users WHERE telegram_id = ?""",
            (telegram_id,),
        )
        user_row = cur.fetchone()
        if not user_row:
            conn.close()
            return {"found": False}

        user_id = user_row["id"]
        profile = {"found": True, "user": dict(user_row)}

        if user_row["referred_by"]:
            cur.execute("SELECT username FROM users WHERE id = ?", (user_row["referred_by"],))
            ref_row = cur.fetchone()
            profile["referred_by_username"] = ref_row["username"] if ref_row else None

        cur.execute(
            """SELECT vk.id, vk.custom_name, vk.expires_at, vk.traffic_used,
                      vk.traffic_limit, vk.created_at, vk.sub_id, vk.server_id,
                      s.name AS server_name, t.name AS tariff_name, t.max_ips
               FROM vpn_keys vk
               LEFT JOIN servers s ON s.id = vk.server_id
               LEFT JOIN tariffs t ON t.id = vk.tariff_id
               WHERE vk.user_id = ?
               ORDER BY vk.created_at DESC""",
            (user_id,),
        )
        keys_list = [dict(row) for row in cur.fetchall()]
        profile["keys"] = keys_list

        cur.execute(
            """SELECT amount_cents, amount_stars, payment_type, status,
                      period_days, paid_at
               FROM payments
               WHERE user_id = ?
               ORDER BY paid_at DESC LIMIT 10""",
            (user_id,),
        )
        profile["payments"] = [dict(row) for row in cur.fetchall()]

        cur.execute(
            """SELECT level, COUNT(*) AS referrals_count,
                      SUM(total_payments_count) AS total_payments,
                      SUM(total_reward_cents) AS total_reward_cents,
                      SUM(total_reward_days) AS total_reward_days
               FROM referral_stats
               WHERE referrer_id = ?
               GROUP BY level""",
            (user_id,),
        )
        profile["referral_stats"] = [dict(row) for row in cur.fetchall()]

        cur.execute(
            """SELECT code, discount_percent, expires_at, activation_limit, usage_count, type
               FROM promo_codes
               WHERE is_active = 1
                 AND ai_visible = 1
                 AND (expires_at IS NULL OR expires_at > datetime('now'))
                 AND (issued_to_user_id IS NULL OR issued_to_user_id = ?)""",
            (user_id,),
        )
        profile["available_promo_codes"] = [dict(row) for row in cur.fetchall()]

        cur.execute(
            """SELECT name, duration_days, price_rub, traffic_limit_gb
               FROM tariffs
               WHERE is_active = 1
               ORDER BY display_order, duration_days"""
        )
        profile["available_tariffs"] = [dict(row) for row in cur.fetchall()]

        cur.execute("SELECT key, value FROM settings WHERE key IN ('referral_enabled', 'trial_enabled')")
        settings_rows = {row["key"]: row["value"] for row in cur.fetchall()}
        profile["referral_enabled"] = settings_rows.get("referral_enabled") == "1"
        profile["trial_enabled"] = settings_rows.get("trial_enabled") == "1"

        cur.execute("SELECT level_number, percent FROM referral_levels WHERE enabled = 1 ORDER BY level_number")
        profile["referral_levels"] = [dict(row) for row in cur.fetchall()]

        conn.close()

        # Реальные ссылки подписки — только для активных (не истёкших) ключей,
        # тем же способом, что использует сам бот (живой запрос к панели).
        from datetime import datetime as _dt
        from bot.services.vpn_api import get_subscription_url_for_key

        now_str = _dt.utcnow().isoformat(sep=" ")
        for k in keys_list:
            k["sub_url"] = None
            if k.get("expires_at") and str(k["expires_at"]) > now_str and k.get("sub_id") and k.get("server_id"):
                try:
                    k["sub_url"] = await get_subscription_url_for_key(
                        {"sub_id": k["sub_id"], "server_id": k["server_id"]}
                    )
                except Exception as e:
                    logger.warning(f"Не удалось получить ссылку подписки для ключа {k.get('id')}: {e}")

        return profile
    except Exception as e:
        logger.error(f"DB error: {e}")
        return {"error": str(e)}

def _format_customer_profile(profile: dict) -> str:
    """Форматирует полный профиль клиента в компактный текстовый блок для system prompt."""
    if not profile or profile.get("found") is False:
        return "Пользователь не найден в базе (возможно, ещё не запускал бота)."
    if profile.get("error"):
        return f"Ошибка БД: {profile['error']}"

    u = profile.get("user", {})
    parts = []

    name_bits = [b for b in (u.get("first_name"), u.get("last_name")) if b]
    display_name = " ".join(name_bits) if name_bits else "—"
    parts.append(f"Клиент: @{u.get('username') or '—'} ({display_name})")
    parts.append(f"Зарегистрирован: {u.get('created_at', 'N/A')}")

    if u.get("is_banned"):
        parts.append("⚠️ СТАТУС: ЗАБАНЕН")
    if u.get("is_bot_blocked"):
        parts.append("⚠️ Пользователь заблокировал бота в Telegram")

    balance_rub = (u.get("personal_balance") or 0) / 100
    parts.append(f"Баланс: {balance_rub:.2f} ₽")
    parts.append(f"Пробный период использован: {'да' if u.get('used_trial') else 'нет'}")

    if profile.get("referred_by_username"):
        parts.append(f"Приглашён пользователем: @{profile['referred_by_username']}")
    if u.get("referral_code"):
        parts.append(f"Свой реферальный код: {u['referral_code']}")

    keys = profile.get("keys", [])
    if not keys:
        parts.append("VPN-ключей нет.")
    else:
        parts.append(f"VPN-ключей всего: {len(keys)}")
        for k in keys[:5]:
            used = (k.get("traffic_used") or 0) / 1e9
            limit = (k.get("traffic_limit") or 0) / 1e9
            traffic_str = f"{used:.2f} ГБ (безлимит)" if not k.get("traffic_limit") else f"{used:.2f}/{limit:.2f} ГБ"
            name = k.get("custom_name") or f"ключ #{k.get('id')}"
            server = k.get("server_name") or "сервер не указан"
            tariff_str = k.get("tariff_name") or "неизвестен"
            max_ips = k.get("max_ips")
            devices_str = f", лимит устройств: {max_ips}" if max_ips else ""
            parts.append(f"  • {name} — {server}, тариф «{tariff_str}»{devices_str}, до {k.get('expires_at')}, трафик {traffic_str}")
            if k.get("sub_url"):
                parts.append(f"    Ссылка VPN-конфигурации для импорта в приложение (Happ/INCY и т.п.), НЕ ссылка на оплату: {k['sub_url']}")
            else:
                parts.append("    Ссылка VPN-конфигурации: недоступна (ключ истёк либо ошибка получения — НЕ придумывай ссылку сам, направь в раздел «Мои ключи» в боте)")

    payments = profile.get("payments", [])
    if payments:
        total_rub = sum((p.get("amount_cents") or 0) for p in payments if p.get("status") == "paid") / 100
        parts.append(f"Платежей в истории: {len(payments)} (сумма последних: {total_rub:.2f} ₽)")
        last = payments[0]
        parts.append(f"Последний платёж: {last.get('paid_at')}, статус: {last.get('status')}")

    ref_stats = profile.get("referral_stats", [])
    if ref_stats:
        total_refs = sum(r.get("referrals_count") or 0 for r in ref_stats)
        total_earned = sum(r.get("total_reward_cents") or 0 for r in ref_stats) / 100
        parts.append(f"Пригласил рефералов: {total_refs}, заработано: {total_earned:.2f} ₽")

    tariffs = profile.get("available_tariffs", [])
    if tariffs:
        parts.append("Реальные доступные тарифы (используй ТОЛЬКО эти названия и цены, никогда не выдумывай другие):")
        for t in tariffs:
            traffic = f"{t['traffic_limit_gb']} ГБ" if t.get("traffic_limit_gb") else "безлимит"
            parts.append(f"  • {t['name']} — {t['price_rub']} ₽ за {t['duration_days']} дн., трафик: {traffic}")
    else:
        parts.append("Список тарифов сейчас недоступен — НЕ придумывай названия/цены тарифов, направь в раздел «Мои ключи» в боте.")

    promo_codes = profile.get("available_promo_codes", [])
    if promo_codes:
        parts.append("Реально доступные ЭТОМУ клиенту промокоды (используй ТОЛЬКО эти, никогда не выдумывай и не подтверждай существование других):")
        for p in promo_codes:
            limit_str = f", осталось активаций: {p['activation_limit'] - p['usage_count']}" if p.get("activation_limit") else ""
            link_str = ""
            if p.get("type") == "promo" and _TELEGRAM_START_PARAM_RE.match(p["code"]):
                link_str = f" — ссылка для автоматической активации (без ручного ввода): https://t.me/{BOT_USERNAME}?start=pr_{p['code']}"
            parts.append(f"  • {p['code']} — скидка {p['discount_percent']}%, действует до {p.get('expires_at') or 'бессрочно'}{limit_str}{link_str}")
    else:
        parts.append("Сейчас нет известных доступных промокодов для этого клиента — НЕ подтверждай существование или скидку по промокоду, который клиент называет сам, если его нет в этом списке; направь в раздел «Промокоды» в боте, там есть реальная проверка.")

    if "referral_enabled" in profile:
        if profile["referral_enabled"]:
            levels = profile.get("referral_levels", [])
            if levels:
                levels_str = ", ".join(f"уровень {l['level_number']}: {l['percent']}%" for l in levels)
                parts.append(f"Реферальная программа: АКТИВНА ({levels_str})")
            else:
                parts.append("Реферальная программа: активна, но нет включённых уровней вознаграждения")
        else:
            parts.append("Реферальная программа: НЕ активна (отключена администратором)")

    if "trial_enabled" in profile:
        parts.append(f"Пробный период сервиса: {'доступен новым клиентам' if profile['trial_enabled'] else 'сейчас отключён администратором'}")

    return "\n".join(parts)

class ConsultRequest(BaseModel):
    user_id: int
    message: str
    image_base64: str | None = None  # data URL скриншота, если клиент прислал фото

class ConsultResponse(BaseModel):
    reply: str
    escalate: bool
    response_id: str | None = None


class FeedbackRequest(BaseModel):
    response_id: str
    rating: str  # "up" или "down"

TELEGRAM_API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

ESCALATION_THROTTLE_SECONDS = int(os.environ.get("ESCALATION_THROTTLE_SECONDS", "600"))
_last_escalation_at: dict[int, float] = {}
_escalation_lock = asyncio.Lock()


def _build_escalation_text(user_id: int, question: str, history_messages: list) -> str:
    lines = [
        "\U0001F916 <b>AI \u043f\u0435\u0440\u0435\u0430\u0434\u0440\u0435\u0441\u043e\u0432\u0430\u043b \u0434\u0438\u0430\u043b\u043e\u0433</b>",
        f"\U0001F464 User ID: <code>{user_id}</code>",
        "",
        "\u2753 <b>\u0412\u043e\u043f\u0440\u043e\u0441:</b>",
        (question or "")[:500],
        "",
    ]
    recent = [m for m in (history_messages or []) if m.get("role") in ("user", "assistant")][-6:]
    if recent:
        lines.append("\U0001F4DC <b>\u041a\u043e\u043d\u0442\u0435\u043a\u0441\u0442 \u043f\u0435\u0440\u0435\u043f\u0438\u0441\u043a\u0438:</b>")
        for m in recent:
            role = "\u041a\u043b\u0438\u0435\u043d\u0442" if m.get("role") == "user" else "AI"
            content = (m.get("content") or "")[:300]
            if content:
                lines.append(f"<i>{role}:</i> {content}")
    text = "\n".join(lines)
    return text[:4000]


async def _notify_admins_escalation(user_id: int, question: str, history_messages: list) -> None:
    if not BOT_TOKEN or not ADMIN_IDS:
        logger.warning("Escalation notify skipped: BOT_TOKEN or ADMIN_IDS not configured")
        return

    now = time.time()
    async with _escalation_lock:
        last = _last_escalation_at.get(user_id, 0.0)
        if now - last < ESCALATION_THROTTLE_SECONDS:
            remaining = int(ESCALATION_THROTTLE_SECONDS - (now - last))
            logger.info(f"Escalation notify throttled for user {user_id}, {remaining}s remaining")
            return
        _last_escalation_at[user_id] = now

    text = _build_escalation_text(user_id, question, history_messages)
    try:
        async with httpx.AsyncClient(timeout=10.0) as http_client:
            for admin_id in ADMIN_IDS:
                try:
                    resp = await http_client.post(
                        f"{TELEGRAM_API_BASE}/sendMessage",
                        json={"chat_id": admin_id, "text": text, "parse_mode": "HTML"},
                    )
                    if resp.status_code != 200:
                        logger.warning(f"Escalation notify failed for admin {admin_id}: {resp.status_code} {resp.text}")
                except Exception as e:
                    logger.error(f"Escalation notify error for admin {admin_id}: {e}")
    except Exception as e:
        logger.error(f"Escalation notify outer error: {e}")


@app.post("/consult", response_model=ConsultResponse)
@limiter.limit("10/minute")
async def consult(req: ConsultRequest, request: Request, token: str = Depends(verify_token)):
    async with _lock:
        history_messages = _load_recent_history(req.user_id, MAX_HISTORY_MESSAGES) + [
            {"role": "user", "content": req.message}
        ]

    customer_profile = await query_full_customer_profile(req.user_id)

    if customer_profile.get("found") is False:
        logger.info(f"AI consult отклонён: пользователь {req.user_id} не зарегистрирован")
        return ConsultResponse(
            reply="Похоже, вы ещё не зарегистрированы в боте. Нажмите /start, чтобы начать пользоваться ECLIPSE Unlimited — после этого я смогу вам помочь.",
            escalate=False,
        )

    profile_block = _format_customer_profile(customer_profile)
    setup_instructions = get_app_setup_instructions()
    from database.requests import get_effective_webapp_url
    public_shop_url = get_effective_webapp_url().rstrip("/") + "/shop"
    full_system = SYSTEM_PROMPT.replace("%PUBLIC_SHOP_URL%", public_shop_url).replace("%BOT_USERNAME%", BOT_USERNAME) + "\n\nПолный профиль клиента (используй для персонального ответа, НЕ показывай пользователю сырые данные без необходимости):\n" + profile_block
    if setup_instructions:
        full_system += "\n\nРЕАЛЬНАЯ инструкция по подключению приложений (используй именно эти данные для вопросов о настройке/приложениях/скачивании — это те же приложения и ссылки, что видит пользователь в самом боте; НЕ упоминай другие приложения и не выдумывай другие ссылки):\n" + setup_instructions

    # Автоматический RAG-поиск по базе знаний ДО обращения к модели — надёжнее,
    # чем полагаться на то, что модель сама решит вызвать search_knowledge_base
    # как инструмент (на практике вызывает не всегда, даже когда это уместно).
    try:
        loop = asyncio.get_event_loop()
        from database.knowledge_base import search_knowledge_base
        # top_k=1 (не 3) и повышенный порог сходства — экономим токены,
        # берём только САМУЮ релевантную статью, а не все более-менее похожие.
        kb_results = await loop.run_in_executor(
            None, lambda: search_knowledge_base(req.message, top_k=1, min_score=0.6)
        )
        if kb_results:
            kb_block = "\n\n".join(f"### {r['title']}\n{r['content']}" for r in kb_results)
            full_system += "\n\nКРИТИЧЕСКИ ВАЖНО: ниже — статья базы знаний поддержки, специально найденная как наиболее релевантная ИМЕННО этому вопросу клиента. Она приоритетнее твоих общих знаний по теме — основывай ответ на ней, а не на догадках или общей теории, даже если у тебя есть другое правдоподобное объяснение. Перескажи её суть своими словами, не копируй дословно:\n" + kb_block
            logger.info(f"📚 Автопоиск в базе знаний нашёл {len(kb_results)} статей (user {req.user_id})")
    except Exception as e:
        logger.warning(f"Ошибка автопоиска в базе знаний: {e}")

    if req.image_base64:
        try:
            reply_text = await analyze_screenshot(req.image_base64, req.message, full_system)
        except Exception as e:
            logger.error(f"Неожиданная ошибка анализа скриншота: {e}")
            reply_text = "Не удалось проанализировать изображение прямо сейчас. Опишите проблему текстом, пожалуйста, или напишите в поддержку."
        escalate = "[ESCALATE]" in reply_text
        reply_text = reply_text.replace("[ESCALATE]", "").strip()
        if not reply_text:
            reply_text = "Не удалось проанализировать изображение."
        if escalate:
            asyncio.create_task(_notify_admins_escalation(req.user_id, req.message or "[скриншот]", history_messages))
        response_id = uuid.uuid4().hex
        async with _lock:
            _save_history_row(req.user_id, "user", req.message or "[прислал скриншот]")
            _save_history_row(req.user_id, "assistant", reply_text, response_id=response_id, escalate=escalate)
        return ConsultResponse(reply=reply_text, escalate=escalate, response_id=response_id)

    messages = [{"role": "system", "content": full_system}] + history_messages

    try:
        response = await _chat_completion_with_fallback(
            messages, max_tokens=1200, temperature=0.7,
            tools=[SEARCH_KNOWLEDGE_BASE_TOOL, WEB_SEARCH_TOOL, GITHUB_SEARCH_TOOL, GITHUB_LATEST_RELEASE_TOOL, CHECK_SERVER_STATUS_TOOL], tool_choice="auto", timeout=15.0,
        )
        assistant_msg = response.choices[0].message

        if not assistant_msg.tool_calls and _looks_like_stray_tool_json(assistant_msg.content):
            logger.warning(f"Модель написала псевдо-вызов инструмента голым текстом вместо tool_call: {assistant_msg.content!r}, форсирую ответ без инструментов")
            response = await _chat_completion_with_fallback(
                messages, max_tokens=1200, temperature=0.7, timeout=15.0,
                tools=[SEARCH_KNOWLEDGE_BASE_TOOL, WEB_SEARCH_TOOL, GITHUB_SEARCH_TOOL, GITHUB_LATEST_RELEASE_TOOL, CHECK_SERVER_STATUS_TOOL], tool_choice="none",
            )
            assistant_msg = response.choices[0].message

        if assistant_msg.tool_calls:
            messages.append(assistant_msg.model_dump(exclude_none=True))
            for tool_call in assistant_msg.tool_calls:
                if tool_call.function.name == "search_knowledge_base":
                    try:
                        args = json.loads(tool_call.function.arguments)
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    kb_query = (args.get("query") or "").strip()
                    logger.info(f"📚 AI ищет в базе знаний (user {req.user_id}): {kb_query}")
                    if kb_query:
                        # asyncio уже импортирован на уровне модуля — локальный
                        # import здесь делал 'asyncio' локальной переменной во
                        # ВСЕЙ функции consult() и ломал более ранний код
                        # (UnboundLocalError), поэтому убран.
                        from database.knowledge_base import search_knowledge_base
                        loop = asyncio.get_event_loop()
                        kb_results = await loop.run_in_executor(None, search_knowledge_base, kb_query)
                        if kb_results:
                            kb_result_text = "\n\n".join(
                                f"### {r['title']}\n{r['content']}" for r in kb_results
                            )
                        else:
                            kb_result_text = "В базе знаний не найдено релевантных статей по этому вопросу."
                    else:
                        kb_result_text = "Пустой запрос."
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": kb_result_text,
                    })
                elif tool_call.function.name == "web_search":
                    try:
                        args = json.loads(tool_call.function.arguments)
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    search_query = (args.get("query") or "").strip()
                    logger.info(f"🔎 AI ищет в интернете (user {req.user_id}): {search_query}")
                    search_result = await web_search_tavily(search_query) if search_query else "Пустой поисковый запрос."
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": search_result,
                    })
                elif tool_call.function.name == "github_search":
                    try:
                        args = json.loads(tool_call.function.arguments)
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    gh_query = (args.get("query") or "").strip()
                    gh_repo = (args.get("repo") or "").strip() or None
                    logger.info(f"🐙 AI ищет на GitHub (user {req.user_id}, repo={gh_repo}): {gh_query}")
                    gh_result = await github_search(gh_query, gh_repo) if gh_query else "Пустой поисковый запрос."
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": gh_result,
                    })
                elif tool_call.function.name == "github_latest_release":
                    try:
                        args = json.loads(tool_call.function.arguments)
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                    rel_repo = (args.get("repo") or "").strip()
                    logger.info(f"🏷️ AI проверяет последнюю версию (user {req.user_id}): {rel_repo}")
                    rel_result = await github_latest_release(rel_repo) if rel_repo else "Не указан репозиторий."
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": rel_result,
                    })
                elif tool_call.function.name == "check_server_status":
                    logger.info(f"🖥️ AI проверяет статус сервера (user {req.user_id})")
                    status_result = await check_server_status(req.user_id)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": status_result,
                    })
                else:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": "Инструмент не найден.",
                    })
            # Повторный запрос к модели уже с результатами поиска.
            # tools передаём снова (Groq требует их в контексте, раз в истории
            # уже есть tool_call/tool-результат), но tool_choice="none" явно
            # запрещает ещё один вызов инструмента — модель обязана дать
            # финальный текстовый ответ на основе того, что уже нашла.
            response = await _chat_completion_with_fallback(
                messages, max_tokens=1200, temperature=0.7, timeout=15.0,
                tools=[SEARCH_KNOWLEDGE_BASE_TOOL, WEB_SEARCH_TOOL, GITHUB_SEARCH_TOOL, GITHUB_LATEST_RELEASE_TOOL, CHECK_SERVER_STATUS_TOOL], tool_choice="none",
            )
            assistant_msg = response.choices[0].message

        reply_text = assistant_msg.content or "Не удалось сформировать ответ."
        reply_text = _sanitize_reply(reply_text)
    except RateLimitError:
        logger.warning(f"Rate limit for user {req.user_id}")
        asyncio.create_task(_notify_admins_escalation(req.user_id, req.message, history_messages))
        _save_history_row(req.user_id, "user", req.message)
        _save_history_row(req.user_id, "assistant", "Высокая нагрузка, попробуйте позже...", escalate=True)
        return ConsultResponse(reply="Высокая нагрузка, попробуйте позже...", escalate=True)
    except (APIConnectionError, APIError) as e:
        logger.error(f"Groq API error: {e}")
        asyncio.create_task(_notify_admins_escalation(req.user_id, req.message, history_messages))
        _save_history_row(req.user_id, "user", req.message)
        _save_history_row(req.user_id, "assistant", "Ошибка API, передаю оператору", escalate=True)
        return ConsultResponse(reply="Ошибка API, передаю оператору", escalate=True)
    except Exception as e:
        logger.error(f"Consult unexpected error: {e}")
        asyncio.create_task(_notify_admins_escalation(req.user_id, req.message, history_messages))
        _save_history_row(req.user_id, "user", req.message)
        _save_history_row(req.user_id, "assistant", "Не удалось получить ответ вовремя, попробуйте ещё раз или обратитесь в поддержку", escalate=True)
        return ConsultResponse(reply="Не удалось получить ответ вовремя, попробуйте ещё раз или обратитесь в поддержку", escalate=True)

    escalate = "[ESCALATE]" in reply_text
    reply_text = reply_text.replace("[ESCALATE]", "").strip()
    if not reply_text:
        reply_text = "Передаю ваш вопрос администратору, он ответит вам в ближайшее время."

    if escalate:
        asyncio.create_task(_notify_admins_escalation(req.user_id, req.message, history_messages))

    response_id = uuid.uuid4().hex
    async with _lock:
        _save_history_row(req.user_id, "user", req.message)
        _save_history_row(req.user_id, "assistant", reply_text, response_id=response_id, escalate=escalate)

    return ConsultResponse(reply=reply_text, escalate=escalate, response_id=response_id)

@app.post("/feedback")
@limiter.limit("30/minute")
async def feedback(req: FeedbackRequest, request: Request, token: str = Depends(verify_token)):
    if req.rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="rating must be 'up' or 'down'")
    saved = _save_feedback(req.response_id, req.rating)
    if not saved:
        raise HTTPException(status_code=404, detail="response_id not found")
    logger.info(f"Фидбек по ответу {req.response_id}: {req.rating}")
    return {"status": "ok"}

@app.get("/stats")
async def stats(token: str = Depends(verify_token)):
    """Агрегированная статистика работы AI-консультанта: сколько уникальных
    пользователей обратилось, сколько ответов дано, доля эскалаций, разбивка
    фидбека 👍/👎. Отдельно за всё время и за последние 24 часа."""

    def _query_window(since_ts: float | None) -> dict:
        conn = sqlite3.connect(AI_HISTORY_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.cursor()
            where = "WHERE created_at >= ?" if since_ts is not None else ""
            params = (since_ts,) if since_ts is not None else ()

            cur.execute(f"SELECT COUNT(DISTINCT telegram_id) AS n FROM history {where}", params)
            unique_users = cur.fetchone()["n"]

            assistant_where = (where + " AND role = 'assistant'") if where else "WHERE role = 'assistant'"
            cur.execute(f"SELECT COUNT(*) AS n FROM history {assistant_where}", params)
            total_responses = cur.fetchone()["n"]

            cur.execute(f"SELECT COUNT(*) AS n FROM history {assistant_where} AND escalate = 1", params)
            escalations = cur.fetchone()["n"]

            cur.execute(f"SELECT COUNT(*) AS n FROM history {assistant_where} AND feedback = 'up'", params)
            feedback_up = cur.fetchone()["n"]

            cur.execute(f"SELECT COUNT(*) AS n FROM history {assistant_where} AND feedback = 'down'", params)
            feedback_down = cur.fetchone()["n"]

            return {
                "unique_users": unique_users,
                "total_responses": total_responses,
                "escalations": escalations,
                "escalation_rate_percent": round(escalations / total_responses * 100, 1) if total_responses else 0.0,
                "feedback_up": feedback_up,
                "feedback_down": feedback_down,
            }
        finally:
            conn.close()

    return {
        "all_time": _query_window(None),
        "last_24h": _query_window(time.time() - 24 * 60 * 60),
    }


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "model_chain": list(MODEL_CHAIN),
        "models": {
            model: {
                "on_cooldown": _is_on_cooldown(model),
                "cooldown_remaining_seconds": round(_remaining_cooldown(model)),
            }
            for model in MODEL_CHAIN
        },
        "db_configured": bool(BOT_DB_PATH),
    }
