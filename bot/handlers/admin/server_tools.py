"""
Инструменты диагностики сервера прямо из админки бота: логи подключений
Xray, состояние сервера (CPU/память/аптайм) и перезапуск Xray.
Избавляет от необходимости заходить в панель или на сервер по SSH.
"""
import logging
from datetime import datetime

from aiogram import Router, F
from aiogram.types import CallbackQuery

from bot.utils.admin import is_admin
from bot.utils.text import safe_edit_or_send, escape_html
from bot.keyboards.admin_server_tools import (
    server_tools_menu_kb,
    server_tools_back_kb,
    server_restart_confirm_kb,
)

logger = logging.getLogger(__name__)
router = Router()

LOGS_COUNT = 25


def _get_panel_client(server_id: int):
    """Возвращает (клиент панели, данные сервера) или (None, None)."""
    from database.requests import get_active_servers
    from bot.services.vpn_api import get_client_from_server_data

    servers = [s for s in get_active_servers() if int(s['id']) == server_id]
    if not servers:
        return None, None
    return get_client_from_server_data(servers[0]), servers[0]


def _format_bytes(value) -> str:
    try:
        num = float(value or 0)
    except (TypeError, ValueError):
        return '—'
    for unit in ('Б', 'КБ', 'МБ', 'ГБ', 'ТБ'):
        if num < 1024:
            return f'{num:.1f} {unit}'
        num /= 1024
    return f'{num:.1f} ПБ'


def _format_uptime(seconds) -> str:
    try:
        total = int(seconds or 0)
    except (TypeError, ValueError):
        return '—'
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f'{days} дн. {hours} ч.'
    if hours:
        return f'{hours} ч. {minutes} мин.'
    return f'{minutes} мин.'


