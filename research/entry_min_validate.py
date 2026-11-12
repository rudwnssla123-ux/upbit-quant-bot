"""
entry_min_validate.py — '신규 매수는 목표 비중 entry_min 이상이면 허용' 검증 (v5.1 LIVE 대비)
    PYTHONIOENCODING=utf-8 python research/entry_min_validate.py
판정: IS·OOS 연수익 개선 + 비용2배 OOS 개선 + MDD -25% 이내 + Sharpe 하락 없음(위험만 늘린 효과 배제)
"""
import os
import sys
from dataclasses import replace

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
import backtest_v5 as b  # noqa: E402
from strategy_v5 import LIVE, UNIVERSE, MARKET  # noqa: E402

IS_END, OOS_START = "2022-12-31", "2023-01-01"


def st(eq):
    s = b.stats(eq)
    return s["cagr"], s["mdd"], s["sharpe"], s["calmar"]


def main():
    f = b.frames(b.load_data(universe=UNIVERSE, max_age=10**9))
    cands = [("v5.1 (지금)", LIVE)] + [(f"entry_min {k:.0%}", replace(LIVE, entry_min=k)) for k in (0.01, 0.02, 0.03, 0.04)]
    res = {}
    print(f"{'설정':<16}{'전체':>22}{'IS':>9}{'OOS':>22}{'비용2배OOS':>11}{'평균비중':>9}{'회전':>6}")
    for name, p in cands:
        full = b.run(f, p)
        a, i, o = st(full["eq"]), st(b.run(f, p, end=IS_END)["eq"]), st(b.run(f, p, start=OOS_START)["eq"])
        c2 = st(b.run(f, replace(p, fee=0.001, slip=0.002), start=OOS_START)["eq"])
        res[name] = (a, i, o, c2, full)
        print(f"{name:<16}{a[0] * 100:6.1f}%/{a[1] * 100:6.1f}% Sh{a[2]:4.2f} Ca{a[3]:4.2f}{i[0] * 100:7.1f}%"
              f"{o[0] * 100:7.1f}%/{o[1] * 100:6.1f}% Sh{o[2]:4.2f}{c2[0] * 100:10.1f}%{full['gross'].mean() * 100:8.0f}%{full['turnover']:6.1f}", flush=True)
    base = res["v5.1 (지금)"]
    print("\n판정 (IS·OOS·비용2배 수익↑, MDD -25% 이내, 전체·OOS Sharpe 하락 없음)")
    passed = []
    for name, (a, i, o, c2, _) in res.items():
        if name == "v5.1 (지금)":
            continue
        ok = (i[0] > base[1][0] and o[0] > base[2][0] and c2[0] > base[3][0] and min(i[1], o[1]) > -0.25
              and a[2] >= base[0][2] - 0.01 and o[2] >= base[2][2] - 0.01)
        print(f"  {name:<14} {'통과' if ok else '불통과'}")
        if ok:
            passed.append(name)
    if not passed:
        return
    pick = passed[len(passed) // 2]
    p = dict(cands)[pick]
    print(f"\n[강건성: {pick}]")
    rng = np.random.default_rng(11)
    alts = [t for t in UNIVERSE if t != MARKET]
    win, diffs = 0, []
    for _ in range(12):
        cols = [MARKET] + list(rng.choice(alts, size=len(alts) // 2, replace=False))
        x0, x1 = st(b.run(f, LIVE, cols=cols)["eq"]), st(b.run(f, p, cols=cols)["eq"])
        win += x1[2] > x0[2]
        diffs.append(x1[0] - x0[0])
    print(f"  종목 무작위 절반 12회: Sharpe 개선 {win}/12, 연수익 차이 평균 {np.mean(diffs) * 100:+.1f}%p")
    for s, e in [("2019-01-01", "2020-12-31"), ("2021-01-01", "2022-12-31"), ("2023-01-01", "2024-12-31"), ("2025-01-01", None)]:
        x0, x1 = st(b.run(f, LIVE, start=s, end=e)["eq"]), st(b.run(f, p, start=s, end=e)["eq"])
        print(f"  {s[:4]}~{(e or '현재')[:4]}: {x0[0] * 100:+6.1f}% → {x1[0] * 100:+6.1f}% (MDD {x0[1] * 100:5.1f} → {x1[1] * 100:5.1f})")
    d = (res[pick][4]["eq"].pct_change() - base[4]["eq"].pct_change()).dropna().to_numpy()
    rng2 = np.random.default_rng(5)
    means = np.array([np.concatenate([d[s_:s_ + 30] for s_ in rng2.integers(0, len(d) - 30, size=len(d) // 30)]).mean()
                      for _ in range(3000)])
    print(f"  우연일 확률 {(means <= 0).mean() * 100:.1f}% (연환산 차이 {d.mean() * 365 * 100:+.1f}%p)")
    y0, y1 = b.yearly(base[4]["eq"]), b.yearly(res[pick][4]["eq"])
    print("  연도별: " + " | ".join(f"{y}: {y0[y] * 100:+.0f}→{y1[y] * 100:+.0f}" for y in y0))


if __name__ == "__main__":
    main()
