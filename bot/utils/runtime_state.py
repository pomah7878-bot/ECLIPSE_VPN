"""
Хранилище живого экземпляра aiogram.Bot, доступное из любого модуля без
проблемы "__main__ vs main".

Почему это нужно: main.py запускается как `python3 main.py`, поэтому
Python регистрирует его в sys.modules под именем "__main__", а не
"main". Если какой-то другой модуль (например bot/webapp/server.py)
делает `from main import bot`, Python не находит "main" в уже
загруженных модулях и импортирует main.py ЗАНОВО, отдельной копией —
но в этой копии `if __name__ == "__main__":` не срабатывает (там
__name__ уже не "__main__"), поэтому функция main() никогда не
вызывается и bot остаётся None навсегда. Получаются две независимые
копии одного файла в памяти одновременно, и обновления в одной не
видны другой.

Этот модуль — нейтральная точка обмена: main.py кладёт сюда экземпляр
бота один раз при старте, а любой другой модуль читает его отсюда же.
Поскольку сам этот файл никогда не запускается как __main__, такой
проблемы с ним не возникает.
"""

_bot_instance = None


def set_bot_instance(bot) -> None:
    """Вызывается один раз из main.py сразу после создания Bot()."""
    global _bot_instance
    _bot_instance = bot


def get_bot_instance():
    """Возвращает текущий экземпляр бота (или None, если ещё не запущен)."""
    return _bot_instance


def get_bot_username() -> str:
    """Юзернейм бота, если уже известен (bot.my_username выставляется в
    main.py при старте после bot.get_me()), иначе пустая строка."""
    bot = _bot_instance
    if bot is not None and getattr(bot, 'my_username', None):
        return bot.my_username
    return ""
