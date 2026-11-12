import unittest
from dataclasses import replace

import numpy as np
import pandas as pd

import strategy_v5 as sv
from strategy_v5 import Params, MARKET


def make_frames(n=400, tickers=("KRW-BTC", "KRW-ETH", "KRW-SOL", "KRW-ADA"), drift=0.003, seed=0):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2023-01-01", periods=n, freq="D")
    close = pd.DataFrame({t: 100 * np.exp(np.cumsum(rng.normal(drift, 0.03, n))) for t in tickers}, index=idx)
    value = pd.DataFrame(1e11, index=idx, columns=list(tickers))
    return close, value


class TestIndicators(unittest.TestCase):
    def test_score_bounds_and_uptrend(self):
        p = Params()
        idx = pd.date_range("2023-01-01", periods=300, freq="D")
        close = pd.DataFrame({MARKET: np.linspace(100, 300, 300), "KRW-ETH": np.linspace(300, 100, 300)}, index=idx)
        ind = sv.indicators(close, close * 0 + 1e11, p)
        self.assertEqual(ind["score"][MARKET].iloc[-1], 1.0)
        self.assertEqual(ind["score"]["KRW-ETH"].iloc[-1], 0.0)
        self.assertEqual(ind["regime"].iloc[-1], 1.0)

    def test_no_lookahead(self):
        """i 행의 목표 비중은 i 이후 데이터를 바꿔도 변하지 않아야 한다."""
        p = Params()
        close, value = make_frames()
        i = 330
        w1 = sv.weights_at(p, sv.indicators(close, value, p), i)
        c2 = close.copy()
        c2.iloc[i + 1:] *= 0.3
        w2 = sv.weights_at(p, sv.indicators(c2, value, p), i)
        pd.testing.assert_series_equal(w1, w2)


class TestWeights(unittest.TestCase):
    def test_weights_valid(self):
        p = Params()
        close, value = make_frames()
        ind = sv.indicators(close, value, p)
        for i in range(p.warmup, len(close)):
            w = sv.weights_at(p, ind, i)
            self.assertTrue((w >= 0).all())
            self.assertLessEqual(w.sum(), 1.0 + 1e-9)
            self.assertLessEqual(w.max(), p.max_w + 1e-9)
            self.assertLessEqual((w > 0).sum(), p.top_k)

    def test_bear_regime_all_cash(self):
        p = Params()
        close, value = make_frames(drift=-0.004)
        w = sv.target_weights(p, close, value)
        self.assertEqual(w.sum(), 0.0)

    def test_illiquid_and_young_excluded(self):
        p = Params()
        close, value = make_frames()
        value["KRW-SOL"] = 1e6                                 # 거래대금 부족
        close.loc[close.index[:300], "KRW-ADA"] = np.nan       # 상장 100일
        w = sv.target_weights(p, close, value)
        self.assertEqual(w["KRW-SOL"], 0.0)
        self.assertEqual(w["KRW-ADA"], 0.0)

    def test_vol_target_caps_portfolio_vol(self):
        p = replace(Params(), max_w=1.0, top_k=1)
        close, value = make_frames(tickers=("KRW-BTC",))
        ind = sv.indicators(close, value, p)
        i = len(close) - 1
        w = sv.weights_at(p, ind, i)["KRW-BTC"]
        realized = ind["ret"]["KRW-BTC"].iloc[i - p.cov_n + 1:i + 1].std() * np.sqrt(365)
        if w > 0:
            self.assertLessEqual(w * realized, p.target_vol + 1e-6)

    def test_drawdown_reduces_exposure(self):
        p = Params()
        close, value = make_frames()
        w0 = sv.target_weights(p, close, value, dd=0.0).sum()
        w1 = sv.target_weights(p, close, value, dd=-0.15).sum()
        if w0 > 0:
            self.assertAlmostEqual(w1, w0 * 0.5, places=6)


class TestHelpers(unittest.TestCase):
    def test_dd_multiplier(self):
        p = Params()
        self.assertEqual(sv.dd_multiplier(p, 0.0), 1.0)
        self.assertAlmostEqual(sv.dd_multiplier(p, -0.15), 0.5)
        self.assertEqual(sv.dd_multiplier(p, -0.5), p.dd_floor)
        self.assertEqual(sv.dd_multiplier(replace(p, dd_scale=0), -0.5), 1.0)

    def test_drawdown_window(self):
        self.assertAlmostEqual(sv.drawdown([200, 100, 100], 90, 2), -0.1)
        self.assertAlmostEqual(sv.drawdown([200, 100, 100], 90, 3), -0.55)
        self.assertEqual(sv.drawdown([100], 120, 10), 0.0)

    def test_apply_buffer(self):
        p = Params()
        cur = pd.Series({"A": 0.20, "B": 0.10, "C": 0.03})
        tgt = pd.Series({"A": 0.23, "B": 0.30, "C": 0.0, "D": 0.02})
        out = sv.apply_buffer(p, tgt, cur)
        self.assertEqual(out["A"], 0.20)        # 변화 3%p ≤ 버퍼 → 유지
        self.assertEqual(out["B"], 0.30)        # 변화 큼 → 목표로
        self.assertEqual(out["C"], 0.0)         # 목표 0 → 항상 청산
        self.assertEqual(out["D"], 0.0)         # 신규 2% ≤ 버퍼 → 진입 안 함


class TestMomentumEnsemble(unittest.TestCase):
    def test_single_period_unchanged(self):
        p = Params()
        close, value = make_frames()
        ind = sv.indicators(close, value, p)
        expect = (close / close.shift(p.mom_n) - 1) / ind["vol"]
        pd.testing.assert_frame_equal(ind["mom"], expect)

    def test_ensemble_is_rank_average(self):
        p = sv.LIVE
        close, value = make_frames()
        mom = sv.indicators(close, value, p)["mom"].dropna(how="all")
        self.assertTrue(((mom.stack() > 0) & (mom.stack() <= 1)).all())
        self.assertEqual(p.mom_ens, (20, 30, 45, 60, 90))

    def test_ensemble_no_lookahead_and_valid_weights(self):
        p = sv.LIVE
        close, value = make_frames(drift=0.004)
        i = 330
        w1 = sv.weights_at(p, sv.indicators(close, value, p), i)
        c2 = close.copy()
        c2.iloc[i + 1:] *= 0.3
        pd.testing.assert_series_equal(w1, sv.weights_at(p, sv.indicators(c2, value, p), i))
        self.assertLessEqual(w1.sum(), 1.0 + 1e-9)
        self.assertLessEqual(w1.max(), p.max_w + 1e-9)


class TestEntryMin(unittest.TestCase):
    def test_default_keeps_buffer_rule(self):
        p = Params()
        out = sv.apply_buffer(p, pd.Series({"A": 0.045}), pd.Series(dtype=float))
        self.assertEqual(out["A"], 0.0)                 # 4.5% ≤ 버퍼 5% → 진입 안 함 (기존 동작)

    def test_entry_min_allows_small_new_position(self):
        p = replace(Params(), entry_min=0.02)
        cur = pd.Series({"B": 0.10})
        out = sv.apply_buffer(p, pd.Series({"A": 0.045, "B": 0.12, "C": 0.015}), cur)
        self.assertEqual(out["A"], 0.045)               # 신규 4.5% ≥ 2% → 진입
        self.assertEqual(out["B"], 0.10)                # 기존 보유 조정은 여전히 버퍼 규칙
        self.assertEqual(out["C"], 0.0)                 # 1.5% < 2% → 진입 안 함


if __name__ == "__main__":
    unittest.main()
