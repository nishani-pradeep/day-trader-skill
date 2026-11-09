import datetime
import gzip
import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from unittest import mock
from contextlib import redirect_stdout, redirect_stderr

from _load import load, ROOT, FIX

alerts = load("trader-alerts", "alerts")

QDIR = os.path.join(FIX, "trader-alerts")
PLAN = os.path.join(FIX, "trader-budget", "plan-tsla.json")
DT = datetime.datetime
UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0; +https://github.com/Jakeschincariol/day-trader-skill)"

# NSE trading holidays as published at https://www.nseindia.com/resources/exchange-communication-holidays
# (read 2026-11-09); weekday ones only
NSE_2026 = ["2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26", "2026-03-31", "2026-04-03", "2026-04-14",
            "2026-05-01", "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02", "2026-10-20", "2026-11-10",
            "2026-11-24", "2026-12-25"]
# a made-up early close: the NSE has none scheduled, the code for one stays
FAKE_EARLY = {"2026-11-27": "a test early close"}


def ist(text):
    return alerts.parse_now(text)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = alerts.main(argv)
    return code, out.getvalue(), err.getvalue()


def plan_rules():
    with open(PLAN) as fh:
        return alerts.rules_from_plan(json.load(fh))


class Calendar(unittest.TestCase):
    def test_holidays_match_the_published_list(self):
        self.assertEqual(sorted(alerts.NSE_HOLIDAYS), NSE_2026)
        self.assertEqual(alerts.NSE_EARLY_CLOSE, {})
        self.assertEqual(alerts.CALENDAR_YEARS, (2026,))
        for d in alerts.NSE_HOLIDAYS:
            self.assertLess(datetime.date.fromisoformat(d).weekday(), 5, d)

    def test_india_time_has_no_daylight_saving(self):
        for utc in (DT(2026, 3, 8, 7, 0), DT(2026, 7, 1, 0, 0), DT(2026, 11, 1, 6, 0)):
            self.assertEqual(alerts.to_ist(utc) - utc, datetime.timedelta(hours=5, minutes=30))
        self.assertEqual(alerts.to_ist(DT(2026, 10, 19, 3, 45)), DT(2026, 10, 19, 9, 15))
        self.assertEqual(alerts.to_ist(DT(2026, 10, 19, 20, 0)), DT(2026, 10, 20, 1, 30))
        self.assertEqual(alerts.ist_to_utc(DT(2026, 10, 19, 15, 30)), DT(2026, 10, 19, 10, 0))
        self.assertEqual((alerts.OPEN, alerts.CLOSE), (datetime.time(9, 15), datetime.time(15, 30)))

    def test_parse_now(self):
        self.assertEqual(ist("2026-10-13T09:15"), DT(2026, 10, 13, 3, 45))
        self.assertEqual(ist("2026-10-19 10:15"), DT(2026, 10, 19, 4, 45))
        self.assertEqual(ist("2026-12-01T09:30"), DT(2026, 12, 1, 4, 0))
        self.assertEqual(ist("2026-10-13T20:00:01Z"), DT(2026, 10, 13, 20, 0, 1))
        with self.assertRaises(alerts.AlertError):
            ist("tomorrow")

    def test_market_status(self):
        st = alerts.market_status(ist("2026-10-13T10:00"))
        self.assertTrue(st["open"])
        self.assertEqual((st["opens"], st["closes"]), ("09:15", "15:30"))
        self.assertTrue(alerts.market_status(ist("2026-10-13T09:15"))["open"])
        self.assertFalse(alerts.market_status(ist("2026-10-13T09:14"))["open"])
        self.assertEqual(alerts.market_status(ist("2026-10-13T09:14"))["next_open"], "2026-10-13 09:15")
        self.assertTrue(alerts.market_status(ist("2026-10-13T15:29"))["open"])
        after = alerts.market_status(ist("2026-10-13T15:30"))
        self.assertFalse(after["open"])
        self.assertEqual(after["next_open"], "2026-10-14 09:15")
        gandhi = alerts.market_status(ist("2026-10-02T11:00"))
        self.assertFalse(gandhi["open"])
        self.assertEqual(gandhi["reason"], "NSE holiday: Mahatma Gandhi Jayanti")
        self.assertEqual(gandhi["next_open"], "2026-10-05 09:15")
        xmas = alerts.market_status(ist("2026-12-25T10:00"))
        self.assertFalse(xmas["open"])
        self.assertEqual(xmas["next_open"], "2026-12-28 09:15")
        self.assertEqual(alerts.market_status(ist("2026-10-17T10:00"))["reason"], "weekend")
        self.assertTrue(alerts.market_status(ist("2026-12-31T10:00"))["calendar_known"])
        self.assertFalse(alerts.market_status(ist("2027-03-01T10:00"))["calendar_known"])

    def test_the_early_close_code_still_works(self):
        self.assertIsNone(alerts.market_status(ist("2026-11-27T12:59"))["early_close"])
        with mock.patch.dict(alerts.NSE_EARLY_CLOSE, FAKE_EARLY):
            self.assertTrue(alerts.market_status(ist("2026-11-27T12:59"))["open"])
            self.assertFalse(alerts.market_status(ist("2026-11-27T13:00"))["open"])
            self.assertEqual(alerts.market_status(ist("2026-11-27T10:00"))["early_close"], "a test early close")


