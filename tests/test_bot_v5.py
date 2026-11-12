import datetime as dt
import json
import os
import tempfile
import unittest
from unittest import mock

import pandas as pd

from tests._stubs import install_pyupbit_stub

install_pyupbit_stub()
os.environ["DRY_RUN"] = "1"

import bot_v5  # noqa: E402
import strategy_v5  # noqa: E402
from tests.test_strategy_v5 import make_frames  # noqa: E402

KST = bot_v5.KST


def new_state(cash=1_000_000):
    st = bot_v5.new_state()
    st["cash"] = float(cash)
    return st


class TestDayKey(unittest.TestCase):
    def test_boundary_at_0900_kst(self):
        self.assertEqual(bot_v5.day_key_now(dt.datetime(2026, 10, 6, 8, 59, tzinfo=KST)), "2026-10-05")
        self.assertEqual(bot_v5.day_key_now(dt.datetime(2026, 10, 6, 9, 0, tzinfo=KST)), "2026-10-06")

    def test_utc_server_gives_same_answer(self):
        utc = dt.timezone.utc
        self.assertEqual(bot_v5.day_key_now(dt.datetime(2026, 10, 5, 23, 59, tzinfo=utc)), "2026-10-05")
        self.assertEqual(bot_v5.day_key_now(dt.datetime(2026, 10, 6, 0, 0, tzinfo=utc)), "2026-10-06")


class TestDryBroker(unittest.TestCase):
    def test_round_trip_costs(self):
        st = new_state()
        b = bot_v5.Broker(st)
        qty = b.buy("KRW-BTC", 100_000, 50_000_000)
        self.assertAlmostEqual(st["cash"], 900_000)
        self.assertEqual(b.sell("KRW-BTC", qty, 50_000_000), qty)
        self.assertLess(st["cash"], 1_000_000)
        self.assertGreater(st["cash"], 995_000)