@router.callback_query(F.data.startswith('admin_server_tools:'))
async def show_server_tools(callback: CallbackQuery):
    """Меню инструментов диагностики сервера."""
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    server_id = int(callback.data.split(':')[1])
    _, server = _get_panel_client(server_id)
    name = escape_html(str(server.get('name'))) if server else str(server_id)
    await safe_edit_or_send(
        callback.message,
        f'🛠 <b>Диагностика сервера</b>\n\n{name}\n\nВыберите инструмент:',
        reply_markup=server_tools_menu_kb(server_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith('admin_srv_logs:'))
async def show_xray_logs(callback: CallbackQuery):
    """Последние подключения по данным Xray."""
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    server_id = int(callback.data.split(':')[1])
    await callback.answer('📋 Загружаю логи...')

    client, _ = _get_panel_client(server_id)
    if not client:
        await safe_edit_or_send(callback.message, '⚠️ Сервер не найден.', reply_markup=server_tools_back_kb(server_id))
        return

    try:
        result = await client._request('POST', f'/panel/api/server/xraylogs/{LOGS_COUNT}')
        entries = result.get('obj') or []
    except Exception as e:
        logger.warning(f"Не удалось получить логи Xray сервера {server_id}: {e}")
        await safe_edit_or_send(
            callback.message,
            f'❌ Не удалось получить логи:\n<pre>{escape_html(str(e))[:400]}</pre>',
            reply_markup=server_tools_back_kb(server_id),
        )
        return

    if not entries:
        text = '📋 <b>Логи подключений</b>\n\nЗа последнее время записей нет.'
    else:
        lines = ['📋 <b>Последние подключения</b>\n']
        for entry in entries[:LOGS_COUNT]:
            raw_time = str(entry.get('DateTime') or '')
            try:
                time_str = datetime.fromisoformat(raw_time.replace('Z', '+00:00')).strftime('%H:%M:%S')
            except Exception:
                time_str = raw_time[:19]
            email = escape_html(str(entry.get('Email') or '—'))
            target = escape_html(str(entry.get('ToAddress') or '—'))
            outbound = escape_html(str(entry.get('Outbound') or '—'))
            lines.append(f'<code>{time_str}</code> <b>{email}</b>\n  → {target} ({outbound})')
        text = '\n'.join(lines)

    if len(text) > 3900:
        text = text[:3900] + '\n\n… список обрезан'

    await safe_edit_or_send(callback.message, text, reply_markup=server_tools_back_kb(server_id))


@router.callback_query(F.data.startswith('admin_srv_status:'))
async def show_server_status(callback: CallbackQuery):
    """Состояние сервера: CPU, память, аптайм."""
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    server_id = int(callback.data.split(':')[1])
    await callback.answer('📊 Запрашиваю состояние...')

    client, _ = _get_panel_client(server_id)
    if not client:
        await safe_edit_or_send(callback.message, '⚠️ Сервер не найден.', reply_markup=server_tools_back_kb(server_id))
        return

    try:
        result = await client._request('GET', '/panel/api/server/status')
        obj = result.get('obj') or {}
    except Exception as e:
        logger.warning(f"Не удалось получить статус сервера {server_id}: {e}")
        await safe_edit_or_send(
            callback.message,
            f'❌ Не удалось получить состояние:\n<pre>{escape_html(str(e))[:400]}</pre>',
            reply_markup=server_tools_back_kb(server_id),
        )
        return

    mem = obj.get('mem') or {}
    swap = obj.get('swap') or {}
    disk = obj.get('disk') or {}
    xray = obj.get('xray') or {}

    try:
        cpu_str = f"{float(obj.get('cpu') or 0):.1f}%"
    except (TypeError, ValueError):
        cpu_str = '—'

    lines = [
        '📊 <b>Состояние сервера</b>\n',
        f"🖥 CPU: {cpu_str} ({obj.get('cpuCores', '—')} ядер)",
        f"🧠 Память: {_format_bytes(mem.get('current'))} / {_format_bytes(mem.get('total'))}",
    ]
    if swap.get('total'):
        lines.append(f"💾 Swap: {_format_bytes(swap.get('current'))} / {_format_bytes(swap.get('total'))}")
    if disk.get('total'):
        lines.append(f"🗄 Диск: {_format_bytes(disk.get('current'))} / {_format_bytes(disk.get('total'))}")
    if obj.get('uptime'):
        lines.append(f"⏱ Аптайм: {_format_uptime(obj.get('uptime'))}")
    if xray:
        state = xray.get('state') or '—'
        version = xray.get('version') or '—'
        lines.append(f"⚙️ Xray: {escape_html(str(state))} (v{escape_html(str(version))})")

    await safe_edit_or_send(callback.message, '\n'.join(lines), reply_markup=server_tools_back_kb(server_id))


@router.callback_query(F.data.startswith('admin_srv_restart_ask:'))
async def ask_restart_xray(callback: CallbackQuery):
    """Запрашивает подтверждение перезапуска Xray."""
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    server_id = int(callback.data.split(':')[1])
    await safe_edit_or_send(
        callback.message,
        '⚠️ <b>Перезапустить Xray?</b>\n\n'
        'Все активные подключения клиентов кратковременно разорвутся '
        '(обычно приложения переподключаются автоматически за несколько секунд).',
        reply_markup=server_restart_confirm_kb(server_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith('admin_srv_restart_do:'))
async def restart_xray(callback: CallbackQuery):
    """Перезапускает Xray на сервере."""
    if not is_admin(callback.from_user.id):
        await callback.answer('⛔ Доступ запрещён', show_alert=True)
        return
    server_id = int(callback.data.split(':')[1])
    await callback.answer('🔄 Перезапускаю...')

    client, _ = _get_panel_client(server_id)
    if not client:
        await safe_edit_or_send(callback.message, '⚠️ Сервер не найден.', reply_markup=server_tools_back_kb(server_id))
        return

    try:
        await client._request('POST', '/panel/api/server/restartXrayService')
        logger.info(f"Админ {callback.from_user.id} перезапустил Xray на сервере {server_id}")
        await safe_edit_or_send(
            callback.message,
            '✅ Команда перезапуска Xray отправлена. Через несколько секунд сервис поднимется.',
            reply_markup=server_tools_back_kb(server_id),
        )
    except Exception as e:
        logger.warning(f"Не удалось перезапустить Xray на сервере {server_id}: {e}")
        await safe_edit_or_send(
            callback.message,
            f'❌ Не удалось перезапустить Xray:\n<pre>{escape_html(str(e))[:400]}</pre>',
            reply_markup=server_tools_back_kb(server_id),
        )
