"""
mom_n_robust.py — '모멘텀 기간 단축(mom_n 60 → 30~45)' 이 우연이 아닌지 강하게 검증
    PYTHONIOENCODING=utf-8 python research/mom_n_robust.py
"""
import os
import sys
from dataclasses import replace

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
import backtest_v5 as b  # noqa: E402
from strategy_v5 import UNIVERSE, MARKET  # noqa: E402

IS_END, OOS_START = "2022-12-31", "2023-01-01"


def P(n):
    return replace(b.BASE, mom_n=n)


def st(eq):
    s = b.stats(eq)
    return s["cagr"], s["mdd"], s["sharpe"], s["calmar"]


def main():
    f = b.frames(b.load_data(universe=UNIVERSE, max_age=10**9))

    print("[1] 모멘텀 기간 촘촘히 (IS 연수익/MDD | OOS 연수익/MDD/Sharpe | 전체 Calmar)")
    full = {}
    for n in range(20, 95, 5):
        i, o = st(b.run(f, P(n), end=IS_END)["eq"]), st(b.run(f, P(n), start=OOS_START)["eq"])
        full[n] = b.run(f, P(n))["eq"]
        a = st(full[n])
        tag = " ← 현재" if n == 60 else ""
        print(f"  mom_n={n:>3}: IS {i[0] * 100:5.1f}/{i[1] * 100:5.1f} | OOS {o[0] * 100:5.1f}/{o[1] * 100:5.1f}/Sh{o[2]:4.2f} | Calmar {a[3]:4.2f}{tag}", flush=True)

    print("\n[2] 기간 분할 (각 구간 독립 실행): mom_n 30 / 45 / 60")
    for s, e in [("2019-01-01", "2020-12-31"), ("2021-01-01", "2022-12-31"), ("2023-01-01", "2024-12-31"), ("2025-01-01", None)]:
        r = {n: st(b.run(f, P(n), start=s, end=e)["eq"]) for n in (30, 45, 60)}
        print(f"  {s[:4]}~{(e or '현재')[:4]}: " + " | ".join(f"{n}일 {r[n][0] * 100:+6.1f}%/{r[n][1] * 100:5.1f}%" for n in (30, 45, 60)), flush=True)

    print("\n[3] 연도별 수익률")
    ys = {n: b.yearly(full[n]) for n in (30, 45, 60)}
    for y in ys[60]:
        print(f"  {y}: 30일 {ys[30][y] * 100:+5.0f}% | 45일 {ys[45][y] * 100:+5.0f}% | 60일 {ys[60][y] * 100:+5.0f}%")

    print("\n[4] 워크포워드: 매년 초 직전 2년 Calmar 최고 기간 선택 (후보 30/45/60/90/120)")
    rets = pd.DataFrame({n: full[n].pct_change() for n in (30, 45, 60, 90)} |
                        {120: b.run(f, P(120))["eq"].pct_change()}).dropna(how="all")
    wf, picks = [], []
    for y in range(rets.index[0].year + 2, rets.index[-1].year + 1):
        past = rets.loc[f"{y - 2}-01-01":f"{y - 1}-12-31"]
        cal = {c: st((1 + past[c].fillna(0)).cumprod())[3] for c in rets.columns}
        best = max(cal, key=lambda c: cal[c] if np.isfinite(cal[c]) else -9)
        picks.append(f"{y}:{best}")
        wf.append(rets.loc[str(y), best])
    wf = (1 + pd.concat(wf).fillna(0)).cumprod()
    fixed = (1 + rets[60].loc[wf.index[0]:].fillna(0)).cumprod()
    print("  선택: " + "  ".join(picks))
    a, c = st(wf), st(fixed)
    print(f"  워크포워드 {a[0] * 100:.1f}%/{a[1] * 100:.1f}% Sh{a[2]:.2f} | 같은 기간 60일 고정 {c[0] * 100:.1f}%/{c[1] * 100:.1f}% Sh{c[2]:.2f}")

    print("\n[5] 종목 구성 무작위 절반 12회: 45일 vs 60일")
    rng = np.random.default_rng(11)
    alts = [t for t in UNIVERSE if t != MARKET]
    win, diffs = 0, []
    for _ in range(12):
        cols = [MARKET] + list(rng.choice(alts, size=len(alts) // 2, replace=False))
        r45, r60 = st(b.run(f, P(45), cols=cols)["eq"]), st(b.run(f, P(60), cols=cols)["eq"])
        win += r45[2] > r60[2]
        diffs.append(r45[0] - r60[0])
    print(f"  Sharpe 개선 {win}/12 | 연수익 차이 평균 {np.mean(diffs) * 100:+.1f}%p (최소 {min(diffs) * 100:+.1f}, 최대 {max(diffs) * 100:+.1f})")

    print("\n[6] 차이가 우연일 확률 (하루 수익률 차이를 30일 블록으로 재표본 5,000회)")
    for n in (30, 45):
        d = (full[n].pct_change() - full[60].pct_change()).dropna().to_numpy()
        rng2 = np.random.default_rng(5)
        means = []
        for _ in range(5000):
            st_ = rng2.integers(0, len(d) - 30, size=len(d) // 30)
            means.append(np.concatenate([d[s:s + 30] for s in st_]).mean())
        means = np.array(means)
        print(f"  {n}일 - 60일: 연환산 차이 {d.mean() * 365 * 100:+.1f}%p | 차이가 0 이하일 확률 {(means <= 0).mean() * 100:.1f}% "
              f"| 하루 수익률 상관 {np.corrcoef(full[n].pct_change().dropna(), full[60].pct_change().dropna())[0, 1]:.2f}")

    print("\n[7] 비용 스트레스 OOS (슬리피지 0.3% / 0.5%)")
    for slip in (0.003, 0.005):
        r = {n: st(b.run(f, replace(P(n), slip=slip), start=OOS_START)["eq"]) for n in (30, 45, 60)}
        print(f"  slip {slip * 100:.1f}%: " + " | ".join(f"{n}일 {r[n][0] * 100:5.1f}%" for n in (30, 45, 60)))


if __name__ == "__main__":
    main()