class TestRebalance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name in ("STATE_FILE", "LOG_FILE"):
            patcher = mock.patch.object(bot_v5, name, os.path.join(self.tmp.name, name))
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_buys_to_target_then_saves(self):
        st = new_state()
        b = bot_v5.Broker(st)
        prices = {"KRW-BTC": 100.0, "KRW-ETH": 10.0}
        bot_v5.rebalance(st, b, prices, pd.Series({"KRW-BTC": 0.30, "KRW-ETH": 0.20}))
        eq = bot_v5.equity(st, prices)
        self.assertAlmostEqual(st["positions"]["KRW-BTC"]["qty"] * 100 / eq, 0.30, delta=0.01)
        self.assertAlmostEqual(st["positions"]["KRW-ETH"]["qty"] * 10 / eq, 0.20, delta=0.01)
        with open(bot_v5.STATE_FILE, encoding="utf-8") as f:
            self.assertEqual(set(json.load(f)["positions"]), {"KRW-BTC", "KRW-ETH"})

    def test_exit_and_buffer(self):
        st = new_state(cash=500_000)
        st["positions"] = {"KRW-BTC": {"qty": 3000.0, "cost": 300_000}, "KRW-ETH": {"qty": 20000.0, "cost": 200_000}}
        b = bot_v5.Broker(st)
        prices = {"KRW-BTC": 100.0, "KRW-ETH": 10.0}
        bot_v5.rebalance(st, b, prices, pd.Series({"KRW-BTC": 0.32, "KRW-ETH": 0.0}))
        self.assertNotIn("KRW-ETH", st["positions"])                       # 목표 0 → 전량 청산
        self.assertEqual(st["positions"]["KRW-BTC"]["qty"], 3000.0)         # 2%p 변화 → 버퍼로 유지
        self.assertAlmostEqual(st["cash"], 500_000 + 200_000 * (1 - 0.001) * (1 - 0.0005))

    def test_partial_sell_reduces_cost(self):
        st = new_state(cash=0)
        st["positions"] = {"KRW-BTC": {"qty": 10_000.0, "cost": 800_000}}
        bot_v5.rebalance(st, bot_v5.Broker(st), {"KRW-BTC": 100.0}, pd.Series({"KRW-BTC": 0.30}))
        pos = st["positions"]["KRW-BTC"]
        self.assertLess(pos["qty"], 10_000)
        self.assertAlmostEqual(pos["cost"] / pos["qty"], 80.0)                # 평균 매입가는 그대로

    def test_sells_before_buys(self):
        st = new_state(cash=0)
        st["positions"] = {"KRW-ETH": {"qty": 100_000.0, "cost": 1_000_000}}
        b = bot_v5.Broker(st)
        bot_v5.rebalance(st, b, {"KRW-BTC": 100.0, "KRW-ETH": 10.0}, pd.Series({"KRW-BTC": 0.30}))
        self.assertNotIn("KRW-ETH", st["positions"])
        self.assertGreater(st["positions"]["KRW-BTC"]["qty"], 0)            # 매도 대금으로 매수

    def test_daily_end_to_end(self):
        close, value = make_frames(tickers=tuple(strategy_v5.UNIVERSE[:4]))
        day = close.index[-1] + pd.Timedelta(days=1)

        def ohlcv(t, interval="day", count=400):
            df = pd.DataFrame({"close": close[t], "value": value[t]})
            today = pd.DataFrame({"close": [close[t].iloc[-1]], "value": [0.0]}, index=[day])
            return pd.concat([df, today]).iloc[-count:]

        prices = {t: float(close[t].iloc[-1]) for t in close.columns}
        st = new_state()
        with mock.patch.object(bot_v5, "UNIVERSE", list(close.columns)), \
                mock.patch.object(bot_v5, "fetch_market_events", return_value=market_events(close.columns)), \
                mock.patch.object(bot_v5.pyupbit, "get_ohlcv", side_effect=ohlcv), \
                mock.patch.object(bot_v5.pyupbit, "get_current_price", return_value=prices), \
                mock.patch.object(bot_v5.time, "sleep"):
            fr = bot_v5.fetch_frames(str(day.date()), list(close.columns))
            self.assertEqual(fr[0].index[-1], close.index[-1])            # 진행 중 일봉 제거
            self.assertIsNone(bot_v5.fetch_frames(str((day + pd.Timedelta(days=1)).date()), list(close.columns)))
            with mock.patch.object(bot_v5, "fetch_frames", return_value=fr):
                self.assertTrue(bot_v5.daily(st, bot_v5.Broker(st), str(day.date())))
        self.assertEqual(len(st["eq_hist"]), 1)
        self.assertGreater(st["eq_hist"][0], 0)

    def test_mode_mismatch_refuses_to_start(self):
        st = new_state()
        st["mode"] = "LIVE"
        with open(bot_v5.STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f)
        with self.assertRaises(SystemExit):
            bot_v5.main()


def market_events(tickers, warn=(), missing=()):
    """fetch_market_events 가짜 응답 (정상 응답으로 인정되도록 KRW 마켓 60개 이상)."""
    ev = {f"KRW-X{i}": {"warning": False, "caution": []} for i in range(60)}
    ev.update({t: {"warning": t in warn, "caution": []} for t in tickers if t not in missing})
    return ev


