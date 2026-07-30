"""
AI Support Integration Handler
Интеграция с микросервисом AI-консультанта на порту 8086
"""
import logging
import aiohttp
from config import SUPPORT_API_TOKEN

logger = logging.getLogger(__name__)

AI_SERVICE_URL = "http://localhost:8086"
AI_CONSULT_ENDPOINT = f"{AI_SERVICE_URL}/consult"
AI_FEEDBACK_ENDPOINT = f"{AI_SERVICE_URL}/feedback"
AI_STATS_ENDPOINT = f"{AI_SERVICE_URL}/stats"


async def get_ai_response(user_id: int, message: str, image_base64: str | None = None) -> tuple[str, bool, str | None]:
    """
    Отправляет запрос к AI сервису и возвращает ответ.

    Args:
        user_id: Telegram ID пользователя
        message: Сообщение пользователя (может быть пустым, если прислан только скриншот)
        image_base64: data URL скриншота ошибки, если клиент его прислал

    Returns:
        (reply_text, should_escalate, response_id): Ответ от AI, флаг эскалации
        и идентификатор ответа для последующего фидбека (может быть None).
    """
    try:
        headers = {"X-Support-Token": SUPPORT_API_TOKEN}
        payload = {"user_id": user_id, "message": message, "image_base64": image_base64}
        
        async with aiohttp.ClientSession() as session:
            async with session.post(
                AI_CONSULT_ENDPOINT,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=40)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return (
                        data.get("reply", "Не получилось обработать запрос"),
                        data.get("escalate", False),
                        data.get("response_id"),
                    )
                elif resp.status == 403:
                    logger.error("AI Service: invalid token")
                    return "⚠️ Ошибка авторизации. Контактируйте с поддержкой.", True, None
                else:
                    logger.error(f"AI Service returned status {resp.status}")
                    return "⚠️ Сервис недоступен. Попробуйте позже.", True, None
    
    except aiohttp.ClientConnectionError:
        logger.error(f"Cannot connect to AI service at {AI_SERVICE_URL}")
        return "⚠️ AI помощник недоступен. Обратитесь в поддержку.", True, None
    except aiohttp.ClientError as e:
        logger.error(f"AI Service error: {e}")
        return "⚠️ Ошибка при обращении к AI. Попробуйте позже.", True, None
    except Exception as e:
        logger.error(f"Unexpected error in AI integration: {e}")
        return "⚠️ Неожиданная ошибка. Контактируйте с поддержкой.", True, None


async def send_ai_feedback(response_id: str, rating: str) -> bool:
    """Отправляет оценку 👍/👎 конкретного ответа AI в микросервис.

    Args:
        response_id: идентификатор ответа, полученный из get_ai_response
        rating: "up" или "down"

    Returns:
        True, если фидбек успешно сохранён
    """
    try:
        headers = {"X-Support-Token": SUPPORT_API_TOKEN}
        payload = {"response_id": response_id, "rating": rating}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                AI_FEEDBACK_ENDPOINT,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                return resp.status == 200
    except Exception as e:
        logger.error(f"AI feedback error: {e}")
        return False


async def get_ai_stats() -> dict | None:
    """Получает агрегированную статистику работы AI-консультанта
    (уникальные пользователи, эскалации, фидбек) — для админской команды.

    Returns:
        Словарь со статистикой (all_time / last_24h) или None при ошибке.
    """
    try:
        headers = {"X-Support-Token": SUPPORT_API_TOKEN}
        async with aiohttp.ClientSession() as session:
            async with session.get(
                AI_STATS_ENDPOINT,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                logger.error(f"AI stats endpoint returned status {resp.status}")
                return None
    except Exception as e:
        logger.error(f"AI stats error: {e}")
        return None
