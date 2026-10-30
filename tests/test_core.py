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

    def test_set_setting_skips_unchanged(self):
        for key in ("t_plain", "t_user:1"):
            with self.assertLogs("database.db_settings", "INFO") as cm:
                st.set_setting(key, "a")
                st.set_setting(key, "a")
            self.assertEqual(len(cm.output), 1)
            with self.assertLogs("database.db_settings", "INFO"):
                st.set_setting(key, "b")
            self.assertEqual(st.get_setting(key), "b")

    def test_roulette_recent_spins(self):
        from database.db_roulette import get_recent_spins
        from bot.handlers.user.roulette import _spin_line
        with get_db() as c:
            c.execute("INSERT INTO users (telegram_id, username) VALUES (9777, 'win_user')")
            uid = c.execute("SELECT id FROM users WHERE telegram_id = 9777").fetchone()["id"]
            c.execute("INSERT INTO roulette_spins (user_id, telegram_id, tier, prize_type, prize_amount, status) "
                      "VALUES (?, 9777, 'pair', 'days', 2, 'done')", (uid,))
            c.execute("INSERT INTO roulette_spins (user_id, telegram_id, tier, prize_type, prize_amount, status) "
                      "VALUES (?, 9777, 'jackpot', 'days', 7, 'failed')", (uid,))
        rows = get_recent_spins(10)
        self.assertEqual(rows[0]["status"], "failed")
        self.assertEqual(rows[0]["username"], "win_user")
        bad = get_recent_spins(10, only_problem=True)
        self.assertTrue(bad and all(r["status"] != "done" for r in bad))
        self.assertIn("@win_user", _spin_line(rows[1]))
        self.assertIn("+2 дня", _spin_line(rows[1]))
        self.assertIn("не начислен", _spin_line(rows[0]))

    def test_bonus_claims_recent(self):
        from database.db_bonus_tasks import list_recent
        from bot.handlers.user.bonus_tasks import _claim_line
        with get_db() as c:
            c.execute("INSERT INTO users (telegram_id, username) VALUES (9778, 'sharer')")
            uid = c.execute("SELECT id FROM users WHERE telegram_id = 9778").fetchone()["id"]
            c.execute("INSERT INTO bonus_task_claims (user_id, telegram_id, task, status, file_id, days) "
                      "VALUES (?, 9778, 'story', 'approved', 'f', 7)", (uid,))
            c.execute("INSERT INTO bonus_task_claims (user_id, telegram_id, task, status, file_id, days) "
                      "VALUES (?, 9778, 'review', 'pending', 'f', 7)", (uid,))
        rows = list_recent(10)
        self.assertEqual(rows[0]["task"], "review")
        self.assertEqual(rows[0]["username"], "sharer")
        self.assertIn("на проверке", _claim_line(rows[0]))
        self.assertIn("+7 дней", _claim_line(rows[1]))

    def test_roulette_button_hidden_for_admin(self):
        import bot.handlers.user.roulette as rl
        orig_enabled, orig_admin = rl.is_enabled, rl._is_admin
        try:
            rl.is_enabled = lambda: True
            rl._is_admin = lambda uid: uid == 1
            self.assertFalse(rl.can_spin(1))
        finally:
            rl.is_enabled, rl._is_admin = orig_enabled, orig_admin

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



class NewMenuLookTest(unittest.TestCase):
    def test_auto_style(self):
        from aiogram.types import InlineKeyboardButton as B
        from bot.services.menu_style import auto_style
        self.assertEqual(auto_style(B(text="💳 Оплатить", callback_data="x")), "success")
        self.assertEqual(auto_style(B(text="🗑 Удалить ключ", callback_data="x")), "danger")
        self.assertEqual(auto_style(B(text="🔑 Мои ключи", callback_data="my_keys")), "primary")
        self.assertEqual(auto_style(B(text="✅ Да, удалить", callback_data="confirm_delete_key:1")), "danger")
        self.assertIsNone(auto_style(B(text="❌ Нет", callback_data="cancel")))
        self.assertIsNone(auto_style(B(text="⬅️ Назад", callback_data="start")))
        self.assertIsNone(auto_style(B(text="Оплатить", callback_data="x", style="danger")))

    def test_table(self):
        from bot.utils.rich_view import table
        h = table("Т", [("a", "b")], header=("x", "y"))
        self.assertIn("<caption>Т</caption>", h)
        self.assertIn("<td>a</td><td>b</td>", h)

    def test_auto_table(self):
        from bot.utils.rich_view import auto_table
        h = auto_table("💰 <b>Баланс</b>\n<b>Остаток:</b> 120 ₽\n🔑 Ключей: <b>3</b>\n• Реферал: 5")
        self.assertIn("<caption>💰 Баланс</caption>", h)
        self.assertIn("<td>Реферал</td><td>5</td>", h)
        self.assertIsNone(auto_table("Длинный обычный текст.\nБез пар вида ключ значение."))
        self.assertIsNone(auto_table('A: 1\nB: 2\nC: <a href="x">3</a>'))

    def test_main_subscription_table(self):
        from bot.services import main_screen as m
        st = {'audience': m.AUDIENCE_CLIENT, 'keys': [], 'balance_cents': 50000,
              'active': [{'expires_at': '2054-11-24 00:00:00', 'tariff_id': None}]}
        h = m.build_subscription_html(st, "Ира")
        self.assertIn("<caption>Ира, всё работает 👋</caption>", h)
        self.assertIn("Баланс", h)
        self.assertIsNone(m.build_subscription_html({'audience': m.AUDIENCE_NEW}, "x"))

if __name__ == "__main__":
    unittest.main()


class WebLogoTest(unittest.TestCase):
    def test_web_logo_is_small_and_rebuilt_only_when_source_newer(self):
        import tempfile
        from PIL import Image
        from bot.utils.web_logo import build_web_logo

        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(build_web_logo(d))  # логотипа нет — ничего не создаём
            Image.effect_noise((512, 512), 80).convert("RGBA").save(os.path.join(d, "logo.png"))
            self.assertTrue(build_web_logo(d))
            out = os.path.join(d, "logo-web.png")
            with Image.open(out) as im:
                self.assertLessEqual(max(im.size), 256)
            self.assertLess(os.path.getsize(out), os.path.getsize(os.path.join(d, "logo.png")))
            m = os.path.getmtime(out)
            self.assertTrue(build_web_logo(d))
            self.assertEqual(os.path.getmtime(out), m)  # свежая копия не пересоздаётся


