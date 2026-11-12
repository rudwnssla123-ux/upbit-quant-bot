"""
boost2.py — v5.1(LIVE) 대비 2차 수익 개선 후보 검증 (실거래 코드 변경 없음, 후보는 실행 전에 고정)
    PYTHONIOENCODING=utf-8 python research/boost2.py
A 순위 평활(rank_smooth)  B 진입 추세 문턱(thr)  C 거래대금 관심도(attn)  D 순위 강도 비중(rank_w)
E 변동성 기간(vol_span)    F 시장 폭 필터(breadth) G BTC 기본 보유(core)   H 공분산 축소(shrink)
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
from strategy_v5 import LIVE, UNIVERSE, MARKET, indicators, dd_multiplier, apply_buffer, drawdown  # noqa: E402

IS_END, OOS_START = "2022-12-31", "2023-01-01"
X0 = {"rank_smooth": 1, "thr": 0.0, "attn": False, "rank_w": False, "breadth": 0.0, "core": 0.0, "shrink": 0.0}


def ext_ind(close, value, p, x):
    ind = indicators(close, value, p)
    mom = ind["mom"]
    if x["attn"]:
        attn = (value.rolling(20).mean() / value.rolling(90).mean()).rank(axis=1, pct=True)
        mom = (mom * len(p.mom_ens) + attn) / (len(p.mom_ens) + 1)
    if x["rank_smooth"] > 1:
        mom = mom.rolling(x["rank_smooth"], min_periods=1).mean()
    ind["mom"] = mom
    if x["breadth"]:
        ind["breadth"] = (close > close.rolling(50).mean()).where(close.notna()).mean(axis=1)
    return ind


def weights(p, ind, i, dd, x):
    cols = ind["score"].columns
    w = pd.Series(0.0, index=cols)
    g = ind["regime"].iloc[i]
    if not g > 0:
        return w
    s, v = ind["score"].iloc[i], ind["vol"].iloc[i]
    ok = ((ind["hist"].iloc[i] >= max(p.lookbacks) + p.min_hist) & s.notna() & v.notna() & (v > 0)
          & (s > x["thr"]) & (ind["liq"].iloc[i] > p.min_liq))
    mom = ind["mom"].iloc[i][ok[ok].index].dropna().sort_values(ascending=False)
    sel = list(mom.index[:p.top_k])
    if not sel:
        return w
    raw = s[sel] / v[sel]
    if x["rank_w"]:
        raw = raw * mom[sel]
    raw = raw / raw.sum()
    cov = ind["ret"].iloc[max(0, i - p.cov_n + 1):i + 1][sel].fillna(0).cov().to_numpy() * 365
    if x["shrink"]:
        cov = (1 - x["shrink"]) * cov + x["shrink"] * np.diag(np.diag(cov))
    pv = float(np.sqrt(max(raw.to_numpy() @ cov @ raw.to_numpy(), 0)))
    scale = min(1.0, p.target_vol / pv) if pv > 0 else 0.0
    mult = g * dd_multiplier(p, dd)
    if x["breadth"]:
        mult *= min(1.0, ind["breadth"].iloc[i] / x["breadth"])
    w[sel] = (raw * scale * mult).clip(upper=p.max_w)
    if x["core"] and g >= 1 and MARKET in w.index and ok.get(MARKET, False):
        w[MARKET] = max(w[MARKET], x["core"])
        if w.sum() > 1:
            w = w / w.sum()
    return w


def run(f, p=LIVE, x=None, start=b.START, end=None, cols=None):
    x = {**X0, **(x or {})}
    close, opn, value = f["close"], f["open"], f["value"]
    if cols is not None:
        close, opn, value = close[cols], opn[cols], value[cols]
    ind = ext_ind(close, value, p, x)
    idx, names = close.index, list(close.columns)
    cl, op = close.ffill().to_numpy(), opn.to_numpy()
    i0 = max(p.warmup, idx.searchsorted(pd.Timestamp(start)))
    i1 = len(idx) if end is None else idx.searchsorted(pd.Timestamp(end), side="right")
    cash, qty = float(b.START_KRW), np.zeros(len(names))
    eqs, dates, eq_hist, turnover = [], [], [], 0.0
    for i in range(i0, i1):
        px = np.where(np.isnan(op[i]), cl[i - 1], op[i])
        px = np.where(np.isnan(px), 0.0, px)
        eq = cash + float(qty @ px)
        dd = drawdown(eq_hist, eq, p.dd_win) if eq_hist else 0.0
        tgt = weights(p, ind, i - 1, dd, x)
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
            else:
                krw = min(diff, cash)
                if krw < b.MIN_ORDER:
                    continue
                cash -= krw
                qty[j] += krw * (1 - p.fee) / (px[j] * (1 + p.slip))
                turnover += krw / eq
        eq_c = cash + float(qty @ np.nan_to_num(cl[i]))
        eq_hist.append(eq_c)
        eqs.append(eq_c)
        dates.append(idx[i])
    return {"eq": pd.Series(eqs, index=pd.DatetimeIndex(dates)), "turnover": turnover / max(len(dates) / 365, 1e-9)}


def st(eq):
    s = b.stats(eq)
    return s["cagr"], s["mdd"], s["sharpe"]


def evaluate(f, p=LIVE, x=None, cols=None):
    full = run(f, p, x, cols=cols)
    return {"all": st(full["eq"]), "is": st(run(f, p, x, end=IS_END, cols=cols)["eq"]),
            "oos": st(run(f, p, x, start=OOS_START, cols=cols)["eq"]),
            "c2": st(run(f, replace(p, fee=0.001, slip=0.002), x, start=OOS_START, cols=cols)["eq"]),
            "turn": full["turnover"], "eq": full["eq"]}


def line(name, r, base):
    gate = (r["is"][0] > base["is"][0] and r["oos"][0] > base["oos"][0] and r["c2"][0] > base["c2"][0]
            and min(r["is"][1], r["oos"][1]) > -0.25)
    print(f"  {name:<24} 전체 {r['all'][0] * 100:5.1f}%/{r['all'][1] * 100:5.1f}% Sh{r['all'][2]:4.2f} | IS {r['is'][0] * 100:5.1f}% "
          f"| OOS {r['oos'][0] * 100:5.1f}%/{r['oos'][1] * 100:5.1f}% Sh{r['oos'][2]:4.2f} | 비용2배OOS {r['c2'][0] * 100:5.1f}% "
          f"| 회전 {r['turn']:4.1f} {'✅' if gate else ''}", flush=True)
    return gate


FAMILIES = {
    "A 순위 평활": [(f"{k}일 평균 순위", LIVE, {"rank_smooth": k}) for k in (2, 3, 5)],
    "B 진입 추세 문턱": [(f"추세점수 > {k}", LIVE, {"thr": k}) for k in (0.2, 0.4, 0.6)],
    "C 거래대금 관심도": [("관심도 순위 추가", LIVE, {"attn": True})],
    "D 순위 강도 비중": [("순위 비례 비중", LIVE, {"rank_w": True})],
    "E 변동성 기간": [(f"vol_span={k}", replace(LIVE, vol_span=k), None) for k in (15, 45, 60)],
    "F 시장 폭 필터": [(f"폭 {k:.0%} 미만 축소", LIVE, {"breadth": k}) for k in (0.4, 0.5, 0.6)],
    "G BTC 기본 보유": [(f"BTC 최소 {k:.0%}", LIVE, {"core": k}) for k in (0.10, 0.15, 0.25)],
    "H 공분산 축소": [(f"축소 {k:.0%}", LIVE, {"shrink": k}) for k in (0.25, 0.5)],
}


def main():
    f = b.frames(b.load_data(universe=UNIVERSE, max_age=10**9))
    print(f"[검산] 연구 시뮬레이터 vs backtest_v5(LIVE) 차이 {(st(run(f)['eq'])[0] - st(b.run(f)['eq'])[0]) * 100:+.4f}%p")
    base = evaluate(f)
    print("\n형식: 연수익/최대낙폭, ✅ = IS·OOS·비용2배 모두 개선 & MDD -25% 이내")
    line("v5.1 기준 (실거래)", base, base)
    print("  (참고) 위험만 늘린 경우: target_vol 0.35", end="")
    line("", evaluate(f, replace(LIVE, target_vol=0.35)), base)
    winners = []
    for fam, cands in FAMILIES.items():
        print(f"\n[{fam}]")
        n = 0
        for name, p, x in cands:
            r = evaluate(f, p, x)
            ok = line(name, r, base)
            n += ok
            if ok:
                winners.append((name, p, x, r))
        print(f"  → 통과 {n}/{len(cands)}")

    if not winners:
        print("\n통과 후보 없음")
        return
    print("\n[통과 후보 강건성] 종목 무작위 절반 12회 Sharpe 개선 횟수 / 기간 분할 / 우연일 확률")
    rng = np.random.default_rng(11)
    alts = [t for t in UNIVERSE if t != MARKET]
    subsets = [[MARKET] + list(rng.choice(alts, size=len(alts) // 2, replace=False)) for _ in range(12)]
    base_sub = [st(run(f, cols=c)["eq"]) for c in subsets]
    for name, p, x, r in winners:
        sub = [st(run(f, p, x, cols=c)["eq"]) for c in subsets]
        win = sum(a[2] > bb[2] for a, bb in zip(sub, base_sub))
        diff = np.mean([a[0] - bb[0] for a, bb in zip(sub, base_sub)])
        per = []
        for s, e in [("2019-01-01", "2020-12-31"), ("2021-01-01", "2022-12-31"), ("2023-01-01", "2024-12-31"), ("2025-01-01", None)]:
            a, c = st(run(f, start=s, end=e)["eq"]), st(run(f, p, x, start=s, end=e)["eq"])
            per.append(f"{s[2:4]}~{(e or '현재')[2:4]} {(c[0] - a[0]) * 100:+.1f}")
        d = (r["eq"].pct_change() - base["eq"].pct_change()).dropna().to_numpy()
        rng2 = np.random.default_rng(5)
        means = np.array([np.concatenate([d[s_:s_ + 30] for s_ in rng2.integers(0, len(d) - 30, size=len(d) // 30)]).mean()
                          for _ in range(3000)])
        print(f"  {name:<20} 무작위 {win}/12 (평균 {diff * 100:+.1f}%p) | 구간별 %p: {', '.join(per)} | 우연 확률 {(means <= 0).mean() * 100:.1f}%")


if __name__ == "__main__":
    main()
