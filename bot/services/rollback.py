"""Откат версии бота и восстановление базы из локального бэкапа (админка → Обновление → «Откат и восстановление»).

Откат кода: перед каждым обновлением бот запоминает прежний коммит (последние 3). Откатиться можно
только на запомненный коммит. Перед откатом делается копия базы. База при откате кода НЕ меняется:
миграции только добавляют данные, старый код с новой базой работает.

Восстановление базы: из файлов backup/<дата>/vpn_bot.db и ручных копий *__database__vpn_bot.db.
Перед заменой делается копия текущей базы, выбранный файл проверяется (целостность, таблица users).
"""
import hashlib
import json
import logging
import os
import sqlite3
import time
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKUP_DIR = os.path.join(PROJECT_ROOT, "backup")
POINTS_FILE = os.path.join(BACKUP_DIR, "rollback_points.json")
KEEP_POINTS = 3
MAX_DB_LIST = 10


def _read_points() -> List[Dict]:
    try:
        with open(POINTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return [p for p in data if isinstance(p, dict) and p.get("commit")]
    except (OSError, ValueError):
        return []


def remember(commit: Optional[str]) -> None:
    """Запоминает коммит, на котором бот работал до обновления. Никогда не мешает обновлению."""
    try:
        if not commit:
            return
        points = [p for p in _read_points() if p["commit"] != commit]
        points.insert(0, {"commit": commit, "ts": int(time.time())})
        os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
        tmp = POINTS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(points[:KEEP_POINTS], f)
        os.replace(tmp, POINTS_FILE)
    except Exception as e:  # noqa: BLE001
        logger.warning("rollback.remember: %s", e)


def list_points() -> List[Dict]:
    """Точки отката без текущего коммита и без исчезнувших из репозитория: commit, ts, subject."""
    from bot.utils.git_utils import get_current_commit, run_git_command
    current = get_current_commit()
    out = []
    for p in _read_points():
        sha = p["commit"]
        if sha == current:
            continue
        ok, subject = run_git_command(["log", "-1", "--format=%s", sha])
        if ok and subject.strip():
            out.append({"commit": sha, "ts": p.get("ts", 0), "subject": subject.strip()})
    return out


def snapshot_db() -> str:
    from database.db_backup import create_bot_database_backup
    return create_bot_database_backup()


def rollback_code(sha: str) -> Tuple[bool, str]:
    """Откат на запомненный коммит. Если код не проходит проверку импорта, возвращает всё обратно."""
    from bot.utils.git_utils import _validate_pulled_code, get_current_commit, run_git_command
    if sha not in {p["commit"] for p in list_points()}:
        return False, "Эта версия не в списке отката"
    current = get_current_commit()
    try:
        snapshot_db()
    except Exception as e:  # noqa: BLE001
        return False, f"Не удалось сделать копию базы, откат отменён: {e}"
    ok, out = run_git_command(["reset", "--hard", sha], timeout=120)
    if not ok:
        return False, f"Ошибка отката: {out}"
    valid, err = _validate_pulled_code()
    if not valid:
        if current:
            run_git_command(["reset", "--hard", current], timeout=120)
        return False, f"Старая версия не прошла проверку и не установлена, возвращена текущая: {err}"
    remember(current)
    return True, "ok"


def _fingerprint(rel: str) -> str:
    return hashlib.sha1(rel.encode("utf-8")).hexdigest()[:8]


def list_db_backups() -> List[Dict]:
    """Локальные копии базы бота, свежие сверху: id, rel, mtime, size."""
    found = []
    try:
        for name in os.listdir(BACKUP_DIR):
            path = os.path.join(BACKUP_DIR, name)
            if os.path.isdir(path):
                cand = os.path.join(path, "vpn_bot.db")
            elif name.endswith("__database__vpn_bot.db"):
                cand = path
            else:
                continue
            if os.path.isfile(cand) and os.path.getsize(cand) > 0:
                rel = os.path.relpath(cand, BACKUP_DIR)
                found.append({"id": _fingerprint(rel), "rel": rel,
                              "mtime": int(os.path.getmtime(cand)), "size": os.path.getsize(cand)})
    except OSError:
        return []
    found.sort(key=lambda x: x["mtime"], reverse=True)
    return found[:MAX_DB_LIST]


def check_db_file(path: str) -> Tuple[bool, str]:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
            if not row or row[0] != "ok":
                return False, "файл повреждён"
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone():
                return False, "это не база бота (нет таблицы users)"
        finally:
            conn.close()
    except sqlite3.Error as e:
        return False, f"не открывается как база: {e}"
    return True, "ok"


def restore_db(backup_id: str) -> Tuple[bool, str]:
    """Заменяет рабочую базу выбранной копией. После успеха нужен перезапуск бота."""
    from database import connection as db_connection
    item = next((b for b in list_db_backups() if b["id"] == backup_id), None)
    if not item:
        return False, "Копия не найдена в списке"
    src = os.path.realpath(os.path.join(BACKUP_DIR, item["rel"]))
    if not src.startswith(os.path.realpath(BACKUP_DIR) + os.sep):
        return False, "Некорректный путь"
    ok, why = check_db_file(src)
    if not ok:
        return False, f"Копия не подходит: {why}"
    try:
        snapshot_db()
    except Exception as e:  # noqa: BLE001
        return False, f"Не удалось сохранить текущую базу, восстановление отменено: {e}"
    live = str(db_connection.DB_PATH)
    tmp = live + ".restore.tmp"
    try:
        os.unlink(tmp) if os.path.exists(tmp) else None
        s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        d = sqlite3.connect(tmp)
        try:
            s.backup(d)
        finally:
            d.close()
            s.close()
        ok, why = check_db_file(tmp)
        if not ok:
            os.unlink(tmp)
            return False, f"Копия не подходит: {why}"
        for suffix in ("-wal", "-shm"):
            if os.path.exists(live + suffix):
                os.unlink(live + suffix)
        os.replace(tmp, live)
    except Exception as e:  # noqa: BLE001
        logger.error("restore_db: %s", e)
        return False, f"Ошибка восстановления: {e}"
    return True, "ok"
