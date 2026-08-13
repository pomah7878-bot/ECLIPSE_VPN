"""
Раздел админ-панели «Инструменты сервера»: диагностика и обслуживание
конкретной ноды через API панели 3x-ui, без SSH-доступа.

Все действия — вызовы к API панели выбранного сервера:
- 📊 Состояние: CPU, RAM, диск, аптайм, нагрузка (server/status)
- 📋 Логи Xray: хвост логов ядра (server/logs/{count})
- 🔄 Перезапуск Xray: рестарт ядра (server/restartXrayService)
- 🧹 Уборка исчерпавших трафик клиентов (inbounds/delDepletedClients/{id})
- 🔑 Генерация свежих Reality-ключей (server/getNewX25519Cert)

Клиент панели берётся через кеширующую фабрику get_client_from_server_data
и логинится перед вызовом (login() дёшев на уже валидном токене).
Ошибки панели ловятся широко на границе хендлера, чтобы любой сбой ноды
показывался пользователю дружелюбным сообщением, а не падал в общий хендлер.
"""
import html
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.utils.admin import is_admin
from bot.utils.text import safe_edit_or_send
from bot.services.vpn_api import get_client_from_server_data
from database.requests import get_server_by_id
from bot.keyboards.admin_server_tools import (
    server_tools_menu_kb,
    server_tools_back_kb,
    xray_logs_count_kb,
    restart_xray_confirm_kb,
    delete_depleted_inbounds_kb,
    delete_depleted_confirm_kb,
)

logger = logging.getLogger(__name__)
router = Router()

# Запас под лимит сообщения Telegram (4096) с учётом заголовка и тегов <pre>.
_TG_LIMIT = 4096