class Quotes(unittest.TestCase):
    def test_yahoo_fixtures(self):
        with open(os.path.join(QDIR, "yahoo-AAPL.json")) as fh:
            q = alerts.parse_yahoo(fh.read(), "AAPL")
        self.assertEqual(q["price"], 341.27)
        self.assertEqual(q["prev_close"], 340.97)
        self.assertEqual(q["time"], "2026-10-13T10:15:01Z")
        with open(os.path.join(QDIR, "yahoo-TSLA.json")) as fh:
            q = alerts.parse_yahoo(fh.read(), "TSLA")
        self.assertEqual((q["price"], q["prev_close"], round(q["open"], 2)), (366.475, 354.72, 357.48))
        self.assertEqual(q["time"], "2026-10-19T05:07:13Z")

    def test_stooq_fixture(self):
        with open(os.path.join(QDIR, "stooq-AAPL.csv")) as fh:
            q = alerts.parse_stooq(fh.read(), "AAPL")
        self.assertEqual(q["price"], 341.27)
        self.assertEqual(q["open"], 344.67)
        with open(os.path.join(QDIR, "stooq-ZZZZ.csv")) as fh:
            with self.assertRaises(alerts.AlertError):
                alerts.parse_stooq(fh.read(), "ZZZZ")

    def test_urls_and_user_agent(self):
        self.assertEqual(alerts.UA, UA)
        self.assertEqual(alerts.stooq_url("BRK.B"), "https://stooq.com/q/l/?s=brk-b.us&f=sd2t2ohlcv&h&e=csv")
        self.assertEqual(alerts.yahoo_url("brk.b"),
                         "https://query2.finance.yahoo.com/v8/finance/chart/BRK-B?range=1d&interval=1d")
        self.assertIn("//query1.finance.yahoo.com/", alerts.yahoo_url("TSLA", alerts.YAHOO_HOSTS[1]))

    def test_ns_symbols_are_kept_as_they_are(self):
        for sym in ("RELIANCE.NS", "TCS.NS", "M&M.NS", "BAJAJ-AUTO.NS"):
            self.assertEqual(alerts.yahoo_symbol(sym), sym)
            self.assertEqual(alerts.yahoo_symbol(sym.lower()), sym)
            self.assertEqual(alerts.yahoo_url(sym.lower()),
                             "https://query2.finance.yahoo.com/v8/finance/chart/%s?range=1d&interval=1d" % sym)
        self.assertEqual(alerts.yahoo_symbol("brk.b"), "BRK-B")

    def test_offline_quotes_and_errors(self):
        q, errors = alerts.get_quotes(["AAPL", "KO", "ZZZZ", "NOPE"], "yahoo", QDIR)
        self.assertEqual(sorted(q), ["AAPL", "KO"])
        self.assertEqual(q["KO"]["price"], 90.11)
        self.assertEqual(len(errors), 2)
        self.assertIn("No data found", errors[0])

    def test_offline_gz(self):
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(QDIR, "yahoo-TSLA.json"), "rb") as src:
                with gzip.open(os.path.join(d, "yahoo-TSLA.json.gz"), "wb") as dst:
                    dst.write(src.read())
            q, errors = alerts.get_quotes(["TSLA"], "yahoo", d)
            self.assertEqual((q["TSLA"]["price"], errors), (366.475, []))
        finally:
            shutil.rmtree(d)


