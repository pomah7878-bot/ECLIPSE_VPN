from config import ADMIN_IDS


def is_admin(user_id: int) -> bool:
    """Checks if the user is an administrator (only IDs from config ADMIN_IDS)."""
    return user_id in ADMIN_IDS