class WebAuthAndLimiterTests(unittest.TestCase):
    TOKEN = "123456:TEST-TOKEN"

    def _signed(self, uid, age_seconds=0, token=None):
        import hashlib, hmac, time, urllib.parse
        fields = {"auth_date": str(int(time.time()) - age_seconds),
                  "user": json.dumps({"id": uid, "first_name": "T"}, separators=(",", ":"))}
        check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        secret = hmac.new(b"WebAppData", (token or self.TOKEN).encode(), hashlib.sha256).digest()
        fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        return urllib.parse.urlencode(fields)

    def test_init_data_accepts_only_fresh_correctly_signed(self):
        from bot.webapp.server import _validate_init_data as v
        self.assertEqual(v(self._signed(42), self.TOKEN), 42)
        self.assertIsNone(v(self._signed(42, token="1:OTHER"), self.TOKEN))        # подпись чужим токеном
        self.assertIsNone(v(self._signed(42).replace("%22id%22%3A42", "%22id%22%3A43"), self.TOKEN))  # подмена id
        self.assertIsNone(v(self._signed(42, age_seconds=3 * 86400), self.TOKEN))  # старше 48 часов

    def test_rate_limiter_counts_window_and_forgets_empty_keys(self):
        from bot.webapp import ratelimit as rl
        rl._BUCKETS.clear()
        for _ in range(3):
            self.assertTrue(rl.rl_allowed("t:1", 3, 60))
            rl.rl_record("t:1")
        self.assertFalse(rl.rl_allowed("t:1", 3, 60))
        self.assertTrue(rl.rl_allowed("t:2", 3, 60))      # другой ключ не затронут
        self.assertTrue(rl.rl_allowed("t:1", 3, 0.0))     # окно прошло — записи не считаются
        self.assertNotIn("t:1", rl._BUCKETS)              # и пустой ключ удалён, не копится

    def test_rate_limiter_cleanup_keeps_entries_needed_by_day_windows(self):
        import time
        from bot.webapp import ratelimit as rl
        rl._BUCKETS.clear()
        rl._BUCKETS["day:1"] = [time.time() - 7200]                  # 2 часа назад, окно — сутки
        for i in range(rl._MAX_KEYS + 1):
            rl._BUCKETS[f"x:{i}"] = []
        rl.rl_allowed("probe", 1, 60)                                  # запускает общую чистку
        self.assertIn("day:1", rl._BUCKETS)                            # суточный лимит не потерян


class CustomWebhookAmountTests(unittest.TestCase):
    def _run(self, webhook_result, order):
        import asyncio
        from unittest import mock
        import bot.services.custom_payments as cp

        done = []

        async def fake_complete(order_id, **kw):
            done.append(order_id)
            return {"ok": True, "processed_now": True}

        async def fake_handler(provider_id, ctx):
            return webhook_result

        with mock.patch.object(cp, "get_payment_provider", lambda pid: mock.Mock(provider_id="p")), \
                mock.patch.object(cp, "handle_payment_webhook", fake_handler), \
                mock.patch.object(cp, "_find_provider_order_for_webhook", lambda *a: {"order_id": "O1", "provider_id": "p"}), \
                mock.patch.object(cp, "find_order_by_order_id", lambda oid: order), \
                mock.patch.object(cp, "update_payment_provider_order_status", lambda *a, **k: True), \
                mock.patch.object(cp, "update_payment_auto_check", lambda *a, **k: True), \
                mock.patch.object(cp, "complete_custom_payment_order", fake_complete):
            result = asyncio.run(cp.process_custom_payment_webhook("p", {}))
        return result, done

    def test_underpaid_order_is_not_completed(self):
        res, done = self._run({"ignored": False, "status": "succeeded", "amount_cents": 5000}, {"final_amount_cents": 10000})
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "amount_mismatch")
        self.assertEqual(done, [])

    def test_full_or_missing_amount_is_completed(self):
        for amount in (10000, 12000, None):  # ровно, с переплатой и без суммы (старые расширения)
            res, done = self._run({"ignored": False, "status": "succeeded", "amount_cents": amount}, {"final_amount_cents": 10000})
            self.assertTrue(res["ok"], amount)
            self.assertEqual(done, ["O1"], amount)

    def test_webhook_rejects_non_integer_amount(self):
        from bot.utils.payment_provider_registry import _normalize_webhook_result as n
        self.assertEqual(n({"status": "succeeded", "order_id": "O1", "amount_cents": 500})["amount_cents"], 500)
        for bad in ("500", 5.5, -1, True):
            with self.assertRaises(ValueError):
                n({"status": "succeeded", "order_id": "O1", "amount_cents": bad})


class AutoRenewSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run_migrations()

    def test_due_keys_are_only_enabled_unbanned_and_expiring_within_a_day(self):
        from database.db_keys import get_keys_due_for_auto_renewal
        with get_db() as c:
            tid = c.execute("INSERT INTO tariffs (name, duration_days, price_cents, price_stars, price_rub) "
                            "VALUES ('AR', 30, 100, 1, 100)").lastrowid
            ids = {}
            for name, tg, banned, auto, when in (
                    ("soon", 9101, 0, 1, "+2 hours"),       # подходит
                    ("expired", 9102, 0, 1, "-3 days"),     # уже истёк, но не обработан — подходит
                    ("far", 9103, 0, 1, "+5 days"),         # ещё рано
                    ("off", 9104, 0, 0, "+2 hours"),        # автопродление выключено
                    ("banned", 9105, 1, 1, "+2 hours")):    # клиент заблокирован
                uid = c.execute("INSERT INTO users (telegram_id, is_banned) VALUES (?, ?)", (tg, banned)).lastrowid
                ids[name] = c.execute(
                    "INSERT INTO vpn_keys (user_id, tariff_id, expires_at, auto_renew) "
                    "VALUES (?, ?, datetime('now', ?), ?)", (uid, tid, when, auto)).lastrowid
        due = {k["id"] for k in get_keys_due_for_auto_renewal(hours=24)}
        self.assertIn(ids["soon"], due)
        self.assertIn(ids["expired"], due)
        for name in ("far", "off", "banned"):
            self.assertNotIn(ids[name], due, name)


