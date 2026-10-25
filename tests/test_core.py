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