class TestMarketScreen(unittest.TestCase):
    """투자유의 자동 제외: 매수 대상에서 빠지고, 보유 중이면 전량 매도."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name in ("STATE_FILE", "LOG_FILE"):
            p = mock.patch.object(bot_v5, name, os.path.join(self.tmp.name, name))
            p.start()
            self.addCleanup(p.stop)
        self.close, self.value = make_frames(tickers=("KRW-BTC", "KRW-ETH", "KRW-SOL", "KRW-ADA"))
        self.day = str((self.close.index[-1] + pd.Timedelta(days=1)).date())
        self.prices = {t: float(self.close[t].iloc[-1]) for t in self.close.columns}

    def run_daily(self, st, events):
        calls = {}

        def fake_fetch(day_key, universe=(), held=(), skip_ok=False):
            calls["universe"], calls["held"] = list(universe), set(held)
            cols = [t for t in universe if t in self.close.columns]
            return self.close[cols], self.value[cols]

        with mock.patch.object(bot_v5, "UNIVERSE", list(self.close.columns)), \
                mock.patch.object(bot_v5, "fetch_market_events", return_value=events), \
                mock.patch.object(bot_v5, "fetch_frames", side_effect=fake_fetch), \
                mock.patch.object(bot_v5.pyupbit, "get_current_price", return_value=self.prices):
            self.assertTrue(bot_v5.daily(st, bot_v5.Broker(st), self.day))
        return calls

    def test_warned_held_coin_is_sold_and_not_bought(self):
        st = new_state()
        st["positions"] = {"KRW-ETH": {"qty": 1000.0, "cost": 100_000}}
        calls = self.run_daily(st, market_events(self.close.columns, warn={"KRW-ETH"}))
        self.assertNotIn("KRW-ETH", calls["universe"])
        self.assertNotIn("KRW-ETH", st["positions"])
        self.assertEqual(st["warned"], ["KRW-ETH"])

    def test_lookup_failure_reuses_last_warned_list(self):
        st = new_state()
        st["warned"] = ["KRW-SOL"]
        calls = self.run_daily(st, None)
        self.assertNotIn("KRW-SOL", calls["universe"])
        self.assertEqual(st["warned"], ["KRW-SOL"])

    def test_delisted_held_coin_leaves_ledger(self):
        st = new_state()
        st["positions"] = {"KRW-ADA": {"qty": 10.0, "cost": 1_000}}
        self.run_daily(st, market_events(self.close.columns, missing={"KRW-ADA"}))
        self.assertNotIn("KRW-ADA", st["positions"])

    def test_btc_always_fetched_for_regime(self):
        calls = self.run_daily(new_state(), market_events(self.close.columns, warn={"KRW-BTC"}))
        self.assertIn("KRW-BTC", calls["universe"])


class TestFetchFrames(unittest.TestCase):
    """일봉 조회 실패(pyupbit 가 None 반환) 시 보유 종목이 전량 매도되지 않아야 한다."""

    def setUp(self):
        self.close, self.value = make_frames(tickers=("KRW-BTC", "KRW-ETH", "KRW-SOL"))
        self.day = self.close.index[-1] + pd.Timedelta(days=1)
        self.failing = set()
        for name, obj in (("get_ohlcv", self.ohlcv), ("sleep", None)):
            target = bot_v5.pyupbit if name == "get_ohlcv" else bot_v5.time
            p = mock.patch.object(target, name, side_effect=obj) if obj else mock.patch.object(target, name)
            p.start()
            self.addCleanup(p.stop)

    def ohlcv(self, t, interval="day", count=400):
        if t in self.failing:
            return None
        df = pd.DataFrame({"close": self.close[t], "value": self.value[t]})
        today = pd.DataFrame({"close": [self.close[t].iloc[-1]], "value": [0.0]}, index=[self.day])
        return pd.concat([df, today]).iloc[-count:]

    def fetch(self, held=(), skip_ok=False):
        return bot_v5.fetch_frames(str(self.day.date()), list(self.close.columns), held, skip_ok)

    def test_held_ticker_failure_defers_day(self):
        self.failing = {"KRW-SOL"}
        self.assertIsNone(self.fetch(held={"KRW-SOL"}))

    def test_unheld_ticker_failure_waits_then_is_excluded(self):
        self.failing = {"KRW-SOL"}
        self.assertIsNone(self.fetch(held={"KRW-ETH"}))                      # 처음엔 기다림 (순위가 달라지지 않게)
        fr = self.fetch(held={"KRW-ETH"}, skip_ok=True)                      # 유예 시간 뒤엔 빼고 진행
        self.assertEqual(list(fr[0].columns), ["KRW-BTC", "KRW-ETH"])

    def test_stale_candle_unheld_excluded_only_after_grace(self):
        real = self.ohlcv

        def stale_sol(t, **k):
            df = real(t, **k)
            return df.iloc[:-1] if t == "KRW-SOL" else df                    # SOL 만 오늘 일봉이 아직 없음

        with mock.patch.object(bot_v5.pyupbit, "get_ohlcv", side_effect=stale_sol):
            self.assertIsNone(self.fetch())
            self.assertEqual(list(self.fetch(skip_ok=True)[0].columns), ["KRW-BTC", "KRW-ETH"])
            self.assertIsNone(self.fetch(held={"KRW-SOL"}, skip_ok=True))   # 보유 종목은 끝까지 기다림

    def test_minutes_into_day(self):
        self.assertEqual(bot_v5.minutes_into_day(dt.datetime(2026, 10, 6, 9, 0, tzinfo=KST)), 0)
        self.assertEqual(bot_v5.minutes_into_day(dt.datetime(2026, 10, 6, 9, 25, tzinfo=KST)), 25)
        self.assertEqual(bot_v5.minutes_into_day(dt.datetime(2026, 10, 7, 8, 59, tzinfo=KST)), 23 * 60 + 59)

    def test_transient_failure_is_retried(self):
        calls = {"n": 0}
        real = self.ohlcv

        def flaky(t, **k):
            if t == "KRW-SOL" and calls["n"] < 2:
                calls["n"] += 1
                return None
            return real(t, **k)

        with mock.patch.object(bot_v5.pyupbit, "get_ohlcv", side_effect=flaky):
            fr = self.fetch(held={"KRW-SOL"})
        self.assertIn("KRW-SOL", fr[0].columns)


class TestStall(unittest.TestCase):
    def setUp(self):
        bot_v5._last_alert.clear()

    def test_alerts_only_after_grace_and_throttled(self):
        st = new_state()
        st["last_day"] = "2026-10-05"
        with mock.patch.object(bot_v5, "log") as log:
            self.assertFalse(bot_v5.check_stall(st, "2026-10-06", dt.datetime(2026, 10, 6, 9, 30, tzinfo=KST)))
            self.assertTrue(bot_v5.check_stall(st, "2026-10-06", dt.datetime(2026, 10, 6, 10, 5, tzinfo=KST)))
            self.assertTrue(bot_v5.check_stall(st, "2026-10-06", dt.datetime(2026, 10, 7, 3, 0, tzinfo=KST)))
            st["last_day"] = "2026-10-06"
            self.assertFalse(bot_v5.check_stall(st, "2026-10-06", dt.datetime(2026, 10, 6, 15, 0, tzinfo=KST)))
        alerts = [c for c in log.call_args_list if c.kwargs.get("alert")]
        self.assertEqual(len(alerts), 1)                                     # 쿨다운 동안 한 번만 알림


class FakeUpbit:
    """LIVE 브로커 테스트용 가짜 업비트. order=None 이면 주문 조회 실패를 흉내 낸다."""

    def __init__(self, krw, coin, order):
        self.b = {"KRW": krw, "BTC": coin}
        self.order = order

    def get_balance(self, cur):
        return self.b[cur]

    def buy_market_order(self, t, krw):
        return {"uuid": "u-1"}

    def sell_market_order(self, t, q):
        return {"uuid": "u-2"}

    def get_individual_order(self, uuid):
        return self.order


class TestLiveBroker(unittest.TestCase):
    def broker(self, up, cash=100_000):
        st = new_state(cash)
        b = bot_v5.Broker(st)
        b.up = up
        for p in (mock.patch.object(bot_v5, "DRY_RUN", False), mock.patch.object(bot_v5.time, "sleep"),
                  mock.patch.object(bot_v5, "FILL_WAIT_SEC", 0.05), mock.patch.object(bot_v5, "log")):
            p.start()
            self.addCleanup(p.stop)
        return st, b

    def test_buy_uses_order_fill(self):
        order = {"state": "cancel", "executed_volume": "0.001", "paid_fee": "25",
                 "trades": [{"funds": "30000"}, {"funds": "19990"}]}
        st, b = self.broker(FakeUpbit(100_000, 0.0, order))
        self.assertAlmostEqual(b.buy("KRW-BTC", 50_000, 50_000_000), 0.001)
        self.assertAlmostEqual(st["cash"], 100_000 - 49_990 - 25)

    def test_sell_uses_order_fill(self):
        order = {"state": "done", "executed_volume": "0.002", "paid_fee": "50", "trades": [{"funds": "100000"}]}
        st, b = self.broker(FakeUpbit(0, 0.002, order), cash=0)
        self.assertAlmostEqual(b.sell("KRW-BTC", 0.002, 50_000_000), 0.002)
        self.assertAlmostEqual(st["cash"], 100_000 - 50)

    def test_buy_fallback_never_loses_coin(self):
        """주문 조회 실패 + 코인 잔고 반영 지연이어도 돈만 빠지고 수량 0 으로 기록되면 안 된다."""
        up = FakeUpbit(100_000, 0.0, None)
        orig = up.buy_market_order

        def buy(t, krw):
            up.b["KRW"] -= 50_000
            return orig(t, krw)

        up.buy_market_order = buy
        st, b = self.broker(up)
        self.assertGreater(b.buy("KRW-BTC", 50_000, 50_000_000), 0)
        self.assertAlmostEqual(st["cash"], 50_000)

    def test_sell_fallback_no_double_count(self):
        """대금은 들어왔는데 코인 잔고가 그대로면 판 것으로 처리 (코인·현금 이중 기록 방지)."""
        up = FakeUpbit(0, 0.002, None)
        orig = up.sell_market_order

        def sell(t, q):
            up.b["KRW"] += 99_950
            return orig(t, q)

        up.sell_market_order = sell
        st, b = self.broker(up, cash=0)
        self.assertAlmostEqual(b.sell("KRW-BTC", 0.002, 50_000_000), 0.002)
        self.assertAlmostEqual(st["cash"], 99_950)


class TestSafety(unittest.TestCase):
    def test_protected_symbols_not_traded(self):
        self.assertFalse(bot_v5.PROTECTED & set(strategy_v5.UNIVERSE))
        self.assertIn("KRW-XRP", bot_v5.PROTECTED)               # 계좌에서 따로 보유하는 종목
        self.assertIs(bot_v5.PARAMS, strategy_v5.LIVE)

    def test_tests_never_use_real_telegram(self):
        """프로젝트 .env 에 실제 토큰이 있어도 테스트 중에는 텔레그램으로 아무것도 보내지 않아야 한다."""
        self.assertEqual(os.environ.get("TELEGRAM_TOKEN"), "")
        self.assertEqual(os.environ.get("UPBIT_ACCESS"), "")
        self.assertFalse(bot_v5.HEALTHCHECK_URL)
        with mock.patch.object(bot_v5.requests, "post") as post:
            bot_v5.notify("x")
        post.assert_not_called()

    def test_dotenv_does_not_override(self):
        with tempfile.TemporaryDirectory() as d:
            fp = os.path.join(d, ".env")
            with open(fp, "w", encoding="utf-8") as f:
                f.write("# c\nV5_TEST_A=1  # note\nV5_TEST_B='x'\nV5_TEST_EMPTY=\n")
            with mock.patch.dict(os.environ, {"V5_TEST_A": "keep"}):
                bot_v5.load_dotenv(fp)
                self.assertEqual(os.environ["V5_TEST_A"], "keep")
                self.assertEqual(os.environ["V5_TEST_B"], "x")
                self.assertNotIn("V5_TEST_EMPTY", os.environ)


class TestNotify(unittest.TestCase):
    def test_telegram_error_is_logged(self):
        resp = mock.Mock(ok=False, status_code=400, headers={"content-type": "application/json"})
        resp.json.return_value = {"ok": False, "description": "Bad Request: chat not found"}
        with mock.patch.dict(os.environ, {"TELEGRAM_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}),                 mock.patch.object(bot_v5.requests, "post", return_value=resp),                 mock.patch.object(bot_v5.logging, "warning") as w:
            bot_v5.notify("x")
        self.assertIn("chat not found", w.call_args[0][0])


if __name__ == "__main__":
    unittest.main()
