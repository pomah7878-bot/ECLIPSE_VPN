"""Версия в адресах статических JS/CSS: /static/x.js -> /static/x.js?v=<время изменения файла>.

Страницы отдаются без кэша, а файлы в /static браузер вправе держать часами (особенно мобильный
и если файл давно не менялся). Новый адрес заставляет скачать изменившийся файл сразу."""
import os
import re

_STATIC_URL_RE = re.compile(r'/static/([A-Za-z0-9_./-]+\.(?:js|css))"')


def version_static_urls(html: str, static_dir: str) -> str:
    """Добавляет ?v=<mtime> к ссылкам вида "/static/....js" и "/static/....css" в html.
    Файл не найден — ссылка остаётся как была."""
    def _add(m):
        try:
            v = int(os.path.getmtime(os.path.join(static_dir, *m.group(1).split("/"))))
        except OSError:
            return m.group(0)
        return f'/static/{m.group(1)}?v={v}"'

    return _STATIC_URL_RE.sub(_add, html)