class DomainProvisionFallbackTests(unittest.TestCase):
    def _run(self, cf_result):
        import asyncio
        from bot.services import domain_provisioning as dp

        async def fake_ip():
            return "203.0.113.5"

        async def fake_cf(token, domain, ip):
            return cf_result

        async def fake_wait(domain, ip, timeout=240, interval=5):
            return False  # дальше DNS не идём: проверяем только развилку

        saved = (dp.get_server_public_ip, dp.cloudflare_create_or_update_a_record, dp.wait_for_dns_propagation)
        dp.get_server_public_ip, dp.cloudflare_create_or_update_a_record, dp.wait_for_dns_propagation = fake_ip, fake_cf, fake_wait
        try:
            async def collect():
                return [s async for s in dp._provision_backup_domain_impl("shop.example.com", "tok", 1)]
            return asyncio.run(collect())
        finally:
            dp.get_server_public_ip, dp.cloudflare_create_or_update_a_record, dp.wait_for_dns_propagation = saved

    def test_zone_not_found_falls_back_to_manual_record(self):
        from bot.services.domain_provisioning import ZONE_NOT_FOUND_PREFIX
        steps = self._run((False, ZONE_NOT_FOUND_PREFIX + " для домена shop.example.com"))
        self.assertTrue(any("Значение: 203.0.113.5" in s for s in steps))
        self.assertTrue(any("Проверяю распространение DNS" in s for s in steps))

    def test_token_error_still_stops(self):
        steps = self._run((False, "Cloudflare отклонил запрос (проверьте токен): bad"))
        self.assertFalse(any("Проверяю распространение DNS" in s for s in steps))
        self.assertTrue(steps[-1].startswith("❌"))


class TuicFallbackTests(unittest.TestCase):
    CLASH = """
proxies:
- alpn:
  - h3
  congestion-controller: bbr
  name: "Gaming & Calls FI"
  password: pw-1
  port: 19817
  reduce-rtt: true
  server: a.example.com
  type: tuic
  udp-relay-mode: native
  uuid: 11111111-1111-1111-1111-111111111111
- alpn:
  - h3
  congestion-controller: bbr
  name: Gaming DE
  password: p:w@2
  port: 10411
  server: b.example.com
  sni: sni.example.com
  type: tuic
  udp-relay-mode: native
  uuid: 22222222-2222-2222-2222-222222222222
- name: vless-one
  type: vless
  server: c.example.com
  port: 443
"""

    def test_links_from_clash(self):
        from bot.services.tuic_fallback import tuic_links_from_clash
        from bot.utils.inbound_links import parse_inbound_link
        links = tuic_links_from_clash(self.CLASH)
        self.assertEqual(len(links), 2)
        first = parse_inbound_link(links[0])
        self.assertEqual((first["protocol"], first["host"], first["port"]), ("tuic", "a.example.com", 19817))
        self.assertEqual(first["name"], "Gaming & Calls FI")
        self.assertIn("sni=a.example.com", links[0])   # sni нет в записи → домен сервера
        self.assertIn("sni=sni.example.com", links[1])
        self.assertIn("p%3Aw%402", links[1])           # спецсимволы пароля экранированы
        self.assertEqual(tuic_links_from_clash("не yaml: [ "), [])

    def test_merge_adds_only_when_missing(self):
        import asyncio
        import base64
        from bot.services import tuic_fallback as fb

        calls = []

        async def fake_fetch(client, sub_id, headers=None):
            calls.append(sub_id)
            return ["tuic://u:p@h.example.com:1?alpn=h3#n"]

        class FakeClient:
            server_id = 1

        saved = fb.fetch_tuic_links
        fb.fetch_tuic_links = fake_fetch
        try:
            without = "vless://x@h.example.com:443#a\n\nvpn://abc"
            out = asyncio.run(fb.add_missing_tuic_text(without, FakeClient(), "s1"))
            self.assertIn("tuic://u:p@h.example.com:1", out)
            self.assertNotIn("\n\n", out)
            already = "vless://x@h.example.com:443#a\ntuic://u:p@h.example.com:2#b"
            self.assertEqual(asyncio.run(fb.add_missing_tuic_text(already, FakeClient(), "s2")), already)
            self.assertEqual(calls, ["s1"])  # при наличии tuic:// панель не дёргаем
        finally:
            fb.fetch_tuic_links = saved


class FailoverTests(unittest.TestCase):
    def test_backup_used_only_when_down_and_backup_ok(self):
        from unittest import mock
        from bot.services import failover
        store = {}
        with mock.patch.object(failover, "get_setting", lambda k, d=None: store.get(k, d)), \
             mock.patch.object(failover, "set_setting", lambda k, v: store.__setitem__(k, v)), \
             mock.patch("database.db_servers.get_server_by_id", lambda i: {"id": i, "is_active": 1}):
            self.assertIsNone(failover.active_backup_for(1))
            failover.set_backup(1, 2)
            self.assertIsNone(failover.active_backup_for(1))       # резерв есть, узел работает
            failover.set_down(1, True)
            self.assertEqual(failover.active_backup_for(1), 2)
            failover.set_backup(1, 1)
            self.assertIsNone(failover.active_backup_for(1))       # сам себе резерв нельзя
            failover.set_backup(1, 2)
            failover.set_down(2, True)
            self.assertIsNone(failover.active_backup_for(1))       # резерв тоже недоступен
            failover.set_down(1, False)
            self.assertFalse(failover.is_down(1))


