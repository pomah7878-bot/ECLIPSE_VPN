"""Текст релиза из CHANGELOG.md: все версии после прошлого релиза до последней.

Использование: release_notes.py <прошлый_тег или ""> → печатает «тег\\n\\nтекст».
Пусто — релизить нечего (последняя версия не новее прошлого релиза)."""
import re
import sys

ENTRY = re.compile(r"^- \*\*(\d+)\.(\d+)\*\*")


def notes(changelog: str, last_tag: str):
    entries = []
    for line in changelog.splitlines():
        m = ENTRY.match(line)
        if m:
            entries.append(((int(m.group(1)), int(m.group(2))), line))
    if not entries:
        return None
    m = re.match(r"v?(\d+)\.(\d+)$", last_tag or "")
    last = (int(m.group(1)), int(m.group(2))) if m else None
    new = [(v, line) for v, line in entries if last is None or v > last]
    if not new:
        return None
    new.sort(reverse=True)
    top, low = new[0][0], new[-1][0]
    tag = "v%d.%d" % top
    head = ""
    if len(new) > 1:
        head = ("Сводный релиз: изменения с %d.%d по %d.%d "
                "(отдельные релизы для этих версий не публиковались).\n\n" % (low + top))
    return tag, head + "\n".join(line for _, line in new)


if __name__ == "__main__":
    with open("CHANGELOG.md", encoding="utf-8") as f:
        res = notes(f.read(), sys.argv[1] if len(sys.argv) > 1 else "")
    if res:
        print(res[0] + "\n\n" + res[1])
