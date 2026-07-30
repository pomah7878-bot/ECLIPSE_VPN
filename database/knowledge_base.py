"""База знаний для RAG (Retrieval-Augmented Generation) AI-консультанта.

Хранит статьи с эмбеддингами (intfloat/multilingual-e5-small, 384 измерения)
и ищет наиболее релевантные к вопросу пользователя через косинусное
сходство. Модель грузится один раз при первом вызове и кэшируется в
памяти процесса — не годится для CLI-скриптов, вызываемых часто, но
идеально для постоянно работающего AI-сервиса (uvicorn).
"""
import json
import logging
import sqlite3
from typing import Any, Optional

logger = logging.getLogger(__name__)

_model = None  # ленивая инициализация — модель грузится при первом вызове


def _get_model():
    """Возвращает (и при необходимости загружает) модель эмбеддингов."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        logger.info("Загружаю модель эмбеддингов intfloat/multilingual-e5-small...")
        _model = SentenceTransformer("intfloat/multilingual-e5-small", device="cpu")
        logger.info("Модель эмбеддингов загружена")
    return _model


def _embed(text: str, is_query: bool = False) -> list[float]:
    """Считает эмбеддинг текста.

    Модель e5 требует префикс "query: " для поисковых запросов и
    "passage: " для сохраняемых документов — так эмбеддинги лучше
    сопоставляются между собой (это требование именно этой модели).
    """
    model = _get_model()
    prefix = "query: " if is_query else "passage: "
    embedding = model.encode(prefix + text, normalize_embeddings=True)
    return embedding.tolist()


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Косинусное сходство. Эмбеддинги уже нормализованы (normalize_embeddings=True),
    поэтому это просто скалярное произведение."""
    return sum(x * y for x, y in zip(a, b))


def add_knowledge_article(
    title: str,
    content: str,
    category: Optional[str] = None,
) -> int:
    """Добавляет статью в базу знаний, считая эмбеддинг автоматически.

    Args:
        title: Заголовок статьи (для навигации/редактирования)
        content: Полный текст статьи (то, что увидит AI при совпадении)
        category: Необязательная категория для группировки

    Returns:
        ID добавленной статьи
    """
    from database.connection import get_db

    embedding = _embed(content, is_query=False)
    with get_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO knowledge_base (title, content, category, embedding)
            VALUES (?, ?, ?, ?)
            """,
            (title, content, category, json.dumps(embedding)),
        )
        return cursor.lastrowid


def search_knowledge_base(query: str, top_k: int = 3, min_score: float = 0.5) -> list[dict[str, Any]]:
    """Ищет наиболее релевантные статьи базы знаний к запросу.

    Args:
        query: Вопрос пользователя (обычный текст, без префиксов)
        top_k: Сколько лучших результатов вернуть
        min_score: Минимальный порог сходства (0..1), чтобы не подсовывать
            AI совсем нерелевантные статьи при отсутствии хорошего совпадения

    Returns:
        Список словарей {id, title, content, category, score},
        отсортированных по убыванию релевантности.
    """
    from database.connection import get_db

    query_embedding = _embed(query, is_query=True)

    with get_db() as conn:
        cursor = conn.execute(
            "SELECT id, title, content, category, embedding FROM knowledge_base WHERE is_active = 1"
        )
        rows = cursor.fetchall()

    scored = []
    for row in rows:
        row_embedding = json.loads(row["embedding"])
        score = _cosine_similarity(query_embedding, row_embedding)
        if score >= min_score:
            scored.append({
                "id": row["id"],
                "title": row["title"],
                "content": row["content"],
                "category": row["category"],
                "score": round(score, 3),
            })

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:top_k]


def get_all_knowledge_articles() -> list[dict[str, Any]]:
    """Возвращает все статьи базы знаний (без эмбеддингов, для админ-панели)."""
    from database.connection import get_db

    with get_db() as conn:
        cursor = conn.execute(
            "SELECT id, title, content, category, is_active, created_at FROM knowledge_base ORDER BY id DESC"
        )
        return [dict(row) for row in cursor.fetchall()]


def delete_knowledge_article(article_id: int) -> bool:
    """Удаляет статью базы знаний по ID."""
    from database.connection import get_db

    with get_db() as conn:
        cursor = conn.execute("DELETE FROM knowledge_base WHERE id = ?", (article_id,))
        return cursor.rowcount > 0
