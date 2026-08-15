"""Runtime identity of the installed ECLIPSE Unlimited bot release."""

from __future__ import annotations

import logging
import re

from bot.utils.git_utils import run_git_command


logger = logging.getLogger(__name__)

UNKNOWN_BOT_VERSION = "unknown"
_RELEASE_PREFIX_RE = re.compile(
    r"^[!?]?\s*версия\s+([0-9]+(?:\.[0-9]+)*)\b",
    flags=re.IGNORECASE,
)


def parse_bot_release(commit_subject: str) -> str | None:
    """Extract a release number from the version prefix of a commit subject."""
    match = _RELEASE_PREFIX_RE.match((commit_subject or "").strip())
    return match.group(1) if match else None


def resolve_bot_version() -> tuple[str, str]:
    """Return ``(release, short_commit)`` for the current Git HEAD."""
    success, output = run_git_command(["log", "-1", "--format=%h%x09%s"])
    if not success or "\t" not in output:
        logger.warning("Cannot resolve ECLIPSE Unlimited bot release from Git HEAD")
        return UNKNOWN_BOT_VERSION, UNKNOWN_BOT_VERSION

    commit, subject = output.split("\t", 1)
    commit = commit.strip() or UNKNOWN_BOT_VERSION
    release = parse_bot_release(subject)
    if release is None:
        logger.warning(
            "Current Git commit subject has no ECLIPSE Unlimited release prefix: %s",
            subject[:160],
        )
        release = UNKNOWN_BOT_VERSION
    return release, commit


BOT_RELEASE, BOT_COMMIT = resolve_bot_version()


# ============================================================================
# Богатая информация о релизе для экрана "Обновление не требуется": ищет
# версию на HEAD, а если её там нет — идёт назад по истории до последнего
# коммита с маркером "Версия N.N". Использует существующий формат проекта:
# "[!?]Версия N.N: заголовок" + тело построчно "+ добавлено" / "- исправлено".
# ============================================================================

_RELEASE_HEADER_RE = re.compile(
    r"^([!?]?)\s*версия\s+([0-9]+(?:\.[0-9]+)*)\s*:?\s*(.*)$",
    flags=re.IGNORECASE,
)

_RELEASE_SEARCH_DEPTH = 300  # разумный предел обхода истории назад


def parse_release_body(body: str) -> dict | None:
    """Разбирает полное тело коммита-версии (git log --format=%B) на структуру
    {marker, version, title, bullets: [(kind, text), ...]}. kind — исходный
    символ строки ('+' или '-'); используется только как маркер начала пункта,
    а не как индикатор типа изменения (в истории проекта смысл +/- не всегда
    последователен). Возвращает None, если первая строка не похожа на версию."""
    lines = [line.rstrip() for line in (body or "").strip("\n").split("\n")]
    if not lines:
        return None
    header_match = _RELEASE_HEADER_RE.match(lines[0].strip())
    if not header_match:
        return None
    marker, version, title = header_match.groups()
    title = title.strip()
    bullets: list[tuple[str, str]] = []
    for line in lines[1:]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped[0] in "+-" and len(stripped) > 1:
            bullets.append((stripped[0], stripped[1:].strip()))
        elif bullets:
            prev_kind, prev_text = bullets[-1]
            bullets[-1] = (prev_kind, f"{prev_text} {stripped}")
        else:
            title = f"{title} {stripped}".strip() if title else stripped
    return {"marker": marker, "version": version, "title": title, "bullets": bullets}


def resolve_release_info(revision: str = "HEAD", max_search: int = _RELEASE_SEARCH_DEPTH):
    """Возвращает (release_info, extra_commits) для экрана статуса версии.

    revision — точка отсчёта: "HEAD" (текущий установленный код) или,
    например, "origin/main" (то, что подтянется при обновлении).

    Если сама revision размечена как версия — release_info о ней,
    extra_commits пуст. Иначе ищет назад по истории (не дальше max_search
    коммитов) последний коммит-версию; extra_commits — всё, что случилось
    после неё (новые сверху), как список (short_hash, subject).

    Возвращает (None, []) если версия не найдена вовсе или git недоступен.
    """
    ok, head_line = run_git_command(["log", "-1", "--format=%h%x09%s", revision])
    if not ok or "\t" not in head_line:
        return None, []
    head_short, head_subject = head_line.split("\t", 1)

    if _RELEASE_HEADER_RE.match(head_subject.strip()):
        ok_body, body = run_git_command(["log", "-1", "--format=%B", head_short])
        if ok_body:
            return parse_release_body(body), []
        return None, []

    ok_bulk, bulk = run_git_command(
        ["log", "--format=%h%x09%s", "-n", str(max_search), revision]
    )
    if not ok_bulk or not bulk:
        return None, []

    extra_commits: list[tuple[str, str]] = []
    for line in bulk.split("\n"):
        if "\t" not in line:
            continue
        short_hash, subject = line.split("\t", 1)
        if _RELEASE_HEADER_RE.match(subject.strip()):
            ok_body, body = run_git_command(["log", "-1", "--format=%B", short_hash])
            info = parse_release_body(body) if ok_body else None
            return info, extra_commits
        extra_commits.append((short_hash, subject))
    return None, extra_commits
