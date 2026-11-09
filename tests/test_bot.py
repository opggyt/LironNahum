"""Offline tests: no network, no secrets. Run with:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import roblox_ads_bot as bot  # noqa: E402

NOW = datetime(2026, 11, 9, 13, 0, tzinfo=timezone.utc)
ISOLATES = str.maketrans("", "", bot.LRI + bot.PDI)


def plain(text: str) -> str:
    """Drop the invisible direction marks so assertions read naturally."""
    return text.translate(ISOLATES)


class FakeResponse:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeSession:
    """Replays a scripted list of responses (or exceptions) and records calls."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def demo(with_metrics=False):
    return bot._load_demo(with_metrics)


class MoneyTests(unittest.TestCase):
    def test_micro_usd_strings_become_dollars(self):
        cases = {
            "5000000": "$5.00",
            "50000000": "$50.00",
            "1234567890": "$1,234.57",
            "0": "$0.00",
            "10000": "$0.01",
            "4999": "$0.00",
            "5000": "$0.01",  # half a cent rounds up
            "999999": "$1.00",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(bot.fmt_usd(bot.parse_micros(raw)), expected)

    def test_large_amounts_keep_full_precision(self):
        # A float would turn this into ...345.60 or ...345.70.
        self.assertEqual(bot.fmt_usd(bot.parse_micros("123456789012345678")), "$123,456,789,012.35")
        self.assertEqual(str(bot.micros_to_usd(123456789012345678)), "123456789012.345678")

    def test_bad_amounts_are_none_not_zero(self):
        for raw in (None, "", "abc", "12.5", "NaN", True):
            with self.subTest(raw=raw):
                self.assertIsNone(bot.parse_micros(raw))
        self.assertEqual(bot.fmt_usd(None), "לא ידוע")

    def test_signed_amounts(self):
        self.assertEqual(bot.fmt_usd(20_000_000, signed=True), "+$20.00")
        self.assertEqual(bot.fmt_usd(-1_500_000, signed=True), "-$1.50")
        self.assertEqual(bot.fmt_usd(0, signed=True), "$0.00")

    def test_ctr_cost_per_play_and_utilization(self):
        self.assertEqual(str(bot.ctr_percent(5000, 250000)), "2.00")
        self.assertIsNone(bot.ctr_percent(0, 0))
        self.assertIsNone(bot.ctr_percent(None, 100))
        self.assertEqual(bot.fmt_cost_per_play(123_456_789, 1800), "$0.069")
        self.assertEqual(bot.fmt_cost_per_play(45_000_000, 30), "$1.50")
        self.assertIsNone(bot.fmt_cost_per_play(45_000_000, 0))

        daily = bot.Campaign("1", "a", "ACTIVE", "SERVING", budget_type="DAILY", budget_micros=50_000_000)
        daily.metrics = bot.Metrics(spend_micros=123_456_789, spend_today_micros=12_500_000)
        self.assertEqual(str(bot.utilization(daily)[0]), "25.0")
        daily.metrics = bot.Metrics(spend_micros=123_456_789)  # no "today" figure: can't know
        self.assertIsNone(bot.utilization(daily))

        lifetime = bot.Campaign("2", "b", "ACTIVE", "SERVING", budget_type="LIFETIME", budget_micros=500_000_000)
        lifetime.metrics = bot.Metrics(spend_micros=123_456_789)
        self.assertEqual(str(bot.utilization(lifetime)[0]), "24.7")


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.campaigns, self.previous = demo()
        self.report = plain("\n\n".join(bot.build_report(self.campaigns, self.previous, NOW)))

    def test_only_active_campaigns_are_listed(self):
        shown = [c.name for c in self.campaigns if bot.is_reportable(c)]
        self.assertEqual(
            sorted(shown),
            ["Obby Rush <Season 2> & Friends", "Pet Simulator Promo", "Sky Tycoon - Winter Launch", "Tower Defense Evergreen"],
        )
        self.assertIn("קמפיינים פעילים: 4 (🟢 2 · 🟡 1 · 🔴 1)", self.report)
        self.assertIn("לא מוצגים: 2 קמפיינים", self.report)
        self.assertNotIn("Summer Sale 2026", self.report)

    def test_status_emoji_and_reason(self):
        self.assertIn("🟢 <b>Sky Tycoon - Winter Launch</b>", self.report)
        self.assertIn("🟡 <b>Obby Rush", self.report)
        self.assertIn("🔴 <b>Pet Simulator Promo</b>", self.report)
        self.assertIn("סיבה: MODERATED (נחסם במודרציה)", self.report)
        self.assertIn("סיבה: LEARNING (בשלב למידה)", self.report)

    def test_budgets(self):
        self.assertIn("תקציב: $50.00 ליום", self.report)
        self.assertIn("תקציב: $500.00 לכל חיי הקמפיין", self.report)
        self.assertIn("סך התקציב היומי: $145.50", self.report)  # 50 + 20 + 75.50
        self.assertIn("סך התקציבים הכוללים: $500.00", self.report)
        # 2026-11-09T22:00Z is midnight in Israel (UTC+2 in winter).
        self.assertIn("ירידת תקציב מתוזמנת ל-$60.00 ב-10/11/2026 00:00", self.report)

    def test_alert_is_first_and_names_both_stopped_campaigns(self):
        blocks = bot.build_report(self.campaigns, self.previous, NOW)
        first = plain(blocks[0])
        self.assertTrue(first.startswith("🚨"))
        self.assertIn("<b>Pet Simulator Promo</b>: SERVING → REJECTED", first)
        self.assertIn("<b>Halloween Event</b>: SERVING → NOT_SERVING", first)
        self.assertNotIn("Summer Sale", first)  # was already NOT_SERVING last time

    def test_no_alert_when_nothing_moved(self):
        state = bot.Snapshot(NOW, bot.build_state(self.campaigns, NOW)["campaigns"])
        self.assertEqual(bot.find_alerts(self.campaigns, state), [])
        report = "\n".join(bot.build_report(self.campaigns, state, NOW))
        self.assertNotIn("🚨", report)
        self.assertIn("שינוי מהדיווח הקודם: ללא שינוי", report)

    def test_new_rejected_campaign_alerts_but_new_serving_does_not(self):
        state = bot.Snapshot(NOW, {})
        names = [c.name for c, _ in bot.find_alerts(self.campaigns, state)]
        self.assertEqual(names, ["Pet Simulator Promo"])

    def test_changes_since_previous_report(self):
        self.assertIn("הגשה IN_REVIEW → SERVING", self.report)
        self.assertIn("תקציב $30.00 → $50.00 (+$20.00)", self.report)
        self.assertIn("שינוי מהדיווח הקודם: 🆕 קמפיין חדש", self.report)
        self.assertIn("ההשוואה היא מול הדיווח מ-09/11/2026 11:00", self.report)

    def test_first_run_has_no_alert_and_says_so(self):
        report = "\n".join(bot.build_report(self.campaigns, None, NOW))
        self.assertNotIn("🚨", report)
        self.assertNotIn("שינוי מהדיווח הקודם", report)
        self.assertIn("זה הדיווח הראשון", report)

    def test_campaign_names_are_html_escaped(self):
        self.assertIn("Obby Rush &lt;Season 2&gt; &amp; Friends", self.report)
        self.assertNotIn("<Season 2>", self.report)

    def test_report_time_is_israel_time(self):
        self.assertIn("🕒 09/11/2026 15:00 (שעון ישראל)", self.report)  # winter, UTC+2
        summer = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        self.assertIn("🕒 01/07/2026 15:00", "\n".join(bot.build_report([], None, summer)))  # UTC+3

    def test_says_metrics_are_unavailable_instead_of_inventing_them(self):
        self.assertIn("Roblox עדיין לא חושפת מדדי קמפיין ב-API", self.report)
        self.assertNotIn("הוצאה:", self.report)

    def test_no_active_campaigns(self):
        report = "\n".join(bot.build_report([], None, NOW))
        self.assertIn("אין כרגע קמפיינים פעילים", report)

    def test_real_runs_get_no_metrics(self):
        self.assertEqual(bot.fetch_campaign_metrics(None, self.campaigns), {})


class MetricsReportTests(unittest.TestCase):
    """The metrics path is unused until Roblox ships the data; keep it correct."""

    def setUp(self):
        campaigns, previous = demo(with_metrics=True)
        self.report = plain("\n\n".join(bot.build_report(campaigns, previous, NOW)))

    def test_per_campaign_metrics(self):
        self.assertIn("הוצאה: $123.46 · ניצול: 25.0% מהתקציב היומי", self.report)
        self.assertIn("חשיפות: 250,000 · קליקים: 5,000 · CTR: 2.00%", self.report)
        self.assertIn("כניסות: 1,800 · עלות לכניסה: $0.069", self.report)
        self.assertIn("הוצאה: $1,234.57 · ניצול: 100.0% מהתקציב היומי", self.report)
        self.assertIn("הוצאה: $0.00 · ניצול: 0.0% מהתקציב הכולל", self.report)

    def test_deltas_since_previous_report(self):
        self.assertIn("הוצאה +$13.46 · חשיפות +22,000 · קליקים +400 · כניסות +150", self.report)
        self.assertIn("הוצאה +$34.57 · חשיפות +50,000 · קליקים +900 · כניסות +500", self.report)
        # Pet Simulator's numbers did not move: only the status change is listed.
        self.assertIn("שינוי מהדיווח הקודם: הגשה SERVING → REJECTED\n", self.report)

    def test_totals(self):
        # 123.456789 + 0 + 45 + 1234.56789 = 1403.024679
        self.assertIn("הוצאה כוללת: $1,403.02", self.report)
        self.assertIn("חשיפות: 3,340,000 · כניסות: 22,100 · CTR: 1.52% · עלות לכניסה: $0.063", self.report)


class SplitTests(unittest.TestCase):
    def test_short_report_is_one_message(self):
        self.assertEqual(len(bot.split_blocks(["a", "b"])), 1)

    def test_long_report_is_split_between_campaigns(self):
        campaigns = [
            bot.Campaign(str(i), f"Campaign {i} <&> 🎮", "ACTIVE", "SERVING", ["LEARNING"], "DAILY", 5_000_000 + i)
            for i in range(120)
        ]
        blocks = bot.build_report(campaigns, None, NOW)
        messages = bot.split_blocks(blocks)
        self.assertGreater(len(messages), 1)
        for message in messages:
            self.assertLessEqual(bot.tg_len(message), bot.TELEGRAM_LIMIT)
            self.assertEqual(message.count("<b>"), message.count("</b>"))
            self.assertEqual(message.count("<code>"), message.count("</code>"))
        self.assertEqual("\n\n".join(messages), "\n\n".join(blocks))  # nothing lost or reordered

    def test_length_is_counted_in_utf16_units(self):
        self.assertEqual(bot.tg_len("🟢"), 2)
        messages = bot.split_blocks(["🟢" * 2000, "🟢" * 2000])
        self.assertEqual(len(messages), 2)

    def test_oversized_block_and_line_still_fit(self):
        block = "\n".join(f"<b>line {i}</b> " + "x" * 90 for i in range(100))
        huge_line = "<b>" + "y&" * 5000 + "</b>"
        for message in bot.split_blocks([block, huge_line]):
            self.assertLessEqual(bot.tg_len(message), bot.TELEGRAM_LIMIT)
            self.assertEqual(message.count("<b>"), message.count("</b>"))
            self.assertIsNone(re.search(r"&(?!amp;|lt;|gt;)", message))  # no entity cut in half


class RobloxClientTests(unittest.TestCase):
    def test_retries_429_and_500_then_succeeds(self):
        session = FakeSession([
            FakeResponse(429, {"errors": [{"code": "RATE_LIMITED"}]}, {"Retry-After": "7"}),
            FakeResponse(500, None),
            FakeResponse(200, {"campaigns": [{"id": "1"}]}),
        ])
        sleeps = []
        result = bot.list_campaigns(session, sleep=sleeps.append)
        self.assertEqual(result, [{"id": "1"}])
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(sleeps[0], 7.0)  # Retry-After wins
        self.assertTrue(4.0 <= sleeps[1] <= 5.0)  # 2nd attempt: 4s + jitter

    def test_gives_up_after_max_attempts(self):
        body = {"errors": [{"code": "RATE_LIMITED", "message": "Too many requests"}]}
        session = FakeSession([FakeResponse(429, body)] * bot.MAX_ATTEMPTS)
        sleeps = []
        with self.assertRaises(bot.RobloxApiError) as caught:
            bot.list_campaigns(session, sleep=sleeps.append)
        self.assertEqual((caught.exception.status, caught.exception.code), (429, "RATE_LIMITED"))
        self.assertEqual(caught.exception.attempts, bot.MAX_ATTEMPTS)
        self.assertEqual(len(sleeps), bot.MAX_ATTEMPTS - 1)
        self.assertEqual(sleeps, sorted(sleeps))  # backoff grows

    def test_403_is_not_retried(self):
        session = FakeSession([FakeResponse(403, {"errors": [{"code": "PERMISSION_DENIED"}]})])
        with self.assertRaises(bot.RobloxApiError) as caught:
            bot.list_campaigns(session, sleep=lambda _: None)
        self.assertEqual(len(session.calls), 1)
        message = bot.build_error_message(caught.exception, NOW)
        self.assertIn("HTTP 403 · PERMISSION_DENIED", message)
        self.assertIn("ad.campaign:read", message)

    def test_network_errors_are_retried_then_reported(self):
        session = FakeSession([requests.ConnectionError("boom")] * bot.MAX_ATTEMPTS)
        with self.assertRaises(bot.RobloxApiError) as caught:
            bot.list_campaigns(session, sleep=lambda _: None)
        self.assertEqual(caught.exception.code, "NETWORK_ERROR")
        self.assertEqual(len(session.calls), bot.MAX_ATTEMPTS)

    def test_follows_pagination_and_only_reads(self):
        session = FakeSession([
            FakeResponse(200, {"campaigns": [{"id": "1"}], "nextPageToken": "abc"}),
            FakeResponse(200, {"campaigns": [{"id": "2"}]}),
        ])
        result = bot.list_campaigns(session, group_id="42", sleep=lambda _: None)
        self.assertEqual([c["id"] for c in result], ["1", "2"])
        self.assertEqual(session.calls[1][2]["params"]["pageToken"], "abc")
        self.assertEqual(session.calls[0][2]["params"]["groupId"], "42")
        for method, url, _ in session.calls:
            self.assertEqual(method, "GET")
            self.assertEqual(url, "https://apis.roblox.com/ads-management/v1/campaigns")


class TelegramTests(unittest.TestCase):
    def test_sends_html_to_the_chat(self):
        session = FakeSession([FakeResponse(200, {"ok": True})])
        bot.send_telegram("TOKEN", "123", "<b>hi</b>", session=session)
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://api.telegram.org/botTOKEN/sendMessage")
        self.assertEqual(kwargs["json"]["parse_mode"], "HTML")
        self.assertEqual(kwargs["json"]["chat_id"], "123")

    def test_waits_for_retry_after_on_429(self):
        session = FakeSession([
            FakeResponse(429, {"ok": False, "parameters": {"retry_after": 3}}),
            FakeResponse(200, {"ok": True}),
        ])
        sleeps = []
        bot.send_telegram("TOKEN", "123", "x", session=session, sleep=sleeps.append)
        self.assertEqual(sleeps, [3.0])

    def test_errors_never_contain_the_token(self):
        secret = "123456:SECRET-TOKEN"
        leaky = requests.ConnectionError(f"https://api.telegram.org/bot{secret}/sendMessage failed")
        cases = [
            FakeSession([leaky] * bot.MAX_ATTEMPTS),
            FakeSession([FakeResponse(400, {"ok": False, "description": "Bad Request: chat not found"})]),
        ]
        for session in cases:
            with self.assertRaises(bot.TelegramError) as caught:
                bot.send_telegram(secret, "123", "x", session=session, sleep=lambda _: None)
            self.assertNotIn("SECRET", str(caught.exception))


class StateTests(unittest.TestCase):
    def test_round_trip(self):
        campaigns, _ = demo(with_metrics=True)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state" / "snapshot.json"
            bot.save_state(path, bot.build_state(campaigns, NOW))
            loaded = bot.load_state(path)
            self.assertEqual(loaded.generated_at, NOW)
            self.assertEqual(loaded.campaigns["c-1001"]["budgetMicros"], "50000000")  # stays a string
            self.assertEqual(loaded.campaigns["c-1001"]["metrics"]["spendMicros"], "123456789")
            self.assertEqual(bot.find_alerts(campaigns, loaded), [])
            self.assertEqual(list(path.parent.iterdir()), [path])  # no temp file left behind

    def test_missing_or_broken_file_is_a_first_run(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(bot.load_state(Path(folder) / "nope.json"))
            broken = Path(folder) / "broken.json"
            broken.write_text("{not json", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertIsNone(bot.load_state(broken))


class MainTests(unittest.TestCase):
    ENV = {"ROBLOX_API_KEY": "key", "TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "1"}

    def run_main(self, argv, env=None, campaigns=None, error=None, telegram_error=None):
        sent = []

        def fake_send(token, chat_id, text, **_):
            if telegram_error:
                raise telegram_error
            sent.append(text)

        def fake_list(session, group_id=None, **_):
            self.assertEqual(session.headers["x-api-key"], "key")
            if error:
                raise error
            return campaigns

        out = io.StringIO()
        with (
            mock.patch.dict(os.environ, self.ENV if env is None else env, clear=True),
            mock.patch.object(bot, "load_dotenv_file", lambda path: None),
            mock.patch.object(bot, "list_campaigns", fake_list),
            mock.patch.object(bot, "send_telegram", fake_send),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            code = bot.main(argv)
        return code, sent, out.getvalue()

    def sample(self):
        return json.loads((bot.SAMPLE_DIR / "campaigns.json").read_text(encoding="utf-8"))["campaigns"]

    def test_normal_run_sends_and_saves_then_compares_next_time(self):
        with tempfile.TemporaryDirectory() as folder:
            state = str(Path(folder) / "snapshot.json")
            code, sent, _ = self.run_main(["--state-file", state], campaigns=self.sample())
            self.assertEqual(code, 0)
            self.assertIn("זה הדיווח הראשון", sent[0])
            self.assertTrue(Path(state).exists())

            later = self.sample()
            later[0]["deliveryStatus"] = "NOT_SERVING"
            later[0]["deliveryStatusReasons"] = ["AUTO_PAUSED"]
            code, sent, _ = self.run_main(["--state-file", state], campaigns=later)
            self.assertEqual(code, 0)
            self.assertTrue(sent[0].startswith("🚨"))
            self.assertIn("AUTO_PAUSED (הושהה אוטומטית)", sent[0])

    def test_roblox_failure_is_reported_to_telegram_and_state_untouched(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "snapshot.json"
            error = bot.RobloxApiError(429, "RATE_LIMITED", "Too many requests", 5)
            code, sent, _ = self.run_main(["--state-file", str(state)], error=error)
            self.assertEqual(code, 1)
            self.assertEqual(len(sent), 1)
            self.assertIn("שגיאה במשיכת נתוני הקמפיינים", sent[0])
            self.assertIn("HTTP 429 · RATE_LIMITED", sent[0])
            self.assertFalse(state.exists())

    def test_missing_api_key_is_reported_too(self):
        env = {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "1"}
        code, sent, _ = self.run_main([], env=env)
        self.assertEqual(code, 1)
        self.assertIn("ROBLOX_API_KEY לא מוגדר", sent[0])
        self.assertNotIn("תקלה זמנית", sent[0])  # it is a setup problem, not an outage

    def test_failed_delivery_does_not_save_state(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "snapshot.json"
            code, _, _ = self.run_main(
                ["--state-file", str(state)], campaigns=self.sample(), telegram_error=bot.TelegramError("HTTP 400")
            )
            self.assertEqual(code, 1)
            self.assertFalse(state.exists())

    def test_dry_run_prints_and_neither_sends_nor_saves(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "snapshot.json"
            code, sent, output = self.run_main(
                ["--dry-run", "--state-file", str(state)], env={"ROBLOX_API_KEY": "key"}, campaigns=self.sample()
            )
            self.assertEqual(code, 0)
            self.assertEqual(sent, [])
            self.assertIn("Sky Tycoon", output)
            self.assertFalse(state.exists())

    def test_missing_telegram_settings_fail_fast(self):
        code, sent, _ = self.run_main([], env={"ROBLOX_API_KEY": "key"})
        self.assertEqual(code, 2)
        self.assertEqual(sent, [])


class RepoHygieneTests(unittest.TestCase):
    def test_bot_only_calls_read_endpoints(self):
        source = (bot.BASE_DIR / "roblox_ads_bot.py").read_text(encoding="utf-8")
        calls = re.findall(r'request_with_retry\(\s*session,\s*"(\w+)",\s*(\w+)', source)
        self.assertEqual(sorted(calls), [("GET", "url"), ("POST", "url")])  # Roblox GET, Telegram POST
        self.assertNotIn("ad.campaign:write", source)

    def test_env_file_is_ignored_by_git(self):
        ignore = (bot.BASE_DIR / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(".env", ignore)
        self.assertNotIn("state/snapshot.json", ignore)


if __name__ == "__main__":
    unittest.main()