class NodeFailoverTests(unittest.IsolatedAsyncioTestCase):
    def _client(self, store, corrupt=False):
        class C:
            async def _request(self, method, path, data=None, **kw):
                if method == "GET":
                    return {"obj": [dict(i) for i in store]}
                ib = next(i for i in store if i["id"] == int(path.rsplit("/", 1)[1]))
                ib.update(data)
                if corrupt:
                    ib["port"] = 1
                return {}
        return C()

    async def _run(self, exclude, store, corrupt=False, saved=None):
        from unittest import mock
        from bot.services import node_failover as nf
        mem = {} if saved is None else saved
        client = self._client(store, corrupt)

        async def get_client(_):
            return client
        with mock.patch.object(nf, "get_setting", lambda k, d=None: mem.get(k, d)), \
             mock.patch.object(nf, "set_setting", lambda k, v: mem.__setitem__(k, v)), \
             mock.patch("bot.services.vpn_api.get_client", get_client):
            return await nf.set_node_excluded(1, 4, exclude), mem

    def _store(self):
        base = {"port": 10, "protocol": "vless", "remark": "x", "settings": "{}", "streamSettings": "{}",
                "sniffing": "{}", "listen": "", "tag": "t", "total": 0, "expiryTime": 0, "up": 5, "down": 6}
        return [dict(base, id=1, nodeId=4, enable=True, excludeFromSub=False),
                dict(base, id=2, nodeId=4, enable=False, excludeFromSub=False),   # выключен вручную
                dict(base, id=3, nodeId=7, enable=True, excludeFromSub=False)]    # другая нода

    async def test_only_enabled_inbounds_of_node_and_restore(self):
        store = self._store()
        (n, err), mem = await self._run(True, store)
        self.assertEqual((n, err), (1, ""))
        self.assertEqual([i["excludeFromSub"] for i in store], [True, False, False])
        self.assertEqual(store[0]["up"], 5)  # счётчики трафика не сброшены
        (n, err), mem = await self._run(False, store, saved=mem)
        self.assertEqual((n, err), (1, ""))
        self.assertEqual([i["excludeFromSub"] for i in store], [False, False, False])

    async def test_rollback_when_panel_changes_other_fields(self):
        store = self._store()
        (n, err), _ = await self._run(True, store, corrupt=True)
        self.assertEqual(n, 0)
        self.assertIn("изменились поля", err)


class ParkedNodeMonitoringTests(unittest.TestCase):
    def test_parked_node_is_not_a_problem(self):
        from bot.services import admin_monitoring as am
        node = am._normalize_node({"id": 4, "name": "N", "status": "offline", "enable": True})
        self.assertTrue(node["problem"])
        node.update(parked=True, problem=False, severity=0)
        self.assertIsNone(am._node_problem_line({"name": "S"}, node))
        self.assertEqual(am._node_icon(node), "🚧")
        self.assertIn("в ремонте", am._format_node_line(node, "└"))


class RepairSkipTests(unittest.TestCase):
    def test_repair_icon_gives_no_alerts(self):
        from bot.services import health_alerts as ha
        state = {}
        for _ in range(ha.FAIL_THRESHOLD + 2):
            ev = ha.evaluate([("🚧", "Панель X", "в ремонте")], state, 1000.0)
            self.assertEqual(ev, [])
        self.assertNotIn("Панель X", state)


class BlockCheckTests(unittest.TestCase):
    SSE = (
        'event:started\ndata:{"online_probes":3,"target":"192.0.2.1"}\n\n:\n'
        'event:result\ndata:{"region":"Москва","verdicts":["tspu_block"]}\n\n'
        'event:result\ndata:{"region":"Москва","verdicts":["tspu_block"]}\n\n'
        'event:result\ndata:{"region":"Пермский край","verdicts":["uncertain"]}\n\n'
        'event:result\ndata:{"region":"Тюмень","verdicts":["ok"]}\n\n'
        'event:done\ndata:{"status":"done"}\n\n'
    )

    def test_parse_and_summary(self):
        from bot.services import block_check as bc
        res = bc.summarize(bc.parse_sse(self.SSE))
        self.assertEqual((res["total"], res["blocked"], res["uncertain"], res["ok"]), (4, 2, 1, 1))
        self.assertEqual(res["regions"], ["Москва"])
        icon, text = bc.verdict_line(res)
        self.assertEqual(icon, "🔴")
        self.assertIn("2 из 4", text)

    def test_clean_cdn_and_error(self):
        from bot.services import block_check as bc
        clean = bc.summarize(bc.parse_sse('event:result\ndata:{"verdicts":["ok"]}\n\n'))
        self.assertEqual(bc.verdict_line(clean)[0], "🟢")
        self.assertEqual(bc.verdict_line(dict(clean, cdn=True))[0], "⚪")
        self.assertEqual(bc.verdict_line({"error": "x"})[0], "⚪")


class BlockDiagnosticsTests(unittest.TestCase):
    def test_lines_from_cache_without_network(self):
        import time
        from unittest import mock
        from bot.services import block_check as bc
        with mock.patch.object(bc, "licensed", lambda: True), mock.patch.object(bc, "auto_enabled", lambda: True), \
             mock.patch.dict(bc._sched, {"ts": time.time() - 125, "checks": [("🔴", "Блокировка: Узел <A>", "заблокирован в 25 из 35 регионов")]}):
            text = "\n".join(bc.diagnostics_lines())
        self.assertIn("🔴 Узел &lt;A&gt;: заблокирован в 25 из 35", text)
        self.assertIn("Проверено 2 мин назад", text)
        with mock.patch.object(bc, "licensed", lambda: False):
            self.assertEqual(bc.diagnostics_lines(), [])


class ButtonIconsTest(unittest.TestCase):
    def test_style_markup(self):
        from aiogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup as M
        from bot.services import button_icons as bi
        m = M(inline_keyboard=[[B(text='🔑 Мои ключи', callback_data='a'), B(text='🟢 Вкл', callback_data='b')],
                               [B(text='🌐 Кабинет', url='https://example.com'), B(text='Без', callback_data='c')]])
        out = bi.style_markup(m, {'🔑': '111', '🌐': '222'})
        r = out.inline_keyboard
        self.assertEqual((r[0][0].text, r[0][0].icon_custom_emoji_id), ('Мои ключи', '111'))
        self.assertEqual(r[0][1].text, '🟢 Вкл')
        self.assertEqual(r[1][0].icon_custom_emoji_id, '222')
        self.assertIs(bi.style_markup(m, {}), m)
        self.assertEqual(bi.parse_pack_name('https://t.me/addemoji/Ab_1'), 'Ab_1')

    def test_coverage(self):
        from bot.services import button_icons as bi
        self.assertTrue(all("\ufe0f" not in e for e in bi.MENU_EMOJI))
        have, missing = bi.coverage({"🔑": "1", "⬅": "2"})
        self.assertEqual(have, 2)
        self.assertNotIn("🔑", missing)