class FakeNet(object):
    """Stands in for urllib: answers each request with the next status code."""

    def __init__(self, codes, body=None):
        self.codes, self.calls, self.sleeps = list(codes), [], []
        with open(os.path.join(QDIR, "yahoo-TSLA.json")) as fh:
            self.body = body or fh.read()

    def urlopen(self, req, timeout=None):
        self.calls.append((req.full_url.split("/")[2], req.get_header("User-agent")))
        code = self.codes.pop(0)
        if code != 200:
            fp = io.BytesIO(b'{"chart":{"result":null,"error":{"code":"Not Found",'
                            b'"description":"No data found, symbol may be delisted"}}}' if code == 404 else b"")
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, fp)
        return io.BytesIO(self.body.encode("utf-8"))


class Network(unittest.TestCase):
    def setUp(self):
        self.saved = (alerts._urlopen, alerts._sleep)

    def tearDown(self):
        alerts._urlopen, alerts._sleep = self.saved

    def net(self, codes):
        f = FakeNet(codes)
        alerts._urlopen = f.urlopen
        alerts._sleep = f.sleeps.append
        return f

    def hosts(self, f):
        return [h.split(".")[0] for h, _ in f.calls]

    def test_query2_first_with_the_contract_user_agent(self):
        f = self.net([200])
        q, errors = alerts.get_quotes(["TSLA"])
        self.assertEqual((q["TSLA"]["price"], errors), (366.475, []))
        self.assertEqual(f.calls, [("query2.finance.yahoo.com", UA)])

    def test_one_retry_with_a_pause_on_429(self):
        f = self.net([429, 200])
        self.assertEqual(alerts.get_quotes(["TSLA"])[0]["TSLA"]["price"], 366.475)
        self.assertEqual(self.hosts(f), ["query2", "query2"])
        self.assertEqual(f.sleeps, [alerts.BACKOFF])

    def test_falls_back_to_query1_on_429_and_5xx(self):
        f = self.net([429, 429, 200])
        self.assertIn("TSLA", alerts.get_quotes(["TSLA"])[0])
        self.assertEqual(self.hosts(f), ["query2", "query2", "query1"])
        f = self.net([503, 200])
        self.assertIn("TSLA", alerts.get_quotes(["TSLA"])[0])
        self.assertEqual(self.hosts(f), ["query2", "query1"])
        f = self.net([500, 502])
        q, errors = alerts.get_quotes(["TSLA"])
        self.assertEqual(q, {})
        self.assertIn("query1.finance.yahoo.com/v8/finance/chart/TSLA answered HTTP 502", errors[0])

    def test_a_404_is_yahoos_answer_and_a_403_is_not_retried(self):
        f = self.net([404])
        q, errors = alerts.get_quotes(["NOPE"])
        self.assertIn("No data found", errors[0])
        self.assertEqual(self.hosts(f), ["query2"])
        f = self.net([403])
        q, errors = alerts.get_quotes(["TSLA"])
        self.assertIn("HTTP 403", errors[0])
        self.assertEqual(self.hosts(f), ["query2"])

    def test_a_pause_between_tickers(self):
        f = self.net([200, 200])
        alerts.get_quotes(["TSLA", "AAPL"])
        self.assertEqual(f.sleeps, [alerts.PAUSE])


