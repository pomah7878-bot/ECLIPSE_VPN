#!/usr/bin/env python3
"""Версия 1.309: кнопка CDN (обход белых списков) на экране выдачи ключа — в одной строке с AmneziaWG."""
import hashlib, shutil, sys
EDITS = [('bot/utils/action_registry.py', "    if not key_id:\n        return None\n    if _key_details_is_unconfigured(ctx) or not ctx.get('has_sub_id'):\n        return None\n    try:\n        from bot.services.cdn import is_cdn_offered_for_key\n", "    if not key_id:\n        return None\n    if 'has_sub_id' in ctx or 'is_unconfigured' in ctx:\n        # Карточка ключа: контекст уже содержит признаки ключа\n        if _key_details_is_unconfigured(ctx) or not ctx.get('has_sub_id'):\n            return None\n    else:\n        # v1.309: экран выдачи ключа (key_delivery) — признаков нет, берём ключ из БД\n        telegram_id = ctx.get('telegram_id')\n        if not telegram_id:\n            return None\n        try:\n            from database.requests import get_key_details_for_user\n            key = get_key_details_for_user(int(key_id), int(telegram_id))\n        except Exception:\n            return None\n        if not key or not key.get('sub_id') or not key.get('server_id'):\n            return None\n    try:\n        from bot.services.cdn import is_cdn_offered_for_key\n"), ('database/migrations.py', '\n# Current version of the database schema (incremented when new migrations are added)\nLATEST_VERSION = 151\n\nDEFAULT_BROADCAST_STYLE_PROFILE = {\n', '\n# Current version of the database schema (incremented when new migrations are added)\nLATEST_VERSION = 152\n\nDEFAULT_BROADCAST_STYLE_PROFILE = {\n'), ('database/migrations.py', '\n\nMIGRATIONS = {\n    74: migration_74,\n', '\n\ndef migration_152(conn: sqlite3.Connection) -> None:\n    """Версия 1.309: кнопка «🌐 CDN (обход белых списков)» на экране выдачи ключа\n    (key_delivery) — в одной строке с «🛡 AmneziaWG». Если строки с AmneziaWG нет или в ней\n    уже две кнопки — отдельной строкой над «Назад». Идемпотентно."""\n    row = conn.execute(\n        "SELECT buttons_default, buttons_custom FROM pages WHERE page_key = \'key_delivery\'"\n    ).fetchone()\n    if not row:\n        return\n    for column_index, column_name in ((0, "buttons_default"), (1, "buttons_custom")):\n        raw = row[column_index]\n        if not raw:\n            continue\n        try:\n            buttons = json.loads(raw)\n        except (TypeError, ValueError):\n            continue\n        if any(b.get("id") == "btn_key_cdn" for b in buttons):\n            continue\n        awg = next((b for b in buttons if b.get("id") == "btn_key_protocols"), None)\n        new_btn = {\n            "id": "btn_key_cdn", "label": "🌐 CDN (обход белых списков)", "color": "secondary",\n            "is_hidden": False, "action_type": "system", "action_value": None,\n        }\n        in_row = [b for b in buttons if awg is not None and b.get("row") == awg.get("row")]\n        if awg is not None and len(in_row) == 1:\n            new_btn["row"], new_btn["col"] = int(awg.get("row", 0)), int(awg.get("col", 0)) + 1\n        else:\n            back_row = next((b.get("row") for b in buttons if b.get("id") == "btn_key_delivery_back"), None)\n            if back_row is None:\n                back_row = max([int(b.get("row", 0)) for b in buttons] + [-1]) + 1\n            for b in buttons:\n                if int(b.get("row", 0)) >= int(back_row):\n                    b["row"] = int(b.get("row", 0)) + 1\n            new_btn["row"], new_btn["col"] = int(back_row), 0\n        buttons.append(new_btn)\n        conn.execute(\n            f"UPDATE pages SET {column_name} = ? WHERE page_key = \'key_delivery\'",\n            (json.dumps(buttons, ensure_ascii=False),)\n        )\n    logger.info("Migration v152 applied: btn_key_cdn добавлена на key_delivery")\n\n\nMIGRATIONS = {\n    74: migration_74,\n'), ('database/migrations.py', '    150: migration_150,\n    151: migration_151,\n}\n\n', '    150: migration_150,\n    151: migration_151,\n    152: migration_152,\n}\n\n')]
EXPECTED = {'bot/utils/action_registry.py': 'e21535efa8f73f53289b17048f96de53798a7f9de3b810480cf804f84eb1afe0', 'database/migrations.py': '2883fd983ca3242debcdfb28b68ddd53e991986f3e2c94d574988a9a1b6fbe71'}
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
