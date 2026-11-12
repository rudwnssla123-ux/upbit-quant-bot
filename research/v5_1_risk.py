"""
v5_1_risk.py — v5.1(알트 비중↑)에서 리밸런싱 주기·장중 급락 손절이 도움이 되는지 검증
    PYTHONIOENCODING=utf-8 python research/v5_1_risk.py
체결 모델은 backtest_v5.run 과 동일 + (1) every 일마다만 리밸런싱 (2) 장중 손절:
  보유 코인의 그날 저가가 '전일 종가 × (1 - 문턱)' 이하이면 문턱 가격(갭이면 시가)에 전량 매도, 다음 리밸런싱까지 재매수 금지.
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
from strategy_v5 import LIVE, UNIVERSE, indicators, weights_at, apply_buffer, drawdown  # noqa: E402

IS_END, OOS_START = "2022-12-31", "2023-01-01"


def run(f, low, p=LIVE, every=1, stop_pct=None, stop_sig=None, start=b.START, end=None):
    close, opn, value = f["close"], f["open"], f["value"]
    ind = indicators(close, value, p)
    idx, names = close.index, list(close.columns)
    cl, op, lo = close.ffill().to_numpy(), opn.to_numpy(), low.reindex(index=idx, columns=names).to_numpy()
    dvol = (ind["vol"] / np.sqrt(365)).to_numpy()
    i0 = max(p.warmup, idx.searchsorted(pd.Timestamp(start)))
    i1 = len(idx) if end is None else idx.searchsorted(pd.Timestamp(end), side="right")
    cash, qty = float(b.START_KRW), np.zeros(len(names))
    eqs, dates, eq_hist, stops, blocked = [], [], [], 0, set()
    for i in range(i0, i1):
        px = np.where(np.isnan(op[i]), cl[i - 1], op[i])
        px = np.where(np.isnan(px), 0.0, px)
        eq = cash + float(qty @ px)
        if (i - i0) % every == 0:
            blocked = set()
            dd = drawdown(eq_hist, eq, p.dd_win) if eq_hist else 0.0
            tgt = weights_at(p, ind, i - 1, dd)
            cur = pd.Series(np.divide(qty * px, eq, out=np.zeros(len(names)), where=eq > 0), index=names)
            want = apply_buffer(p, tgt, cur).reindex(names).to_numpy()
            for j in np.argsort(want - cur.to_numpy()):
                diff = (want[j] - cur.iloc[j]) * eq
                if px[j] <= 0 or abs(diff) < 1e-9:
                    continue
                if diff < 0:
                    full = want[j] == 0
                    if not full and -diff < b.MIN_ORDER:
                        continue
                    q = qty[j] if full else min(qty[j], -diff / px[j])
                    cash += q * px[j] * (1 - p.slip) * (1 - p.fee)
                    qty[j] -= q
                elif names[j] not in blocked:
                    krw = min(diff, cash)
                    if krw < b.MIN_ORDER:
                        continue
                    cash -= krw
                    qty[j] += krw * (1 - p.fee) / (px[j] * (1 + p.slip))
        if stop_pct or stop_sig:                                   # 장중 손절 (전일 종가 기준)
            for j in np.nonzero(qty > 0)[0]:
                thr = stop_pct if stop_pct else stop_sig * dvol[i - 1, j]
                if not np.isfinite(thr) or np.isnan(lo[i, j]):
                    continue
                level = cl[i - 1, j] * (1 - thr)
                if lo[i, j] <= level:
                    fill = min(px[j], level)
                    cash += qty[j] * fill * (1 - p.slip) * (1 - p.fee)
                    qty[j] = 0.0
                    blocked.add(names[j])
                    stops += 1
        eq_c = cash + float(qty @ np.nan_to_num(cl[i]))
        eq_hist.append(eq_c)
        eqs.append(eq_c)
        dates.append(idx[i])
    eq = pd.Series(eqs, index=pd.DatetimeIndex(dates))
    return eq, stops / max(len(dates) / 365, 1e-9)


def st(eq):
    s = b.stats(eq)
    r = eq.pct_change().dropna()
    return s["cagr"], s["mdd"], s["sharpe"], r.min(), (r < -0.05).sum() / (len(r) / 365)


def main():
    data = b.load_data(universe=UNIVERSE, max_age=10**9)
    f = b.frames(data)
    low = pd.DataFrame({t: df["low"] for t, df in data.items()})
    print("형식: 연수익 / 최대낙폭 / Sharpe | 최악의 하루 | 하루 -5% 이상 손실 횟수/년")

    def show(name, **kw):
        a_eq, n = run(f, low, **kw)
        a, i, o = st(a_eq), st(run(f, low, end=IS_END, **kw)[0]), st(run(f, low, start=OOS_START, **kw)[0])
        print(f"  {name:<22} 전체 {a[0] * 100:5.1f}%/{a[1] * 100:5.1f}%/Sh{a[2]:4.2f} | IS {i[0] * 100:5.1f}% | OOS {o[0] * 100:5.1f}%/{o[1] * 100:5.1f}% "
              f"| 최악 {a[3] * 100:5.1f}% | -5%일 {a[4]:3.1f}회 | 손절 {n:4.1f}회/년", flush=True)

    print("\n[0] 기준: v5 (BTC 위주) vs v5.1 (앙상블)")
    show("v5 (이전)", p=replace(LIVE, mom_ens=()))
    show("v5.1 (현재)")
    print("\n[1] 리밸런싱 주기 (v5.1)")
    for k in (2, 3, 7):
        show(f"{k}일마다", every=k)
    print("\n[2] 장중 급락 손절: 전일 종가 대비 고정 %")
    for s in (0.10, 0.15, 0.20, 0.25):
        show(f"-{s * 100:.0f}% 손절", stop_pct=s)
    print("\n[3] 장중 급락 손절: 코인별 평소 하루 변동폭의 배수")
    for k in (3, 4, 5):
        show(f"평소 변동 {k}배 손절", stop_sig=k)

    print("\n[4] v5.1 실제 급락 노출: 보유 코인이 장중 전일 대비 크게 빠진 날 (2023~)")
    eq, _ = run(f, low, start=OOS_START)
    r = eq.pct_change().dropna()
    worst = r.nsmallest(5)
    print("  계좌 최악의 날: " + ", ".join(f"{d:%Y-%m-%d} {v * 100:+.1f}%" for d, v in worst.items()))
    print(f"  계좌 하루 -3% 이상 손실: {(r < -0.03).sum()}일 / {len(r)}일 | -5% 이상: {(r < -0.05).sum()}일")


if __name__ == "__main__":
    main()
