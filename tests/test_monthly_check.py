import datetime as dt
import os
import unittest

from tests._stubs import install_pyupbit_stub

install_pyupbit_stub()
os.environ["DRY_RUN"] = "1"

import monthly_check as mc  # noqa: E402


class TestPerf(unittest.TestCase):
    def test_returns_and_drawdown(self):
        st = {"eq_log": [["2026-01-01", 1_000_000], ["2026-01-15", 1_200_000], ["2026-02-01", 900_000]]}
        s = mc.eq_series(st, 1_100_000, "2026-03-05")
        pf = mc.perf(s, 1_000_000, "2026-03-05")
        self.assertAlmostEqual(pf["r_all"], 0.10)
        self.assertAlmostEqual(pf["r30"], 1_100_000 / 900_000 - 1)     # 30일 전 기준점 = 02-01
        self.assertAlmostEqual(pf["mdd"], 900_000 / 1_200_000 - 1)
        self.assertIsNone(pf["r365"])

    def test_legacy_state_without_eq_log(self):
        st = {"eq_hist": [700_000.0], "last_day": "2026-10-06"}
        s = mc.eq_series(st, 710_000, "2026-11-01")
        self.assertEqual(len(s), 2)
        self.assertAlmostEqual(mc.perf(s, 700_000, "2026-11-01")["r30"], 710_000 / 700_000 - 1)


class TestChecks(unittest.TestCase):
    def test_levels(self):
        base = {"mdd": -0.05, "r365": None}
        self.assertEqual(mc.perf_checks(base, -0.03), [])
        self.assertEqual(mc.perf_checks({**base, "mdd": -0.22}, -0.1)[0][0], mc.WARN)
        self.assertEqual(mc.perf_checks(base, -0.31)[0][0], mc.STOP)
        self.assertEqual(mc.perf_checks({**base, "r365": -0.1}, 0)[0][0], mc.STOP)

    def test_calendar(self):
        self.assertEqual(mc.calendar_notes(dt.date(2027, 2, 1)), [])
        self.assertIn("분기", mc.calendar_notes(dt.date(2027, 4, 1))[0][1])
        self.assertIn("연간", mc.calendar_notes(dt.date(2027, 1, 1))[0][1])


class TestTrackingAndExecution(unittest.TestCase):
    def test_bot_order_time_window(self):
        self.assertTrue(mc.is_bot_order_time("2026-10-13T09:01:02+09:00"))
        self.assertTrue(mc.is_bot_order_time("2026-10-13T09:14:59+09:00"))
        self.assertFalse(mc.is_bot_order_time("2026-10-13T09:15:00+09:00"))
        self.assertFalse(mc.is_bot_order_time("2026-10-13T21:10:00+09:00"))

    def test_slippage_sign_and_weighting(self):
        opens = {("KRW-A", "2026-10-13"): 100.0, ("KRW-B", "2026-10-13"): 200.0}
        fills = [{"t": "KRW-A", "day": "2026-10-13", "side": "bid", "funds": 1000.0, "avg": 100.1},   # 매수 비싸게 +10bp
                 {"t": "KRW-B", "day": "2026-10-13", "side": "ask", "funds": 3000.0, "avg": 199.8},   # 매도 싸게 +10bp
                 {"t": "KRW-C", "day": "2026-10-13", "side": "bid", "funds": 500.0, "avg": 1.0}]       # 시가 없음 → 제외
        n, bp = mc.slippage_bp(fills, opens)
        self.assertEqual(n, 2)
        self.assertAlmostEqual(bp, 10.0, places=6)
        self.assertIsNone(mc.slippage_bp([], opens))

    def test_tracking_compares_same_period(self):
        import backtest_v5
        from tests.test_strategy_v5 import make_frames
        close, value = make_frames(n=400, tickers=("KRW-BTC", "KRW-ETH", "KRW-SOL"))
        f = {"close": close, "open": close.shift(1).fillna(close), "value": value}
        d0, d1 = close.index[-20], close.index[-1]
        st = {"eq_log": [[str(d0.date()), 1000], [str(d1.date()), 1100]]}
        start, live, shadow = mc.tracking(st, f)
        self.assertEqual(start, str(d0.date()))
        self.assertAlmostEqual(live, 0.10)
        expect = backtest_v5.run(f, mc.bot_v5.PARAMS, start=start)["eq"]
        expect = expect[expect.index.normalize() < d1.normalize()].iloc[-1] / backtest_v5.START_KRW - 1
        self.assertAlmostEqual(shadow, expect)
        self.assertIsNone(mc.tracking({"eq_log": [[str(d0.date()), 1000]]}, f))


if __name__ == "__main__":
    unittest.main()
