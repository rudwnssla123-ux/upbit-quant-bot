"""
mom_ensemble.py — 순위용 모멘텀을 한 기간(60일) 대신 여러 기간의 '횡단면 순위 평균'으로 (기간 선택 과최적화 회피)
    PYTHONIOENCODING=utf-8 python research/mom_ensemble.py
"""
import os
import sys
from dataclasses import replace

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
import backtest_v5 as b  # noqa: E402
import return_boost as rb  # noqa: E402
from strategy_v5 import UNIVERSE, MARKET, indicators  # noqa: E402

_orig = rb.ext_indicators


def ens_indicators(close, value, p, x):
    ind = indicators(close, value, p)
    if x.get("mom_ens"):
        ranks = [((close / close.shift(n) - 1) / ind["vol"]).rank(axis=1, pct=True) for n in x["mom_ens"]]
        ind["mom"] = sum(ranks) / len(ranks)
    return ind


rb.ext_indicators = ens_indicators
rb.DEFAULT_X["mom_ens"] = None


def main():
    f = b.frames(b.load_data(universe=UNIVERSE, max_age=10**9))
    base = rb.evaluate(f)
    print("형식: 연수익/최대낙폭, ✅ = IS·OOS·비용2배 모두 개선 & MDD -25% 이내")
    rb.line("v5 기준 (60일)", base, base)
    sets = [(20, 30, 45, 60, 90), (30, 45, 60, 90), (20, 40, 60, 80), (30, 60, 90), (20, 30, 45, 60, 90, 120)]
    full = {"base": rb.run(f)["eq"]}
    for s in sets:
        r = rb.evaluate(f, x={"mom_ens": s})
        rb.line("앙상블 " + "/".join(map(str, s)), r, base)
        full[s] = rb.run(f, x={"mom_ens": s})["eq"]
    main_set = sets[0]
    print(f"\n[연도별] 기준 vs 앙상블 {main_set}")
    y0, y1 = b.yearly(full["base"]), b.yearly(full[main_set])
    print("  " + " | ".join(f"{y}: {y0[y] * 100:+.0f}→{y1[y] * 100:+.0f}" for y in y0))
    print("\n[기간 분할]")
    for s, e in [("2019-01-01", "2020-12-31"), ("2021-01-01", "2022-12-31"), ("2023-01-01", "2024-12-31"), ("2025-01-01", None)]:
        a = rb.st(rb.run(f, start=s, end=e)["eq"])
        c = rb.st(rb.run(f, x={"mom_ens": main_set}, start=s, end=e)["eq"])
        print(f"  {s[:4]}~{(e or '현재')[:4]}: 기준 {a[0] * 100:+6.1f}%/{a[1] * 100:5.1f}% | 앙상블 {c[0] * 100:+6.1f}%/{c[1] * 100:5.1f}%")
    print("\n[종목 무작위 절반 12회]")
    rng = np.random.default_rng(11)
    alts = [t for t in UNIVERSE if t != MARKET]
    win, diffs = 0, []
    for _ in range(12):
        cols = [MARKET] + list(rng.choice(alts, size=len(alts) // 2, replace=False))
        a, c = rb.st(rb.run(f, cols=cols)["eq"]), rb.st(rb.run(f, x={"mom_ens": main_set}, cols=cols)["eq"])
        win += c[2] > a[2]
        diffs.append(c[0] - a[0])
    print(f"  Sharpe 개선 {win}/12 | 연수익 차이 평균 {np.mean(diffs) * 100:+.1f}%p (최소 {min(diffs) * 100:+.1f}, 최대 {max(diffs) * 100:+.1f})")
    d = (full[main_set].pct_change() - full["base"].pct_change()).dropna().to_numpy()
    rng2 = np.random.default_rng(5)
    means = np.array([np.concatenate([d[s:s + 30] for s in rng2.integers(0, len(d) - 30, size=len(d) // 30)]).mean()
                      for _ in range(5000)])
    print(f"\n[우연일 확률] 연환산 차이 {d.mean() * 365 * 100:+.1f}%p | 0 이하일 확률 {(means <= 0).mean() * 100:.1f}%")
    for slip in (0.003, 0.005):
        a = rb.st(rb.run(f, replace(b.BASE, slip=slip), start="2023-01-01")["eq"])
        c = rb.st(rb.run(f, replace(b.BASE, slip=slip), x={"mom_ens": main_set}, start="2023-01-01")["eq"])
        print(f"[비용 OOS] slip {slip * 100:.1f}%: 기준 {a[0] * 100:.1f}% | 앙상블 {c[0] * 100:.1f}%")


if __name__ == "__main__":
    main()
