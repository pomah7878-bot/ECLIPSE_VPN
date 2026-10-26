"""Быстрые проверки ядра: python3 -m unittest discover tests (из корня проекта, нужен config.py)."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_tmp = tempfile.mkdtemp()
os.environ["ECLIPSE_DB_PATH"] = os.path.join(_tmp, "t.db")  # до импорта database.*

from database.migrations import LATEST_VERSION, get_current_version, run_migrations  # noqa: E402
from database.db_licenses import tariff_final_price, tariff_term_savings  # noqa: E402
import database.db_settings as st  # noqa: E402
from database.db_business_operations import apply_balance_operation  # noqa: E402
from database.connection import get_db  # noqa: E402


class CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run_migrations()

    def test_migrations_reach_latest(self):
        self.assertEqual(get_current_version(), LATEST_VERSION)

    def test_tariff_prices(self):
        a = {"id": 1, "price_rub": 1000, "duration_days": 30}
        b = {"id": 2, "price_rub": 2400, "duration_days": 90, "discount_percent": 10}
        self.assertEqual(tariff_final_price(b), 2160)
        self.assertEqual(tariff_term_savings(a, [a, b]), 0)
        self.assertEqual(tariff_term_savings(b, [a, b]), 28)

    def test_balance(self):
        with get_db() as c:
            c.execute("INSERT INTO users (telegram_id, personal_balance) VALUES (777, 0)")
            uid = c.execute("SELECT id FROM users WHERE telegram_id = 777").fetchone()["id"]
        kw = dict(user_id=uid, source="test", reason="t")
        self.assertTrue(apply_balance_operation(operation_type="credit", cents=500, **kw)["ok"])
        self.assertEqual(apply_balance_operation(operation_type="debit", cents=900, **kw)["status"], "insufficient_funds")
        self.assertEqual(apply_balance_operation(operation_type="debit", cents=200, **kw)["balance_after"], 300)

    def test_license_found_by_order(self):
        from database.db_licenses import create_partner_license, find_partner_license_key_by_order
        key = create_partner_license("t", "", 30, notes="Куплено через бота, order_id=LIC_AB12, telegram_id=1")
        self.assertEqual(find_partner_license_key_by_order("LIC_AB12"), key)
        self.assertIsNone(find_partner_license_key_by_order("AB12"))      # чужой суффикс не совпадает
        self.assertIsNone(find_partner_license_key_by_order("LIC_AB1"))   # и чужой префикс тоже
        self.assertIsNone(find_partner_license_key_by_order("LIC_ZZ99"))

    def test_license_retry_does_not_issue_second_key(self):
        import asyncio
        from unittest import mock
        from database import db_licenses as L
        import bot.services.license_purchase as lp, bot.services.billing as bl
        tid = L.create_license_tariff("T", "", 1000, 30)
        L.create_license_purchase("ORD_RETRY", 5, tid)
        with get_db() as c:
            c.execute("UPDATE license_purchases SET yookassa_payment_id='yk' WHERE order_id='ORD_RETRY'")

        async def paid(*a, **k):
            return "succeeded"

        real, calls = L.complete_license_purchase, {"n": 0}

        def flaky(o, k):  # первый раз падает уже ПОСЛЕ создания ключа
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("db locked")
            return real(o, k)

        with mock.patch.object(bl, "check_yookassa_payment_status", paid), \
                mock.patch.object(L, "complete_license_purchase", flaky):
            s1, _ = asyncio.run(lp.issue_license_for_order("ORD_RETRY"))
            s2, info = asyncio.run(lp.issue_license_for_order("ORD_RETRY"))
        self.assertEqual((s1, s2), ("error", "issued"))
        with get_db() as c:
            n = c.execute("SELECT COUNT(*) FROM partner_licenses WHERE instr(notes, 'order_id=ORD_RETRY,') > 0").fetchone()[0]
        self.assertEqual(n, 1)

    def test_daily_tasks_catch_up(self):
        import asyncio, json
        from datetime import datetime
        import bot.services.scheduler as sc
        now = datetime.now()
        before = now.replace(hour=1, minute=0, second=0, microsecond=0)
        st.set_setting("daily_done", "")
        # первый запуск: ничего не догоняем, состояние создаётся
        self.assertFalse(sc._daily_pending(now, 0, 0))
        self.assertFalse(sc._daily_pending(now, 0, 0))
        # бот перезапустили посреди задач: выполнен только один шаг
        st.set_setting("daily_done", json.dumps({"date": now.strftime("%Y-%m-%d"), "steps": ["stats"]}))
        self.assertTrue(sc._daily_pending(now, 0, 0))
        self.assertFalse(sc._daily_pending(before, 3, 0))  # время запуска ещё не наступило
        # шаг с ключом выполняется один раз за день
        runs = []

        async def work():
            runs.append(1)

        asyncio.run(sc._run_daily_step("t", work(), "backup"))
        asyncio.run(sc._run_daily_step("t", work(), "backup"))
        self.assertEqual(len(runs), 1)
        for step in sc._DAILY_STEPS:
            sc._daily_mark(step)
        self.assertFalse(sc._daily_pending(now, 0, 0))
        # новый день — снова все шаги к выполнению
        st.set_setting("daily_done", json.dumps({"date": "2000-01-01", "steps": list(sc._DAILY_STEPS)}))
        self.assertTrue(sc._daily_pending(now, 0, 0))

    def test_session_revoked_by_logout(self):
        import asyncio, time
        from unittest import mock
        from bot.webapp import server as sv
        with mock.patch.object(sv, "_get_session_secret", return_value=b"k"):
            c = sv._sign_session(42)
            self.assertEqual(sv._verify_session(c), 42)
            st.set_setting("site_session_min_42", str(int(time.time()) + 5))
            st.invalidate_settings_cache()
            self.assertIsNone(sv._verify_session(c))

    def test_balance_parallel_debits(self):
        import threading
        with get_db() as c:
            c.execute("INSERT INTO users (telegram_id, personal_balance) VALUES (778, 1000)")
            uid = c.execute("SELECT id FROM users WHERE telegram_id = 778").fetchone()["id"]
        res = []
        def go():
            res.append(apply_balance_operation(user_id=uid, operation_type="debit", cents=300, source="t", reason="t")["ok"])
        ts = [threading.Thread(target=go) for _ in range(6)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertEqual(sum(res), 3)  # 1000 коп хватает ровно на три списания по 300

    def test_routing_does_not_freeze_fallback(self):
        # профиль из GitHub ещё не загружен: публикация не должна фиксировать запасной шаблон
        st._live_profile_cache["smart"].update(data=None, ts=1e18, busy=True)
        out = json.loads(st.prepare_routing_profile(st.get_default_happ_routing_profile_json("smart"), "happ", "smart"))
        self.assertNotIn("LastUpdated", out)
        self.assertEqual(st.get_setting("routing_published", ""), "")


if __name__ == "__main__":
    unittest.main()