class RollbackTest(unittest.TestCase):
    def test_points_and_db_backups(self):
        import os, sqlite3, tempfile
        from bot.services import rollback as rb
        old = (rb.BACKUP_DIR, rb.POINTS_FILE)
        with tempfile.TemporaryDirectory() as d:
            rb.BACKUP_DIR, rb.POINTS_FILE = d, os.path.join(d, "p.json")
            try:
                for c in ("a1", "b2", "c3", "d4", "a1"):
                    rb.remember(c)
                rb.remember(None)
                self.assertEqual([p["commit"] for p in rb._read_points()], ["a1", "d4", "c3"])
                day = os.path.join(d, "2026-01-01"); os.makedirs(day)
                good = os.path.join(day, "vpn_bot.db")
                con = sqlite3.connect(good); con.execute("create table users(id int)"); con.commit(); con.close()
                bad = os.path.join(d, "x__database__vpn_bot.db")
                con = sqlite3.connect(bad); con.execute("create table t(id int)"); con.commit(); con.close()
                self.assertTrue(rb.check_db_file(good)[0])
                self.assertFalse(rb.check_db_file(bad)[0])
                with open(os.path.join(d, "junk__database__vpn_bot.db"), "wb") as f:
                    f.write(b"not a db")
                self.assertFalse(rb.check_db_file(os.path.join(d, "junk__database__vpn_bot.db"))[0])
                self.assertEqual(len(rb.list_db_backups()), 3)
                self.assertEqual(rb.restore_db("nope")[0], False)
                self.assertEqual(rb.rollback_code("not-in-list")[0], False)
            finally:
                rb.BACKUP_DIR, rb.POINTS_FILE = old


class LapsedCouponsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run_migrations()

    def test_candidates_and_no_repeat(self):
        from bot.services import lapsed_coupons as lc
        with get_db() as c:
            ids = {}
            for tg, days in ((9801, 10), (9802, 2), (9803, 200)):
                c.execute("INSERT INTO users (telegram_id, username) VALUES (?, ?)", (tg, f"lp{tg}"))
                uid = c.execute("SELECT id FROM users WHERE telegram_id = ?", (tg,)).fetchone()["id"]
                ids[tg] = uid
                c.execute("INSERT INTO vpn_keys (user_id, tariff_id, expires_at) VALUES (?, 1, datetime('now', ?))",
                          (uid, f"-{days} days"))
        got = {x["telegram_id"] for x in lc.find_candidates()}
        self.assertIn(9801, got)          # ушёл 10 дней назад
        self.assertNotIn(9802, got)       # ушёл 2 дня назад — рано
        self.assertNotIn(9803, got)       # ушёл давно
        cand = next(x for x in lc.find_candidates() if x["telegram_id"] == 9801)
        lc.set_enabled(True)
        self.assertTrue(lc.is_enabled())
        lc.set_enabled(False)
        self.assertFalse(lc.is_enabled())
        self.assertIsNotNone(lc.issue_coupon(cand["user_id"], cand["source"]))
        self.assertNotIn(9801, {x["telegram_id"] for x in lc.find_candidates()})  # повторов нет


class UsersListFiltersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run_migrations()

    def test_trial_and_banned_filters(self):
        from database.db_users import get_all_users_paginated
        with get_db() as c:
            c.execute("INSERT INTO users (telegram_id, username, used_trial, is_banned) VALUES (9901, 'tr', 1, 0)")
            c.execute("INSERT INTO users (telegram_id, username, used_trial, is_banned) VALUES (9902, 'bn', 0, 1)")
        trial = {u["telegram_id"] for u in get_all_users_paginated(0, 50, "used_trial")[0]}
        banned = {u["telegram_id"] for u in get_all_users_paginated(0, 50, "banned")[0]}
        allu = {u["telegram_id"] for u in get_all_users_paginated(0, 50, "all")[0]}
        self.assertIn(9901, trial)
        self.assertNotIn(9902, trial)
        self.assertIn(9902, banned)
        self.assertNotIn(9902, allu)


class UpdateNoticeTest(unittest.TestCase):
    def test_update_log_fits_telegram_limit(self):
        from bot.utils import git_utils as g
        orig = (g.get_pending_commits_list, g.run_git_command)
        g.get_pending_commits_list = lambda: (True, [{"hash": "a", "message": "m"}] * 10)
        g.run_git_command = lambda args, timeout=30: (True, "abc " + "я<&" * 3000)
        try:
            ok, behind, text, *_ = g.check_for_updates()
        finally:
            g.get_pending_commits_list, g.run_git_command = orig
        self.assertTrue(ok)
        self.assertEqual(behind, 10)
        self.assertLess(len(f"📦 <b>Доступно обновление!</b>\n\n{text}"), 4096)
        self.assertTrue(text.endswith("</pre>"))
        self.assertNotIn("&am</pre>", text)


class LicenseExtendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run_migrations()

    def test_extended_expiry_matches_extend(self):
        from database.db_licenses import (create_partner_license, get_partner_license,
                                          extended_expiry, extend_partner_license)
        key = create_partner_license("Тест продления", {"ai_assistant"}, 10)
        lic = get_partner_license(key)
        preview = extended_expiry(lic, 90)
        self.assertTrue(extend_partner_license(key, 90))
        self.assertEqual(get_partner_license(key)["expires_at"], preview)
        self.assertGreater(preview, lic["expires_at"])


class PingVpnFlagTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run_migrations()

    def _call(self, client_ip):
        import asyncio
        from aiohttp.test_utils import make_mocked_request
        from bot.webapp import server as w
        saved = (w._get_telegram_id, w._measure_ping, w._get_known_vpn_server_ips)

        async def fake_ping(host, port, timeout=5.0):
            return 10

        async def fake_ips():
            return {"203.0.113.7"}
        w._get_telegram_id = lambda request: 9555
        w._measure_ping = fake_ping
        w._get_known_vpn_server_ips = fake_ips
        try:
            req = make_mocked_request("GET", "/api/ping", headers={"X-Forwarded-For": client_ip})
            resp = asyncio.run(w.handle_ping(req))
            return json.loads(resp.body)
        finally:
            w._get_telegram_id, w._measure_ping, w._get_known_vpn_server_ips = saved

    def test_vpn_flag(self):
        self.assertTrue(self._call("203.0.113.7")["vpn_active"])
        self.assertFalse(self._call("198.51.100.9")["vpn_active"])


class DirectServersProfileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run_migrations()

    def test_adds_without_duplicates(self):
        from unittest import mock
        srvs = [{"is_active": 1, "host": "Vpn.Example.com", "public_ip": "1.2.3.4, 5.6.7.8"},
                {"is_active": 1, "host": "9.9.9.9", "public_ip": ""},
                {"is_active": 0, "host": "off.example.com", "public_ip": "2.2.2.2"}]
        with mock.patch("database.db_servers.get_all_servers", return_value=srvs):
            out = st.add_direct_servers({"DirectIp": ["1.2.3.4"], "DirectSites": []})
        self.assertEqual(out["DirectIp"], ["1.2.3.4", "5.6.7.8", "9.9.9.9"])
        self.assertEqual(out["DirectSites"], ["domain:vpn.example.com"])

    def test_off_by_default(self):
        self.assertFalse(st.get_routing_direct_servers())


class HappInstallToggleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run_migrations()

    def test_disabled_gives_no_install_code(self):
        import asyncio
        from unittest import mock
        from bot.services import happ_proxy as hp
        self.assertTrue(st.is_happ_install_enabled())
        st.set_happ_install_enabled(False)
        try:
            with mock.patch.object(hp, "is_configured", return_value=True), \
                 mock.patch.object(hp, "add_install", side_effect=AssertionError("не должен вызываться")):
                self.assertIsNone(asyncio.run(hp.get_or_create_install_code_for_sub("sub123")))
        finally:
            st.set_happ_install_enabled(True)


class StripEmailSuffixTest(unittest.TestCase):
    def test_strip_plain_and_base64(self):
        import base64
        from bot.webapp.server import _strip_email_suffix
        body = b"vless://a@h:1#%F0%9F%87%AB-user_x1\nvless://b@h:2#Name"
        out = _strip_email_suffix(body, ["user_x1"]).decode()
        self.assertIn("#%F0%9F%87%AB", out)
        self.assertNotIn("user_x1", out)
        self.assertTrue(out.endswith("#Name"))
        b64 = base64.b64encode(body)
        self.assertNotIn(b"user_x1", base64.b64decode(_strip_email_suffix(b64, ["user_x1"])))
        self.assertEqual(_strip_email_suffix(body, [None]), body)
        cdn = b"vless://u@de-cdn.example.org:443?type=xhttp#Name%20%F0%9F%87%A9%F0%9F%87%AA-de-cdn.example.org"
        self.assertEqual(_strip_email_suffix(cdn, []).decode().split("#")[1], "Name%20%F0%9F%87%A9%F0%9F%87%AA")
        raw = (b"vpn://AAAA\n"
               b"vless://u@a.example.org:1?type=tcp#VLESS%20TCP-user_x1\n"
               b"vless://u@de-cdn.example.org:443?type=xhttp#WL-de-cdn.example.org-user_x1\n"
               b"tuic://u:p@a.example.org:2#Gaming")
        for payload in (raw, base64.b64encode(raw)):
            res = _strip_email_suffix(payload, ["user_x1", "cdn_user_x1"])
            res = res if res.startswith(b"vpn") else base64.b64decode(res)
            names = [l.rsplit(b"#", 1)[1] for l in res.splitlines() if b"#" in l]
            self.assertEqual(names, [b"VLESS%20TCP", b"WL", b"Gaming"])
        two = b"vless://u@de-cdn.example.org:443?type=xhttp#Name-de-cdn.example.org-user_x1"
        self.assertEqual(_strip_email_suffix(two, ["user_x1"]).decode().split("#")[1], "Name")


class AuditFixesTest(unittest.TestCase):
    """1.450: двойная оплата, лимитер и закрепление сертификата панели."""

    @classmethod
    def setUpClass(cls):
        run_migrations()

    def test_complete_order_pays_only_once(self):
        from database.db_payments import complete_order, is_order_already_paid, reopen_paid_order
        with get_db() as c:
            c.execute("INSERT OR IGNORE INTO users (telegram_id) VALUES (777001)")
            uid = c.execute("SELECT id FROM users WHERE telegram_id = 777001").fetchone()["id"]
            c.execute("INSERT INTO payments (user_id, order_id, status) VALUES (?, 'audit-ord-1', 'pending')", (uid,))
        self.assertTrue(complete_order("audit-ord-1"))
        self.assertFalse(complete_order("audit-ord-1"))  # второй опрос/вебхук ничего не делает
        self.assertTrue(is_order_already_paid("audit-ord-1"))
        self.assertTrue(reopen_paid_order("audit-ord-1"))
        self.assertTrue(complete_order("audit-ord-1"))  # после неудачной выдачи можно оформить снова

    def test_rate_limiter_blocks_after_limit(self):
        from bot.webapp.ratelimit import rl_allowed, rl_record
        for _ in range(3):
            self.assertTrue(rl_allowed("audit-rl", 3, 60))
            rl_record("audit-rl")
        self.assertFalse(rl_allowed("audit-rl", 3, 60))

    def test_panel_ssl_off_by_default_and_pins_when_enabled(self):
        import asyncio
        import aiohttp
        from bot.services import panel_tls
        self.assertIs(asyncio.run(panel_tls.panel_ssl(1, "h", 1)), False)
        st.set_setting("panel_tls_pin", "1")
        st.set_setting("panel_tls_pin_9", "ab" * 32)
        try:
            res = asyncio.run(panel_tls.panel_ssl(9, "h", 1))
            self.assertIsInstance(res, aiohttp.Fingerprint)
            self.assertIs(asyncio.run(panel_tls.panel_ssl(9, "h", 1, "http")), False)
        finally:
            st.set_setting("panel_tls_pin", "0")


class ShopThemesTest(unittest.TestCase):
    def test_every_shop_theme_has_css_in_shop_html(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "bot", "webapp", "templates", "shop.html"), encoding="utf-8") as f:
            html = f.read()
        for tid in st.SHOP_THEMES:
            if tid != "classic":  # classic — исходный вид, переопределений нет
                self.assertIn(f'body[data-theme="{tid}"]', html, tid)


