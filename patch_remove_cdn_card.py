#!/usr/bin/env python3
"""Версия 1.310: кнопка CDN убирается с карточки ключа (остаётся на экране выдачи ключа)."""
import hashlib, shutil, sys
EDITS = [('database/migrations.py', '\n# Current version of the database schema (incremented when new migrations are added)\nLATEST_VERSION = 152\n\nDEFAULT_BROADCAST_STYLE_PROFILE = {\n', '\n# Current version of the database schema (incremented when new migrations are added)\nLATEST_VERSION = 153\n\nDEFAULT_BROADCAST_STYLE_PROFILE = {\n'), ('database/migrations.py', '\n\nMIGRATIONS = {\n    74: migration_74,\n', '\n\ndef migration_153(conn: sqlite3.Connection) -> None:\n    """Версия 1.310: кнопка CDN остаётся только на экране выдачи ключа (key_delivery) —\n    с карточки ключа (key_details) убирается как дубль. Идемпотентно."""\n    row = conn.execute(\n        "SELECT buttons_default, buttons_custom FROM pages WHERE page_key = \'key_details\'"\n    ).fetchone()\n    if row:\n        for column_index, column_name in ((0, "buttons_default"), (1, "buttons_custom")):\n            raw = row[column_index]\n            if not raw:\n                continue\n            try:\n                buttons = json.loads(raw)\n            except (TypeError, ValueError):\n                continue\n            kept = [b for b in buttons if b.get("id") != "btn_key_cdn"]\n            if len(kept) != len(buttons):\n                conn.execute(\n                    f"UPDATE pages SET {column_name} = ? WHERE page_key = \'key_details\'",\n                    (json.dumps(kept, ensure_ascii=False),)\n                )\n    logger.info("Migration v153 applied: btn_key_cdn убрана с key_details")\n\n\nMIGRATIONS = {\n    74: migration_74,\n'), ('database/migrations.py', '    151: migration_151,\n    152: migration_152,\n}\n\n', '    151: migration_151,\n    152: migration_152,\n    153: migration_153,\n}\n\n')]
EXPECTED = {'database/migrations.py': 'a8b75e55498576b49693f0b7cc796146fc08f5a2b5c7a6d8a6b5fa8b4778896f'}
files = sorted({e[0] for e in EDITS})
for f in files:
    shutil.copy2(f, f + ".bak")
try:
    for f in files:
        s = open(f, encoding="utf-8").read()
        for ef, old, new in EDITS:
            if ef != f:
                continue
            if s.count(old) != 1:
                raise RuntimeError(f"{f}: фрагмент найден {s.count(old)} раз(а), ожидался 1")
            s = s.replace(old, new, 1)
        open(f, "w", encoding="utf-8").write(s)
        h = hashlib.sha256(s.encode("utf-8")).hexdigest()
        if h != EXPECTED[f]:
            raise RuntimeError(f"{f}: SHA256 не совпал ({h})")
except Exception as e:
    for f in files:
        shutil.move(f + ".bak", f)
    print("ОШИБКА, всё откачено:", e)
    sys.exit(1)
print("OK: патч применён, бэкапы *.bak рядом с файлами")