# --------------------------------------------------------------------------- #
# Вспомогательные функции форматирования
# --------------------------------------------------------------------------- #
def _fmt_bytes(value) -> str:
    """Человекочитаемый размер из байтов."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return '—'
    for unit in ('Б', 'КБ', 'МБ', 'ГБ', 'ТБ'):
        if abs(num) < 1024.0:
            return f'{num:.1f} {unit}'
        num /= 1024.0
    return f'{num:.1f} ПБ'


def _fmt_uptime(value) -> str:
    """Аптайм из секунд в формат «Nд Nч Nм»."""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return '—'
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f'{days}д')
    if hours:
        parts.append(f'{hours}ч')
    if minutes:
        parts.append(f'{minutes}м')
    return ' '.join(parts) or '<1м'


def _pair(status: dict, key: str):
    """Возвращает (current, total) для полей вида mem/disk = {current, total}."""
    value = status.get(key) or {}
    if isinstance(value, dict):
        return value.get('current'), value.get('total')
    return None, None


def _format_status(server: dict, status: dict) -> str:
    """Собирает текст дашборда состояния сервера."""
    name = html.escape(str(server.get('name', '')))
    lines = [f'📊 <b>Состояние</b> · 🖥 {name}\n']

    cpu = status.get('cpu')
    if cpu is not None:
        try:
            lines.append(f'⚙️ CPU: <b>{float(cpu):.1f}%</b>')
        except (TypeError, ValueError):
            pass

    loads = status.get('loads')
    if isinstance(loads, list) and loads:
        lines.append(f"📈 Load: <b>{' / '.join(str(x) for x in loads[:3])}</b>")

    mem_cur, mem_total = _pair(status, 'mem')
    if mem_cur is not None:
        lines.append(f'🧠 RAM: <b>{_fmt_bytes(mem_cur)}</b> / {_fmt_bytes(mem_total)}')

    disk_cur, disk_total = _pair(status, 'disk')
    if disk_cur is not None:
        lines.append(f'💾 Диск: <b>{_fmt_bytes(disk_cur)}</b> / {_fmt_bytes(disk_total)}')

    uptime = status.get('uptime')
    if uptime is not None:
        lines.append(f'⏱ Аптайм: <b>{_fmt_uptime(uptime)}</b>')

    xray = status.get('xray') or {}
    if isinstance(xray, dict) and xray:
        state = html.escape(str(xray.get('state', '?')))
        version = html.escape(str(xray.get('version', '?')))
        lines.append(f'🚀 Xray: <b>{state}</b> (v{version})')

    if len(lines) == 1:
        lines.append('⚠️ Панель не вернула детальных полей статуса.')
    return '\n'.join(lines)


def _resolve_server(callback: CallbackQuery):
    """Общая проверка прав + получение сервера. Возвращает (server_id, server) или (None, None)."""
    server_id = int(callback.data.split(':')[1])
    server = get_server_by_id(server_id)
    return server_id, server


# --------------------------------------------------------------------------- #
# Меню раздела
# --------------------------------------------------------------------------- #
@router.callback_query(F.data.startswith('admin_server_tools:'))
async def show_server_tools(callback: CallbackQuery):
    """Открывает меню инструментов конкретного сервера."""
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    server_id, server = _resolve_server(callback)
    if not server:
        await callback.answer('❌ Сервер не найден', show_alert=True)
        return
    name = html.escape(str(server.get('name', server_id)))
    text = (
        f'🛠 <b>Инструменты сервера</b>\n'
        f'🖥 {name}\n\n'
        f'Диагностика и обслуживание ноды напрямую через API панели.'
    )
    await safe_edit_or_send(callback.message, text=text,
                            reply_markup=server_tools_menu_kb(server_id))
    await callback.answer()


# --------------------------------------------------------------------------- #
# 📊 Состояние сервера
# --------------------------------------------------------------------------- #
@router.callback_query(F.data.startswith('srvtools_status:'))
async def show_status(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    server_id, server = _resolve_server(callback)
    if not server:
        await callback.answer('❌ Сервер не найден', show_alert=True)
        return
    await callback.answer('Запрашиваю статус…')
    try:
        client = get_client_from_server_data(server)
        await client.login()
        status = await client.get_server_status()
    except Exception as exc:  # noqa: BLE001 — граница хендлера, показываем ошибку пользователю
        logger.warning('srvtools status failed (server %s): %s', server_id, exc)
        await safe_edit_or_send(
            callback.message,
            text=f'❌ Не удалось получить статус.\n\n<code>{html.escape(str(exc))}</code>',
            reply_markup=server_tools_back_kb(server_id))
        return
    if not status:
        text = '⚠️ Панель не вернула данные статуса (возможно, эндпоинт недоступен в этой версии).'
    else:
        text = _format_status(server, status)
    await safe_edit_or_send(callback.message, text=text,
                            reply_markup=server_tools_back_kb(server_id))


# --------------------------------------------------------------------------- #
# 📋 Логи Xray
# --------------------------------------------------------------------------- #
@router.callback_query(F.data.startswith('srvtools_logs:'))
async def logs_pick_count(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    server_id, server = _resolve_server(callback)
    if not server:
        await callback.answer('❌ Сервер не найден', show_alert=True)
        return
    text = '📋 <b>Логи Xray</b>\n\nСколько последних строк показать?'
    await safe_edit_or_send(callback.message, text=text,
                            reply_markup=xray_logs_count_kb(server_id))
    await callback.answer()


@router.callback_query(F.data.startswith('srvtools_logs_show:'))
async def logs_show(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    parts = callback.data.split(':')
    server_id = int(parts[1])
    count = int(parts[2])
    server = get_server_by_id(server_id)
    if not server:
        await callback.answer('❌ Сервер не найден', show_alert=True)
        return
    await callback.answer('Запрашиваю логи…')
    try:
        client = get_client_from_server_data(server)
        await client.login()
        lines = await client.get_xray_logs(count)
    except Exception as exc:  # noqa: BLE001
        logger.warning('srvtools logs failed (server %s): %s', server_id, exc)
        await safe_edit_or_send(
            callback.message,
            text=f'❌ Не удалось получить логи.\n\n<code>{html.escape(str(exc))}</code>',
            reply_markup=xray_logs_count_kb(server_id))
        return

    header = f'📋 <b>Логи Xray</b> · последние {count} строк\n'
    # Обрезаем по целым экранированным строкам с конца, чтобы не разорвать
    # HTML-сущность посередине и не превысить лимит Telegram.
    limit = _TG_LIMIT - len(header) - len('<pre></pre>') - 40
    escaped_lines = [html.escape(str(line)) for line in lines]
    kept: list = []
    total = 0
    for line in reversed(escaped_lines):
        addition = len(line) + 1
        if total + addition > limit:
            break
        kept.append(line)
        total += addition
    kept.reverse()
    if kept:
        body = '\n'.join(kept)
    else:
        body = 'Логи пусты или панель их не вернула.'
    text = f'{header}<pre>{body}</pre>'
    await safe_edit_or_send(callback.message, text=text,
                            reply_markup=xray_logs_count_kb(server_id))


# --------------------------------------------------------------------------- #
# 🔄 Перезапуск Xray
# --------------------------------------------------------------------------- #
@router.callback_query(F.data.startswith('srvtools_restart:'))
async def restart_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    server_id, server = _resolve_server(callback)
    if not server:
        await callback.answer('❌ Сервер не найден', show_alert=True)
        return
    text = (
        '🔄 <b>Перезапуск Xray</b>\n\n'
        '⚠️ Ядро Xray на этой ноде будет перезапущено. Все активные '
        'подключения кратковременно оборвутся — клиенты переподключатся '
        'автоматически за несколько секунд.\n\n'
        'Продолжить?'
    )
    await safe_edit_or_send(callback.message, text=text,
                            reply_markup=restart_xray_confirm_kb(server_id))
    await callback.answer()


@router.callback_query(F.data.startswith('srvtools_restart_do:'))
async def restart_do(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    server_id, server = _resolve_server(callback)
    if not server:
        await callback.answer('❌ Сервер не найден', show_alert=True)
        return
    await callback.answer('Перезапускаю Xray…')
    try:
        client = get_client_from_server_data(server)
        await client.login()
        ok = await client.restart_xray()
    except Exception as exc:  # noqa: BLE001
        logger.warning('srvtools restart failed (server %s): %s', server_id, exc)
        await safe_edit_or_send(
            callback.message,
            text=f'❌ Не удалось перезапустить Xray.\n\n<code>{html.escape(str(exc))}</code>',
            reply_markup=server_tools_back_kb(server_id))
        return
    if ok:
        text = '✅ Xray перезапущен.'
    else:
        text = '⚠️ Панель вернула отрицательный результат — перезапуск мог не выполниться.'
    await safe_edit_or_send(callback.message, text=text,
                            reply_markup=server_tools_back_kb(server_id))


# --------------------------------------------------------------------------- #
# 🧹 Уборка исчерпавших трафик клиентов
# --------------------------------------------------------------------------- #
@router.callback_query(F.data.startswith('srvtools_deplete:'))
async def deplete_pick(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    server_id, server = _resolve_server(callback)
    if not server:
        await callback.answer('❌ Сервер не найден', show_alert=True)
        return
    await callback.answer('Загружаю inbound…')
    try:
        client = get_client_from_server_data(server)
        await client.login()
        inbounds = await client.get_inbounds()
    except Exception as exc:  # noqa: BLE001
        logger.warning('srvtools deplete list failed (server %s): %s', server_id, exc)
        await safe_edit_or_send(
            callback.message,
            text=f'❌ Не удалось получить список inbound.\n\n<code>{html.escape(str(exc))}</code>',
            reply_markup=server_tools_back_kb(server_id))
        return
    text = (
        '🧹 <b>Уборка исчерпавших трафик</b>\n\n'
        'Будут удалены клиенты, полностью выбравшие лимит трафика и не '
        'продлившие подписку. Выберите inbound или уберите по всем сразу.'
    )
    await safe_edit_or_send(callback.message, text=text,
                            reply_markup=delete_depleted_inbounds_kb(server_id, inbounds))


@router.callback_query(F.data.startswith('srvtools_deplete_confirm:'))
async def deplete_confirm(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    parts = callback.data.split(':')
    server_id = int(parts[1])
    inbound_id = int(parts[2])
    server = get_server_by_id(server_id)
    if not server:
        await callback.answer('❌ Сервер не найден', show_alert=True)
        return
    scope = 'по всем inbound' if inbound_id == -1 else f'inbound #{inbound_id}'
    text = (
        '🧹 <b>Подтверждение</b>\n\n'
        f'Удалить всех исчерпавших трафик клиентов ({scope})?\n\n'
        '⚠️ Действие необратимо — записи клиентов будут удалены с панели.'
    )
    await safe_edit_or_send(callback.message, text=text,
                            reply_markup=delete_depleted_confirm_kb(server_id, inbound_id))
    await callback.answer()


@router.callback_query(F.data.startswith('srvtools_deplete_do:'))
async def deplete_do(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    parts = callback.data.split(':')
    server_id = int(parts[1])
    inbound_id = int(parts[2])
    server = get_server_by_id(server_id)
    if not server:
        await callback.answer('❌ Сервер не найден', show_alert=True)
        return
    await callback.answer('Удаляю…')
    try:
        client = get_client_from_server_data(server)
        await client.login()
        ok = await client.delete_depleted_clients(inbound_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning('srvtools deplete do failed (server %s): %s', server_id, exc)
        await safe_edit_or_send(
            callback.message,
            text=f'❌ Не удалось выполнить уборку.\n\n<code>{html.escape(str(exc))}</code>',
            reply_markup=server_tools_back_kb(server_id))
        return
    scope = 'по всем inbound' if inbound_id == -1 else f'inbound #{inbound_id}'
    if ok:
        text = f'✅ Уборка выполнена ({scope}).'
    else:
        text = '⚠️ Панель вернула отрицательный результат.'
    await safe_edit_or_send(callback.message, text=text,
                            reply_markup=server_tools_back_kb(server_id))


# --------------------------------------------------------------------------- #
# 🔑 Генерация Reality-ключей
# --------------------------------------------------------------------------- #
@router.callback_query(F.data.startswith('srvtools_reality:'))
async def reality_keys(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    server_id, server = _resolve_server(callback)
    if not server:
        await callback.answer('❌ Сервер не найден', show_alert=True)
        return
    await callback.answer('Генерирую ключи…')
    try:
        client = get_client_from_server_data(server)
        await client.login()
        keys = await client.get_new_x25519_cert()
    except Exception as exc:  # noqa: BLE001
        logger.warning('srvtools reality failed (server %s): %s', server_id, exc)
        await safe_edit_or_send(
            callback.message,
            text=f'❌ Не удалось сгенерировать ключи.\n\n<code>{html.escape(str(exc))}</code>',
            reply_markup=server_tools_back_kb(server_id))
        return
    private_key = keys.get('private_key') if keys else ''
    public_key = keys.get('public_key') if keys else ''
    if not private_key and not public_key:
        text = '⚠️ Панель не вернула ключи.'
    else:
        text = (
            '🔑 <b>Новая пара Reality-ключей</b>\n\n'
            f'Private key:\n<code>{html.escape(private_key)}</code>\n\n'
            f'Public key:\n<code>{html.escape(public_key)}</code>\n\n'
            '🔒 Private key нигде не сохранён — скопируйте его сейчас.'
        )
    await safe_edit_or_send(callback.message, text=text,
                            reply_markup=server_tools_back_kb(server_id))
