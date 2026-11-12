"""
vol_forecast.py — 목표 변동성(30%) 계산에 쓰는 포트폴리오 공분산을 '60일 단순 평균' 대신 더 반응 빠른 예측으로 (v5.1 LIVE 대비)
    PYTHONIOENCODING=utf-8 python research/vol_forecast.py
후보
  ewma{s}      : 지수가중 공분산 (span s일, 최근 날에 더 큰 무게). RiskMetrics 방식
  garch{a}     : GARCH(1,1) 다단계 예측과 같은 꼴. 종목 분산 = a·EWMA(20) + (1-a)·장기(365일) 분산, 상관은 60일
                 (단기 충격에 반응하되 장기 평균으로 되돌아감. 모수 적합 없이 고정 → 과최적화 방지)
  max{s}       : max(60일 단순, EWMA s) 분산 → 보수적 (위험이 커질 땐 빨리, 줄어들 땐 천천히)
판정: IS·OOS 연수익 개선 + 비용2배 OOS 개선 + MDD -25% 이내 + 전체·OOS Sharpe 하락 없음
     (변동성 추정이 낮아져 비중만 커진 '숨은 레버리지' 효과는 Sharpe/Calmar 와 평균 비중으로 따로 본다)
전략 코드(strategy_v5.py)는 건드리지 않는다. backtest_v5 안의 weights_at 만 이 스크립트 안에서 바꿔 끼운다.
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
import strategy_v5 as sv  # noqa: E402
from strategy_v5 import LIVE, UNIVERSE, MARKET, dd_multiplier  # noqa: E402

IS_END, OOS_START = "2022-12-31", "2023-01-01"
ORIG = b.weights_at
MODE = {"kind": "sma"}


def ewma_cov(r, span):
    """r: (n, k) 수익률 배열 (오래된 → 최근). 지수가중 공분산 (평균 0 가정, 일 단위)."""
    lam = 1 - 2 / (span + 1)
    w = lam ** np.arange(len(r))[::-1]
    w = w / w.sum()
    return (r * w[:, None]).T @ r


def port_cov(ret, i, sel, p):
    m = MODE
    if m["kind"] == "sma":
        return ret.iloc[max(0, i - p.cov_n + 1):i + 1][sel].fillna(0).cov().to_numpy() * 365
    if m["kind"] == "ewma":
        r = ret.iloc[max(0, i - 4 * m["span"] + 1):i + 1][sel].fillna(0).to_numpy()
        return ewma_cov(r, m["span"]) * 365
    base = ret.iloc[max(0, i - p.cov_n + 1):i + 1][sel].fillna(0)
    c60 = base.cov().to_numpy()
    sd60 = np.sqrt(np.clip(np.diag(c60), 1e-12, None))
    corr = c60 / np.outer(sd60, sd60)
    if m["kind"] == "garch":
        short = np.diag(ewma_cov(ret.iloc[max(0, i - 80 + 1):i + 1][sel].fillna(0).to_numpy(), 20))
        long = ret.iloc[max(0, i - 365 + 1):i + 1][sel].var().fillna(0).to_numpy()
        var = m["a"] * short + (1 - m["a"]) * np.where(long > 0, long, short)
    else:                                                  # max
        short = np.diag(ewma_cov(ret.iloc[max(0, i - 4 * m["span"] + 1):i + 1][sel].fillna(0).to_numpy(), m["span"]))
        var = np.maximum(sd60 ** 2, short)
    sd = np.sqrt(np.clip(var, 0, None))
    return corr * np.outer(sd, sd) * 365


def weights_at(p, ind, i, dd=0.0):
    """strategy_v5.weights_at 과 같고 공분산 추정만 MODE 로 바꾼다."""
    if MODE["kind"] == "sma":
        return ORIG(p, ind, i, dd)
    cols = ind["score"].columns
    w = pd.Series(0.0, index=cols)
    g = ind["regime"].iloc[i]
    if not g > 0:
        return w
    s, v = ind["score"].iloc[i], ind["vol"].iloc[i]
    ok = ((ind["hist"].iloc[i] >= max(p.lookbacks) + p.min_hist) & s.notna() & v.notna() & (v > 0)
          & (s > 0) & (ind["liq"].iloc[i] > p.min_liq))
    mom = ind["mom"].iloc[i][ok[ok].index].dropna().sort_values(ascending=False)
    sel = list(mom.index[:p.top_k])
    if not sel:
        return w
    raw = s[sel] / v[sel]
    raw = raw / raw.sum()
    cov = port_cov(ind["ret"], i, sel, p)
    pv = float(np.sqrt(max(raw.to_numpy() @ cov @ raw.to_numpy(), 0)))
    scale = min(1.0, p.target_vol / pv) if pv > 0 else 0.0
    w[sel] = (raw * scale * g * dd_multiplier(p, dd)).clip(upper=p.max_w)
    return w


b.weights_at = weights_at


def go(f, mode, **kw):
    MODE.clear()
    MODE.update(mode)
    return b.run(f, LIVE, **kw)


def st(eq):
    s = b.stats(eq)
    return s["cagr"], s["mdd"], s["sharpe"], s["calmar"]


def realized_vol(eq):
    return eq.pct_change().dropna().std() * np.sqrt(365)


def main():
    assert sv.weights_at is ORIG                           # 실거래 전략 코드는 그대로
    f = b.frames(b.load_data(universe=UNIVERSE, max_age=10**9))
    cands = [("v5.1 (지금, 60일 단순)", {"kind": "sma"})]
    cands += [(f"EWMA span {s}", {"kind": "ewma", "span": s}) for s in (10, 15, 20, 30, 45, 60)]
    cands += [(f"GARCH형 a={a}", {"kind": "garch", "a": a}) for a in (0.5, 0.7, 0.9)]
    cands += [(f"max(60일, EWMA {s})", {"kind": "max", "span": s}) for s in (10, 20, 30)]
    res = {}
    print(f"{'설정':<24}{'전체 연/MDD':>16}{'Sh':>6}{'Ca':>6}{'IS':>8}{'OOS 연/MDD':>16}{'OOS Sh':>8}{'비용2배':>8}"
          f"{'평균비중':>8}{'실현변동':>8}")
    for name, m in cands:
        full = go(f, m)
        a = st(full["eq"])
        i = st(go(f, m, end=IS_END)["eq"])
        o = st(go(f, m, start=OOS_START)["eq"])
        MODE.clear()
        MODE.update(m)
        c2 = st(b.run(f, replace(LIVE, fee=0.001, slip=0.002), start=OOS_START)["eq"])
        res[name] = (a, i, o, c2, full, m)
        print(f"{name:<24}{a[0] * 100:7.1f}%/{a[1] * 100:6.1f}%{a[2]:6.2f}{a[3]:6.2f}{i[0] * 100:7.1f}%"
              f"{o[0] * 100:8.1f}%/{o[1] * 100:6.1f}%{o[2]:8.2f}{c2[0] * 100:7.1f}%"
              f"{full['gross'].mean() * 100:7.0f}%{realized_vol(full['eq']) * 100:7.1f}%", flush=True)
    base = res["v5.1 (지금, 60일 단순)"]
    print("\n판정 (IS·OOS·비용2배 수익 개선, MDD -25% 이내, 전체·OOS Sharpe 하락 없음)")
    passed = []
    for name, (a, i, o, c2, _, _) in res.items():
        if name == "v5.1 (지금, 60일 단순)":
            continue
        ok = (i[0] > base[1][0] and o[0] > base[2][0] and c2[0] > base[3][0] and min(a[1], i[1], o[1]) > -0.25
              and a[2] >= base[0][2] - 0.01 and o[2] >= base[2][2] - 0.01)
        print(f"  {name:<22} {'통과' if ok else '불통과'}")
        if ok:
            passed.append(name)
    if not passed:
        print("\n통과 후보 없음")
        return
    for pick in passed:
        m = res[pick][5]
        print(f"\n[강건성: {pick}]")
        rng = np.random.default_rng(11)
        alts = [t for t in UNIVERSE if t != MARKET]
        win, diffs = 0, []
        for _ in range(12):
            cols = [MARKET] + list(rng.choice(alts, size=len(alts) // 2, replace=False))
            x0, x1 = st(go(f, {"kind": "sma"}, cols=cols)["eq"]), st(go(f, m, cols=cols)["eq"])
            win += x1[2] > x0[2]
            diffs.append(x1[0] - x0[0])
        print(f"  종목 무작위 절반 12회: Sharpe 개선 {win}/12, 연수익 차이 평균 {np.mean(diffs) * 100:+.1f}%p")
        for s, e in [("2019-01-01", "2020-12-31"), ("2021-01-01", "2022-12-31"), ("2023-01-01", "2024-12-31"),
                     ("2025-01-01", None)]:
            x0, x1 = st(go(f, {"kind": "sma"}, start=s, end=e)["eq"]), st(go(f, m, start=s, end=e)["eq"])
            print(f"  {s[:4]}~{(e or '현재')[:4]}: {x0[0] * 100:+6.1f}% -> {x1[0] * 100:+6.1f}% "
                  f"(MDD {x0[1] * 100:5.1f} -> {x1[1] * 100:5.1f}, Sh {x0[2]:.2f} -> {x1[2]:.2f})")
        d = (res[pick][4]["eq"].pct_change() - base[4]["eq"].pct_change()).dropna().to_numpy()
        rng2 = np.random.default_rng(5)
        means = np.array([np.concatenate([d[s_:s_ + 30] for s_ in rng2.integers(0, len(d) - 30, size=len(d) // 30)])
                          .mean() for _ in range(3000)])
        print(f"  우연일 확률 {(means <= 0).mean() * 100:.1f}% (연환산 차이 {d.mean() * 365 * 100:+.1f}%p)")
        y0, y1 = b.yearly(base[4]["eq"]), b.yearly(res[pick][4]["eq"])
        print("  연도별: " + " | ".join(f"{y}: {y0[y] * 100:+.0f}->{y1[y] * 100:+.0f}" for y in y0))


if __name__ == "__main__":
    main()