class Rules(unittest.TestCase):
    def setUp(self):
        self.q, _ = alerts.get_quotes(["AAPL", "BAC", "KO", "TSLA"], "yahoo", QDIR)
        self.open = alerts.market_status(ist("2026-10-13T10:00"))
        self.now = ist("2026-10-13T10:00")

    def at(self, text, rules, state=None, quotes=None):
        now = ist(text)
        return alerts.evaluate(rules, self.q if quotes is None else quotes, alerts.market_status(now),
                               state or {}, now)

    def test_a_target_rings_once(self):
        rules = [{"id": "aapl-340", "symbol": "AAPL", "type": "above", "price": 340}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertEqual(len(rings), 1)
        self.assertIn("AAPL is INR 341.27: at or above your alert level INR 340", rings[0]["message"])
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(rings, [])

    def test_stop_and_rearm(self):
        rules = [{"id": "bac-stop", "symbol": "BAC", "type": "stop", "price": 54, "rearm": True}]
        rings, st = alerts.evaluate(rules, self.q, self.open, {}, self.now)
        self.assertIn("your stop INR 54", rings[0]["message"])
        self.q["BAC"]["price"] = 55.0
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(rings, [])
        self.assertNotIn("bac-stop", st["rung"])
        self.q["BAC"]["price"] = 53.5
        rings, st = alerts.evaluate(rules, self.q, self.open, st, self.now)
        self.assertEqual(len(rings), 1)

    def test_pct_move_from_previous_close(self):
        big = [{"id": "bac-3", "symbol": "BAC", "type": "pct_move", "pct": 3}]
        self.assertEqual(alerts.evaluate(big, self.q, self.open, {}, self.now)[0], [])
        small = [{"id": "bac-1", "symbol": "BAC", "type": "pct_move", "pct": 1, "direction": "down"}]
        rings, st = alerts.evaluate(small, self.q, self.open, {}, self.now)
        self.assertIn("-1.14%", rings[0]["message"])
        self.assertEqual(alerts.evaluate(small, self.q, self.open, st, self.now)[0], [])
        up = [{"id": "bac-up", "symbol": "BAC", "type": "pct_move", "pct": 1, "direction": "up"}]
        self.assertEqual(alerts.evaluate(up, self.q, self.open, {}, self.now)[0], [])
        tsla = [{"id": "tsla-3", "symbol": "TSLA", "type": "pct_move", "pct": 3}]
        rings, _ = self.at("2026-10-19 10:37", tsla)
        self.assertIn("TSLA moved +3.31% to INR 366.48 (from INR 354.72)", rings[0]["message"])

    def test_opening_bell_once_per_trading_day(self):
        rules = [{"id": "bell", "type": "opening_bell"}]
        closed = alerts.market_status(ist("2026-10-13T09:14"))
        self.assertEqual(alerts.evaluate(rules, {}, closed, {}, ist("2026-10-13T09:14"))[0], [])
        rings, st = alerts.evaluate(rules, {}, self.open, {}, self.now)
        self.assertIn("Opening bell: the NSE is open (2026-10-13 10:00 IST)", rings[0]["message"])
        self.assertEqual(alerts.evaluate(rules, {}, self.open, st, ist("2026-10-13T11:00"))[0], [])
        nxt = alerts.market_status(ist("2026-10-14T09:16"))
        self.assertEqual(len(alerts.evaluate(rules, {}, nxt, st, ist("2026-10-14T09:16"))[0]), 1)

    def test_the_plan_gives_a_stop_a_target_and_flat_by_close(self):
        rules = plan_rules()
        self.assertEqual([(r["id"], r["type"]) for r in rules],
                         [("tsla-plan-stop", "stop"), ("tsla-plan-target", "target"), ("tsla-flat-by", "time")])
        self.assertEqual((rules[0]["price"], rules[1]["price"]), (358.32, 370.04))
        self.assertEqual((rules[2]["at"], rules[2]["label"], rules[2]["before_close"]), ("15:15", "flat by close", True))
        self.assertTrue(all(r["date"] == "2026-10-19" for r in rules))
        rings, st = self.at("2026-10-19 10:37", rules)          # the 10:37 quote, 366.475
        self.assertEqual(rings, [])
        q = json.loads(json.dumps(self.q))
        q["TSLA"]["price"] = 358.10
        rings, st = self.at("2026-10-19 11:30", rules, st, q)
        self.assertEqual([r["id"] for r in rings], ["tsla-plan-stop"])
        self.assertIn("TSLA is INR 358.10: at or below your stop INR 358.32 (plan stop: opening-range midpoint)",
                      rings[0]["message"])
        q["TSLA"]["price"] = 370.04
        self.assertEqual([r["id"] for r in self.at("2026-10-19 12:00", rules, st, q)[0]], ["tsla-plan-target"])

    def test_flat_by_close_rings_once_per_trading_day(self):
        rules = plan_rules()[2:]
        self.assertEqual(self.at("2026-10-19 15:14", rules)[0], [])
        rings, st = self.at("2026-10-19 15:15", rules)
        self.assertEqual(len(rings), 1)
        self.assertIn("Flat by close: it is 15:15 IST (set for 15:15), the market closes at 15:30", rings[0]["message"])
        self.assertIn("trader-broker flatten --symbol TSLA", rings[0]["message"])
        self.assertEqual(self.at("2026-10-19 15:29", rules, st)[0], [])
        rings, _ = self.at("2026-10-19 15:40", rules)
        self.assertIn("the market has closed", rings[0]["message"])
        self.assertEqual(self.at("2026-10-21 15:16", rules, st)[0], [])     # plan rules: their day only

    def test_a_time_alert_moves_with_an_early_close(self):
        rule = [{"id": "flat", "type": "time", "at": "15:15", "label": "flat by close", "before_close": True}]
        with mock.patch.dict(alerts.NSE_EARLY_CLOSE, FAKE_EARLY):
            self.assertEqual(self.at("2026-11-27 12:14", rule)[0], [])
            rings, _ = self.at("2026-11-27 12:15", rule)
            self.assertIn("set for 12:15), the market closes at 13:00", rings[0]["message"])
            plain = [{"id": "t", "type": "time", "at": "15:15", "label": "check"}]
            self.assertEqual(self.at("2026-11-27 12:15", plain)[0], [])
        self.assertEqual(self.at("2026-10-17 15:16", rule)[0], [])         # Saturday: no session
        self.assertEqual(self.at("2026-11-24 15:16", rule)[0], [])         # Guru Nanak Jayanti
        rings, st = self.at("2026-12-01 15:15", rule)                      # no early close: the 15:15 itself
        self.assertEqual(len(rings), 1)
        self.assertIn("set for 15:15), the market closes at 15:30", rings[0]["message"])
        self.assertEqual(st["rung"]["flat"], "2026-12-01T09:45:00Z")

    def test_the_example_rules_are_valid(self):
        with open(os.path.join(ROOT, "trader-alerts", "alerts.example.json")) as fh:
            rules = json.load(fh)["rules"]
        for r in rules:
            alerts.validate(r)
        self.assertEqual([r for r in rules if r["id"].startswith("tsla-plan") or r["id"] == "tsla-flat-by"],
                         [dict(r, date="2026-10-19") for r in plan_rules()])

    def test_bad_rules(self):
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "moon"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "above", "symbol": "AAPL"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.evaluate([{"id": "x", "type": "time", "at": "4pm"}], {}, self.open, {}, self.now)
        with self.assertRaises(alerts.AlertError):
            alerts.rules_from_plan({"symbol": "TSLA", "stop": 358.32})

    def test_a_ns_plan_keeps_its_symbol_and_flats_by_15_15(self):
        rules = alerts.rules_from_plan({"symbol": "reliance.ns", "stop": 2890.5, "target": 2950.0})
        self.assertEqual([(r["id"], r["symbol"]) for r in rules],
                         [("reliance.ns-plan-stop", "RELIANCE.NS"), ("reliance.ns-plan-target", "RELIANCE.NS"),
                          ("reliance.ns-flat-by", "RELIANCE.NS")])
        self.assertEqual(rules[2]["at"], "15:15")
        for r in rules:
            alerts.validate(r)


class Cli(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.rules = os.path.join(self.d, "alerts.json")
        self.state = os.path.join(self.d, "state", "alerts.json")
        self.prices = os.path.join(self.d, "prices.json")

    def tearDown(self):
        shutil.rmtree(self.d)

    def check(self, now):
        return run(["check", "--rules", self.rules, "--state", self.state, "--prices-out", self.prices,
                    "--offline", QDIR, "--now", now])

    def test_the_reel_trade_from_the_plan(self):
        code, text, _ = run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state,
                             "--config", os.path.join(self.d, "none")])
        self.assertEqual(code, 0)
        self.assertIn("Added tsla-flat-by", text)
        self.assertEqual(run(["add", "--rules", self.rules, "--opening-bell"])[0], 0)

        code, text, _ = self.check("2026-10-19 10:37")
        self.assertEqual(code, 0)                                      # the opening bell rings first
        code, text, _ = self.check("2026-10-19 10:38")
        self.assertEqual(code, 1)
        self.assertIn("Nothing hit. 4 rule(s) watched today, TSLA INR 366.48.", text)
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["prices"], {"TSLA": 366.475})
        self.assertEqual(doc["detail"]["TSLA"]["time"], "2026-10-19T05:07:13Z")
        self.assertEqual(doc["detail"]["TSLA"]["fetched"], "2026-10-19T05:08:00Z")

        code, text, _ = self.check("2026-10-19 15:15")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Flat by close", text)
        self.assertIn("not an order", text)
        self.assertEqual(self.check("2026-10-19 15:17")[0], 1)
        code, text, _ = self.check("2026-10-21 10:00")
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-10-21 10:05")
        self.assertIn("Nothing hit. 1 rule(s) watched today.", text)

        # a new plan replaces the plan rules and re-arms them
        code, text, _ = run(["add", "--from-plan", "--plan", PLAN, "--rules", self.rules, "--state", self.state])
        with open(self.rules) as fh:
            ids = [r["id"] for r in json.load(fh)["rules"]]
        self.assertEqual(sorted(ids), ["opening-bell", "tsla-flat-by", "tsla-plan-stop", "tsla-plan-target"])
        with open(self.state) as fh:
            self.assertNotIn("tsla-flat-by", json.load(fh)["rung"])

    def test_the_v1_portfolio_evening_and_morning(self):
        for args in (["--opening-bell"], ["--symbol", "AAPL", "--above", "350"], ["--symbol", "BAC", "--stop", "50"]):
            code, _, _ = run(["add", "--rules", self.rules] + args)
            self.assertEqual(code, 0)
        code, _, err = run(["add", "--rules", self.rules, "--symbol", "AAPL", "--above", "350"])
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)

        code, text, _ = self.check("2026-10-13T20:00")
        self.assertEqual(code, 1)
        self.assertIn("Nothing hit", text)
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["prices"], {"AAPL": 341.27, "BAC": 53.96})

        code, text, _ = self.check("2026-10-14T09:16")
        self.assertEqual(code, 0)
        self.assertIn("ALERT Opening bell", text)
        code, text, _ = self.check("2026-10-14T09:30")
        self.assertEqual(code, 1)

        code, text, _ = run(["list", "--rules", self.rules, "--state", self.state])
        self.assertIn("rang", text)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 0)
        self.assertEqual(run(["reset", "opening-bell", "--state", self.state])[0], 1)

    def test_add_time_and_target(self):
        code, text, _ = run(["add", "--rules", self.rules, "--at", "15:15", "--label", "flat by close",
                             "--before-close", "--symbol", "tsla"])
        self.assertEqual(code, 0)
        code, text, _ = run(["add", "--rules", self.rules, "--symbol", "TSLA", "--target", "370.04",
                             "--date", "2026-10-19"])
        self.assertEqual(code, 0)
        with open(self.rules) as fh:
            rules = json.load(fh)["rules"]
        self.assertEqual(rules[0], {"id": "time-1515", "type": "time", "at": "15:15", "label": "flat by close",
                                    "symbol": "TSLA", "before_close": True})
        self.assertEqual(rules[1], {"id": "tsla-target-370.04", "symbol": "TSLA", "type": "target",
                                    "price": 370.04, "date": "2026-10-19"})
        self.assertEqual(run(["add", "--rules", self.rules, "--at", "4pm"])[0], 1)
        code, _, err = run(["add", "--rules", self.rules, "--from-plan", "--plan", os.path.join(self.d, "none")])
        self.assertEqual(code, 1)
        self.assertIn("no plan", err)

    def test_quote_write_merges(self):
        code, text, _ = run(["quote", "AAPL", "KO", "--offline", QDIR, "--write", self.prices,
                             "--now", "2026-10-13T10:15:01Z"])
        self.assertEqual(code, 0)
        self.assertIn("AAPL   INR 341.27", text)
        self.assertIn("delayed", text)
        run(["quote", "BAC", "--offline", QDIR, "--write", self.prices])
        with open(self.prices) as fh:
            doc = json.load(fh)
        self.assertEqual(sorted(doc["prices"]), ["AAPL", "BAC", "KO"])
        self.assertEqual(doc["detail"]["KO"]["prev_close"], 89.79)
        self.assertEqual(doc["detail"]["KO"]["fetched"], "2026-10-13T10:15:01Z")

    def test_quote_holdings_from_portfolio_and_plan(self):
        pf = os.path.join(self.d, "portfolio.json")
        with open(pf, "w") as fh:
            json.dump({"cash": 0, "positions": [{"ticker": "KO", "shares": 1}]}, fh)
        code, text, _ = run(["quote", "--holdings", "--portfolio", pf, "--plan", PLAN, "--offline", QDIR, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(json.loads(text)["quotes"]), ["KO", "TSLA"])

    def test_clock_exit_codes(self):
        self.assertEqual(run(["clock", "--now", "2026-10-19 10:15"])[0], 0)
        code, text, _ = run(["clock", "--now", "2026-12-25T10:00"])
        self.assertEqual(code, 1)
        self.assertIn("NSE holiday: Christmas", text)
        self.assertIn("Next open: 2026-12-28 09:15 IST.", text)
        code, text, _ = run(["clock", "--now", "2027-01-04T10:00"])
        self.assertEqual(code, 0)
        self.assertIn("Holiday list covers 2026 only: check nseindia.com", text)

    def test_check_without_rules(self):
        code, _, err = run(["check", "--rules", self.rules, "--offline", QDIR])
        self.assertEqual(code, 1)
        self.assertIn("no rules", err)


if __name__ == "__main__":
    unittest.main()