class SubInfoLineTest(unittest.TestCase):
    NAME = "ℹ️ Оплата: @bot"

    @classmethod
    def setUpClass(cls):
        run_migrations()

    def test_plain_body_line_goes_right_after_first_real_server(self):
        from urllib.parse import quote
        from bot.utils.sub_info_line import add_info_line
        body = b"#profile-title: X\nvless://a@h:1#one\nvless://b@h:2#two"
        out = add_info_line(body, self.NAME).decode("utf-8").split("\n")
        self.assertEqual(out[:2], ["#profile-title: X", "vless://a@h:1#one"])  # первым остаётся настоящий сервер
        self.assertTrue(out[2].startswith("vless://0000") and out[2].endswith("#" + quote(self.NAME, safe="")))
        self.assertEqual(out[3:], ["vless://b@h:2#two"])

    def test_single_server_line_goes_after_it(self):
        from bot.utils.sub_info_line import add_info_line
        out = add_info_line(b"vless://a@h:1#one", self.NAME).decode("utf-8").split("\n")
        self.assertEqual(out[0], "vless://a@h:1#one")
        self.assertEqual(len(out), 2)

    def test_base64_body_and_idempotent(self):
        import base64
        from bot.utils.sub_info_line import add_info_line
        body = base64.b64encode(b"vless://a@h:1#one")
        once = add_info_line(body, self.NAME)
        self.assertEqual(add_info_line(once, self.NAME), once)
        self.assertEqual(base64.b64decode(once).decode("utf-8").count("vless://"), 2)

    def test_empty_or_unreadable_body_untouched(self):
        from bot.utils.sub_info_line import add_info_line
        for body in (b"", b"   \n", b"\xff\xfe", b"not base64 or links!"):
            self.assertEqual(add_info_line(body, self.NAME), body)

    def test_info_line_toggle_exists_and_is_off_by_default(self):
        for app in ("happ", "incy"):
            self.assertIn("info_line", st.CLIENT_TOGGLE_LABELS_BY_APP[app])
            self.assertFalse(st.is_client_toggle_enabled(app, "info_line"))


class ClientToggleMenuTest(unittest.TestCase):
    """Каждый переключатель Happ/INCY обязан быть во всех справочниках меню админки:
    иначе меню падает с KeyError (так сломалась «строка-подсказка» в 1.457)."""

    @staticmethod
    def _dict_assigned_to(path, name):
        import ast
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, *path), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
                return ast.literal_eval(node.value)
        raise AssertionError(f"{name} не найден в {'/'.join(path)}")

    def test_every_toggle_has_icon_and_hint(self):
        icons = self._dict_assigned_to(("bot", "keyboards", "admin_settings.py"), "_icons")
        hints = self._dict_assigned_to(("bot", "handlers", "admin", "integrations.py"), "_CLIENT_SETTING_INFO")
        for app, toggles in st.CLIENT_TOGGLE_LABELS_BY_APP.items():
            for toggle in toggles:
                self.assertIn(toggle, icons, f"нет иконки: {app}/{toggle}")
                self.assertIn(toggle, hints.get(app, {}), f"нет подсказки: {app}/{toggle}")


class StaticVersionTest(unittest.TestCase):
    def test_adds_mtime_version_to_js_and_css_only(self):
        from bot.utils.static_version import version_static_urls
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "vendor"))
            for rel, t in (("site-ai.js", 1700000000), ("vendor/tailwind.css", 1700000500), ("logo.png", 1700000900)):
                p = os.path.join(d, *rel.split("/"))
                open(p, "wb").write(b"x")
                os.utime(p, (t, t))
            html = ('<script src="/static/site-ai.js" defer></script>'
                    '<link href="/static/vendor/tailwind.css">'
                    '<img src="/static/logo.png"><script src="/static/missing.js">')
            out = version_static_urls(html, d)
        self.assertIn('/static/site-ai.js?v=1700000000"', out)
        self.assertIn('/static/vendor/tailwind.css?v=1700000500"', out)
        self.assertIn('src="/static/logo.png"', out)            # картинки не трогаем
        self.assertIn('src="/static/missing.js"', out)          # нет файла — как было

    def test_version_changes_when_file_changes(self):
        from bot.utils.static_version import version_static_urls
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.js")
            open(p, "wb").write(b"x")
            os.utime(p, (1700000000, 1700000000))
            before = version_static_urls('<script src="/static/a.js">', d)
            os.utime(p, (1700000100, 1700000100))
            after = version_static_urls('<script src="/static/a.js">', d)
        self.assertNotEqual(before, after)


class ProbeNameTailsTest(unittest.TestCase):
    def test_strip_name_tails(self):
        from bot.services.connection_probe import strip_name_tails
        t = ["user_x1", "cdn_user_x1", "cdn.example.org"]
        self.assertEqual(strip_name_tails("VLESS REALITY 🇫🇮-user_x1", t), "VLESS REALITY 🇫🇮")
        self.assertEqual(strip_name_tails("Белые списки 🇫🇮-cdn.example.org-cdn_user_x1", t), "Белые списки 🇫🇮")
        self.assertEqual(strip_name_tails("Gaming", t), "Gaming")
        self.assertIsNone(strip_name_tails(None, t))


class TrialNotCountedAsPaymentTest(unittest.TestCase):
    def test_site_trial_not_in_payment_stats(self):
        run_migrations()
        from database.db_payments import get_payments_stats
        with get_db() as c:
            c.execute("INSERT OR IGNORE INTO tariffs (id, name, duration_days, price_cents, price_stars, price_rub) VALUES (9001, 'T', 5, 100, 100, 100)")
            before = get_payments_stats(1)["site_count"]
            c.execute("INSERT INTO anonymous_purchases (order_id, tariff_id, claim_code, status, yookassa_payment_id) VALUES ('pubTR1', 9001, 'TRIA-0001', 'paid', 'trial')")
            c.execute("INSERT INTO anonymous_purchases (order_id, tariff_id, claim_code, status, yookassa_payment_id) VALUES ('pubPD1', 9001, 'TRIA-0002', 'paid', 'yk-123')")
        self.assertEqual(get_payments_stats(1)["site_count"], before + 1)


