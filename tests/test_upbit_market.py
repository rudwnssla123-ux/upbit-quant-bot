import unittest
from unittest import mock

import upbit_market as um

B = 1e8          # 1억


def stat(days=400, w=(150 * B, 140 * B, 120 * B), max_day=None):
    return {"days": days, "windows": list(w), "max_day": max_day if max_day is not None else w[0] * 2}


class TestWindows(unittest.TestCase):
    def test_window_means_recent_first(self):
        vals = [1.0] * 30 + [2.0] * 30 + [3.0] * 30
        self.assertEqual(um.window_means(vals), [3.0, 2.0, 1.0])
        self.assertEqual(um.window_means(vals[:50]), [(10 * 1.0 + 20 * 2.0) / 30])   # 구간이 부족하면 있는 만큼만


class TestSuggest(unittest.TestCase):
    def setUp(self):
        self.ev = {m: {"warning": False, "caution": []} for m in
                   ("KRW-BTC", "KRW-TRX", "KRW-ONDO", "KRW-NEW", "KRW-USDT", "KRW-PUMP", "KRW-HOT", "KRW-LOW")}

    def test_add_candidates(self):
        stats = {"KRW-ONDO": stat(), "KRW-NEW": stat(days=200), "KRW-USDT": stat(), "KRW-LOW": stat(w=(150 * B, 90 * B, 150 * B)),
                 "KRW-PUMP": stat(max_day=2000 * B)}
        self.ev["KRW-HOT"]["caution"] = ["PRICE_FLUCTUATIONS"]
        stats["KRW-HOT"] = stat()
        adds, _ = um.suggest_universe(stats, ["KRW-BTC"], self.ev)
        names = {m: pump for m, _, pump in adds}
        self.assertIn("KRW-ONDO", names)                 # 조건 충족
        self.assertNotIn("KRW-NEW", names)               # 상장 1년 미만
        self.assertNotIn("KRW-USDT", names)              # 스테이블코인
        self.assertNotIn("KRW-LOW", names)               # 한 달이라도 100억 미만
        self.assertNotIn("KRW-HOT", names)               # 주의 지정
        self.assertTrue(names["KRW-PUMP"])               # 후보지만 펌핑 의심 표시
        self.assertFalse(names["KRW-ONDO"])

    def test_drop_candidates(self):
        stats = {"KRW-TRX": stat(w=(27 * B, 30 * B, 35 * B)), "KRW-BTC": stat(w=(1000 * B, 900 * B, 40 * B))}
        ev = dict(self.ev)
        ev["KRW-ETH"] = {"warning": True, "caution": []}
        _, drops = um.suggest_universe(stats, ["KRW-BTC", "KRW-TRX", "KRW-ETH", "KRW-GONE"], ev)
        why = dict(drops)
        self.assertIn("미달", why["KRW-TRX"])
        self.assertNotIn("KRW-BTC", why)                 # 3개월 모두 미달이어야 제외 후보
        self.assertEqual(why["KRW-ETH"], "투자유의 지정")
        self.assertIn("상장폐지", why["KRW-GONE"])


class TestFetchEvents(unittest.TestCase):
    def test_parse_and_failure(self):
        rows = [{"market": "KRW-A", "market_event": {"warning": True, "caution": {"X": False}}},
                {"market": "KRW-B", "market_event": {"warning": False, "caution": {"PRICE_FLUCTUATIONS": True}}},
                {"market": "BTC-A", "market_event": {"warning": False}}]
        with mock.patch.object(um.requests, "get") as g:
            g.return_value.json.return_value = rows
            ev = um.fetch_market_events()
        self.assertEqual(ev, {"KRW-A": {"warning": True, "caution": []},
                              "KRW-B": {"warning": False, "caution": ["PRICE_FLUCTUATIONS"]}})
        self.assertEqual(um.warned_markets(ev, ["KRW-A", "KRW-B", "KRW-C"]), {"KRW-A"})
        with mock.patch.object(um.requests, "get", side_effect=OSError("down")):
            self.assertIsNone(um.fetch_market_events())


if __name__ == "__main__":
    unittest.main()
