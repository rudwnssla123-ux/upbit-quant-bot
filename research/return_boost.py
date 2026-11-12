"""
return_boost.py — v5 연수익 개선 후보 검증 (실거래 코드 변경 없음, 후보는 실행 전에 고정)
    PYTHONIOENCODING=utf-8 python research/return_boost.py
후보
  A 순위 여유폭(exit_rank): 상위 top_k 에 들면 매수, 순위가 exit_rank 밖으로 밀려야 매도
  B 모멘텀 최근 제외(mom_skip): 순위 = (skip일 전 가격 / mom_n일 전 가격 - 1) / 변동성
  C 모멘텀 기간(mom_n)   D 보유 종목 수(top_k)   E 비중 방식(eq 균등, iv 변동성 역비례)
  F 체결 개선(슬리피지)   G 종목 확대(참고: 생존편향)
채택 기준(CLAUDE.md 규칙 4): IS·OOS 연수익 모두 개선, 비용 2배 OOS 개선, MDD -25% 이내, 같은 계열 이웃 값에서도 유지.
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
from strategy_v5 import UNIVERSE, indicators, dd_multiplier, apply_buffer, drawdown  # noqa: E402

IS_END, OOS_START = "2022-12-31", "2023-01-01"
DEFAULT_X = {"exit_rank": None, "mom_skip": 0, "wmode": "sv"}


def ext_indicators(close, value, p, x):
    ind = indicators(close, value, p)
    if x["mom_skip"]:
        ind["mom"] = (close.shift(x["mom_skip"]) / close.shift(p.mom_n) - 1) / ind["vol"]
    return ind


def weights_ext(p, ind, i, dd, held, x):
    cols = ind["score"].columns
    w = pd.Series(0.0, index=cols)
    g = ind["regime"].iloc[i]
    if not g > 0:
        return w
    s, v = ind["score"].iloc[i], ind["vol"].iloc[i]
    ok = ((ind["hist"].iloc[i] >= max(p.lookbacks) + p.min_hist) & s.notna() & v.notna() & (v > 0)
          & (s > 0) & (ind["liq"].iloc[i] > p.min_liq))
    ranked = list(ind["mom"].iloc[i][ok[ok].index].dropna().sort_values(ascending=False).index)
    if x["exit_rank"]:
        keep = [t for t in ranked[:x["exit_rank"]] if t in held][:p.top_k]
        sel = keep + [t for t in ranked if t not in keep][:p.top_k - len(keep)]
    else:
        sel = ranked[:p.top_k]
    if not sel:
        return w
    raw = {"sv": s[sel] / v[sel], "iv": 1 / v[sel], "eq": pd.Series(1.0, index=sel)}[x["wmode"]]
    raw = raw / raw.sum()
    cov = ind["ret"].iloc[max(0, i - p.cov_n + 1):i + 1][sel].fillna(0).cov().to_numpy() * 365
    pv = float(np.sqrt(max(raw.to_numpy() @ cov @ raw.to_numpy(), 0)))
    scale = min(1.0, p.target_vol / pv) if pv > 0 else 0.0
    w[sel] = (raw * scale * g * dd_multiplier(p, dd)).clip(upper=p.max_w)
    return w


def run(f, p=b.BASE, x=None, start=b.START, end=None, cols=None):
    """backtest_v5.run 과 같은 체결 모델 + 보유 종목을 아는 비중 함수."""
    x = {**DEFAULT_X, **(x or {})}
    close, opn, value = f["close"], f["open"], f["value"]
    if cols is not None:
        close, opn, value = close[cols], opn[cols], value[cols]
    ind = ext_indicators(close, value, p, x)
    idx = close.index
    cl, op = close.ffill().to_numpy(), opn.to_numpy()
    names = list(close.columns)
    i0 = max(p.warmup, idx.searchsorted(pd.Timestamp(start)))
    i1 = len(idx) if end is None else idx.searchsorted(pd.Timestamp(end), side="right")
    cash, qty = float(b.START_KRW), np.zeros(len(names))
    eqs, dates, eq_hist, turnover, trades = [], [], [], 0.0, 0
    for i in range(i0, i1):
        px = np.where(np.isnan(op[i]), cl[i - 1], op[i])
        px = np.where(np.isnan(px), 0.0, px)
        eq = cash + float(qty @ px)
        dd = drawdown(eq_hist, eq, p.dd_win) if eq_hist else 0.0
        held = {names[j] for j in range(len(names)) if qty[j] > 0}
        tgt = weights_ext(p, ind, i - 1, dd, held, x)
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
                turnover += q * px[j] / eq
                trades += 1
            else:
                krw = min(diff, cash)
                if krw < b.MIN_ORDER:
                    continue
                cash -= krw
                qty[j] += krw * (1 - p.fee) / (px[j] * (1 + p.slip))
                turnover += krw / eq
                trades += 1
        eq_c = cash + float(qty @ np.nan_to_num(cl[i]))
        eq_hist.append(eq_c)
        eqs.append(eq_c)
        dates.append(idx[i])
    yrs = max(len(dates) / 365, 1e-9)
    return {"eq": pd.Series(eqs, index=pd.DatetimeIndex(dates)), "turnover": turnover / yrs, "trades": trades / yrs}


def st(eq):
    s = b.stats(eq)
    return s["cagr"], s["mdd"], s["sharpe"]


def evaluate(f, p=b.BASE, x=None, cols=None):
    full = run(f, p, x, cols=cols)
    return {"all": st(full["eq"]), "is": st(run(f, p, x, end=IS_END, cols=cols)["eq"]),
            "oos": st(run(f, p, x, start=OOS_START, cols=cols)["eq"]),
            "c2": st(run(f, replace(p, fee=0.001, slip=0.002), x, start=OOS_START, cols=cols)["eq"]),
            "turn": full["turnover"], "trades": full["trades"]}


def line(name, r, base):
    gate = (r["is"][0] > base["is"][0] and r["oos"][0] > base["oos"][0] and r["c2"][0] > base["c2"][0]
            and min(r["is"][1], r["oos"][1]) > -0.25)
    print(f"  {name:<26} 전체 {r['all'][0] * 100:5.1f}%/{r['all'][1] * 100:5.1f}% | IS {r['is'][0] * 100:5.1f}%/{r['is'][1] * 100:5.1f}% "
          f"| OOS {r['oos'][0] * 100:5.1f}%/{r['oos'][1] * 100:5.1f}% Sh{r['oos'][2]:4.2f} | 비용2배OOS {r['c2'][0] * 100:5.1f}% "
          f"| 회전 {r['turn']:4.1f} 매매 {r['trades']:4.0f}/년 {'✅' if gate else ''}", flush=True)
    return gate


def main():
    f = b.frames(b.load_data(universe=UNIVERSE, max_age=10**9))
    sanity = st(run(f)["eq"])[0] - st(b.run(f)["eq"])[0]
    print(f"[검산] 연구용 시뮬레이터 vs backtest_v5 연수익 차이 {sanity * 100:+.4f}%p (0 이어야 정상)")
    base = evaluate(f)
    print("\n형식: 연수익/최대낙폭, ✅ = IS·OOS·비용2배 모두 개선 & MDD -25% 이내")
    line("v5 기준", base, base)
    fams = {
        "A 순위 여유폭": [(f"exit_rank={k}", b.BASE, {"exit_rank": k}) for k in (6, 7, 8, 10)],
        "B 모멘텀 최근 제외": [(f"mom_skip={k}", b.BASE, {"mom_skip": k}) for k in (3, 7, 14)],
        "C 모멘텀 기간": [(f"mom_n={k}", replace(b.BASE, mom_n=k), None) for k in (30, 45, 90, 120)],
        "D 보유 종목 수": [(f"top_k={k}", replace(b.BASE, top_k=k), None) for k in (3, 4, 6, 7)],
        "E 비중 방식": [(f"wmode={k}", b.BASE, {"wmode": k}) for k in ("eq", "iv")],
        "F 체결 개선(슬리피지)": [(f"slip={k * 100:.2f}%", replace(b.BASE, slip=k), None) for k in (0.0005, 0.0003)],
    }
    summary = {}
    for fam, cands in fams.items():
        print(f"\n[{fam}]")
        passed = 0
        for name, p, x in cands:
            r = evaluate(f, p, x)
            passed += line(name, r, base)
            summary[name] = r
        print(f"  → 통과 {passed}/{len(cands)}")

    print("\n[G 종목 확대 - 참고용: 지금 시점 거래대금 상위 코인을 고른 것이라 생존편향이 큼]")
    extra = ["KRW-ONDO", "KRW-WLD", "KRW-ENA", "KRW-SAND", "KRW-AXS"]
    try:
        f2 = b.frames(b.load_data(universe=UNIVERSE + extra, max_age=10**9))
        line("+ ONDO WLD ENA SAND AXS", evaluate(f2), base)
    except SystemExit as e:
        print("  데이터 다운로드 실패:", e)


if __name__ == "__main__":
    main()