class LastPaymentTest(unittest.TestCase):
    def test_last_real_payment_skips_trial_and_shows_name(self):
        run_migrations()
        from database.db_payments import get_last_real_payment
        from bot.services.admin_monitoring import _last_payment_text
        with get_db() as c:
            c.execute("INSERT OR IGNORE INTO users (telegram_id, first_name, last_name, username) VALUES (777002, 'Иван', 'Петров', 'ivan')")
            uid = c.execute("SELECT id FROM users WHERE telegram_id = 777002").fetchone()["id"]
            c.execute("INSERT INTO payments (user_id, order_id, status, payment_type, final_amount_cents, paid_at) VALUES (?, 'lp-real', 'paid', 'cards', 25000, '2099-01-01 10:00:00')", (uid,))
            c.execute("INSERT INTO payments (user_id, order_id, status, payment_type, final_amount_cents, paid_at) VALUES (?, 'lp-trial', 'paid', 'trial', 0, '2099-01-02 10:00:00')", (uid,))
        self.assertEqual(get_last_real_payment()["order_id"], "lp-real")
        text = _last_payment_text()
        self.assertIn("Иван Петров", text)
        self.assertIn("250 ₽", text)


class AdminInsightsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run_migrations()

    def test_build_text_and_blocks(self):
        from bot.services import admin_insights as ai
        with get_db() as c:
            c.execute("INSERT OR IGNORE INTO tariffs (id, name, duration_days, price_cents, price_stars, price_rub) VALUES (9002, 'T2', 30, 100, 100, 100)")
            c.execute("INSERT OR IGNORE INTO users (telegram_id, first_name) VALUES (777003, 'Анна')")
            uid = c.execute("SELECT id FROM users WHERE telegram_id = 777003").fetchone()["id"]
            c.execute("INSERT INTO payments (user_id, order_id, status, payment_type, final_amount_cents, paid_at) VALUES (?, 'ai-trial', 'paid', 'trial', 0, datetime('now', '-2 days'))", (uid,))
            c.execute("INSERT INTO payments (user_id, order_id, status, payment_type, final_amount_cents, paid_at) VALUES (?, 'ai-real', 'paid', 'cards', 12500, datetime('now', '-1 days'))", (uid,))
            c.execute("INSERT INTO payments (user_id, order_id, status, payment_type, final_amount_cents, paid_at) VALUES (?, 'ai-pend', 'pending', 'cards', 9900, datetime('now', '-1 hours'))", (uid,))
        self.assertGreaterEqual(ai.trial_funnel(7)["bought"], 1)
        self.assertEqual(ai.recent_payments(1)[0]["who"].split()[0] in ("Анна", "гость"), True)
        text = ai.build_text()
        self.assertIn("Аналитика", text)
        self.assertLess(len(text), 3500)
        ai.record_events([("down", "Сервер А", "нет ответа")])
        self.assertEqual(ai.recent_incidents(1)[0]["title"], "Сервер А")
        self.assertIn("Анна", ai.payments_list_text())


class LastBackupTest(unittest.TestCase):
    def test_finds_bot_db_in_dated_subfolder_not_other_files(self):
        import shutil
        from database.db_backup import BACKUP_DIR
        from bot.services.admin_insights import last_backup, _size
        existed = os.path.isdir(BACKUP_DIR)
        day = os.path.join(BACKUP_DIR, "2099-01-01")
        try:
            os.makedirs(day, exist_ok=True)
            with open(os.path.join(day, "vpn_bot.db"), "wb") as f:
                f.write(b"x" * 300000)
            with open(os.path.join(BACKUP_DIR, "note.txt"), "wb") as f:
                f.write(b"y")
            res = last_backup()
            self.assertEqual(res["bytes"], 300000)
        finally:
            shutil.rmtree(day, ignore_errors=True)
            try:
                os.remove(os.path.join(BACKUP_DIR, "note.txt"))
            except OSError:
                pass
            if not existed:
                shutil.rmtree(BACKUP_DIR, ignore_errors=True)
        self.assertEqual(_size(300000), "292 КБ")
        self.assertEqual(_size(5 * 1048576), "5.0 МБ")


class ClientCabinetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        run_migrations()

    def test_bar(self):
        from bot.services.client_cabinet import bar
        self.assertEqual(bar(30, 100), "▓▓▓░░░░░░░ 30%")
        self.assertEqual(bar(5, 0), "")
        self.assertEqual(bar(500, 100), "▓▓▓▓▓▓▓▓▓▓ 100%")

    def test_collect_and_render(self):
        from aiogram.types import InputRichMessage
        from bot.services import client_cabinet as cab
        with get_db() as c:
            c.execute("INSERT OR IGNORE INTO tariffs (id, name, duration_days, price_cents, price_stars, price_rub) VALUES (9003, 'T3', 30, 100, 100, 100)")
            c.execute("INSERT OR IGNORE INTO users (telegram_id, first_name, personal_balance) VALUES (777004, 'Клиент', 12500)")
            uid = c.execute("SELECT id FROM users WHERE telegram_id = 777004").fetchone()["id"]
            c.execute("INSERT OR IGNORE INTO users (telegram_id, referred_by) VALUES (777005, ?)", (uid,))
            c.execute("INSERT INTO vpn_keys (user_id, tariff_id, expires_at, traffic_used, traffic_limit, client_uuid, panel_email) "
                      "VALUES (?, 9003, datetime('now', '+10 days'), 30000000000, 100000000000, 'cab-uuid', 'cab@x')", (uid,))
            c.execute("INSERT INTO payments (user_id, order_id, status, payment_type, final_amount_cents, paid_at) VALUES (?, 'cab-pay', 'paid', 'cards', 25000, datetime('now', '-2 days'))", (uid,))
        d = cab.collect(777004)
        self.assertEqual((d["keys_active"], d["refs"], d["paid30"]), (1, 1, "250 ₽"))
        self.assertIn("125", d["balance"])
        self.assertTrue(d["days_left"] in (10, 11))
        html_text = cab.build_html(d)
        self.assertTrue(html_text.startswith("<table bordered striped>"))
        InputRichMessage(html=html_text)
        self.assertIn("Личный кабинет", cab.build_fallback(d))
        styles = [b.style for row in cab.keyboard().inline_keyboard for b in row]
        self.assertEqual(styles[:2], ["primary", "success"])
        last_row = cab.keyboard().inline_keyboard[-1]
        self.assertEqual(len(last_row), 2)  # «Назад» и «На главную» в одной строке

    def test_disabled_by_default(self):
        from bot.services import client_cabinet as cab
        self.assertFalse(cab.enabled())
