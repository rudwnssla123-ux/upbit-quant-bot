"""
overlay_validate.py — 외부 데이터 위험 오버레이 검증 (실거래 코드 변경 없음)
    PYTHONIOENCODING=utf-8 python research/overlay_validate.py            # ①②③ 각각 단독
    PYTHONIOENCODING=utf-8 python research/overlay_validate.py 12 23 13 123   # 조합 (예: 12 = ①+②)

오버레이 = v5 목표 비중 전체에 곱하는 배수(위험 신호가 켜진 날만 < 1). 매매 로직(종목 선택·비중)은 그대로.
  ① 펀딩비+미결제약정 (BTC 무기한 선물)
     펀딩: 최근 7일 평균 펀딩비 연환산 > F_TH            → × MULT
     OI  : 7일 OI 증가 > OI_TH 이면서 7일 가격 상승 < +5%  → × MULT   (둘 중 하나라도 켜지면 적용)
  ② 김치 프리미엄 (업비트 BTC / (바이낸스 BTCUSDT × 원달러) - 1)
     최근 7일 평균 > K_TH                               → × MULT
  ③ 공포·탐욕 지수 (alternative.me)
     값 ≥ G_TH                                          → × MULT
룩어헤드 방지: 업비트 일봉 d(00:00~24:00 UTC)로 다음날 09:00 KST 에 판단하므로
  펀딩: 시각 ≤ d+1 00:00 UTC 이벤트까지 / OI: 타임스탬프 d 스냅샷(하루 늦게 보수적) / 바이낸스 일봉 d /
  원달러: d-1 (발표 지연 고려) / 공포·탐욕: d 날짜 값(d 00:00 UTC 산출).
데이터가 없는 기간은 신호 없음(배수 1).
"""
import os
import sys
import time
import datetime as dt
from dataclasses import replace

import numpy as np
import pandas as pd
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
import backtest_v5 as b  # noqa: E402
import strategy_v5 as sv  # noqa: E402
from strategy_v5 import UNIVERSE  # noqa: E402

CACHE = os.path.join("data", "ext")
IS_END, OOS_START = "2022-12-31", "2023-01-01"
BASE_CFG = {1: {"f_th": 0.30, "oi_th": 0.20, "mult": 0.5},
            2: {"k_th": 0.05, "mult": 0.5},
            3: {"g_th": 80, "mult": 0.5}}
NAMES = {1: "① 펀딩비+미결제약정", 2: "② 김치 프리미엄", 3: "③ 공포·탐욕"}


def ms(s):
    return int(dt.datetime.fromisoformat(s).replace(tzinfo=dt.timezone.utc).timestamp() * 1000)


def cached(name, fetch):
    os.makedirs(CACHE, exist_ok=True)
    fp = os.path.join(CACHE, name + ".csv")
    if os.path.exists(fp) and time.time() - os.path.getmtime(fp) < 86400:
        return pd.read_csv(fp, index_col=0, parse_dates=True).iloc[:, 0]
    s = fetch()
    s.to_frame().to_csv(fp)
    return s


def fetch_funding():
    out, start = [], ms("2019-09-01")
    while True:
        r = requests.get("https://fapi.binance.com/fapi/v1/fundingRate",
                         params={"symbol": "BTCUSDT", "startTime": start, "limit": 1000}, timeout=20).json()
        if not r:
            break
        out += [(x["fundingTime"], float(x["fundingRate"])) for x in r]
        start = r[-1]["fundingTime"] + 1
        if len(r) < 1000:
            break
        time.sleep(0.3)
    df = pd.DataFrame(out, columns=["t", "f"])
    df["d"] = pd.to_datetime(df.t - 1, unit="ms").dt.normalize()                      # 00:00 이벤트는 전날 봉에 귀속
    s = df.groupby("d").f.mean()
    s.name = "funding"
    return s


def fetch_oi():
    out, end = [], ms(dt.date.today().isoformat())
    while True:
        r = requests.get("https://api.bybit.com/v5/market/open-interest",
                         params={"category": "linear", "symbol": "BTCUSDT", "intervalTime": "1d",
                                 "endTime": end, "limit": 200}, timeout=20).json()
        lst = r.get("result", {}).get("list", [])
        if not lst:
            break
        out += [(int(x["timestamp"]), float(x["openInterest"])) for x in lst]
        end = min(int(x["timestamp"]) for x in lst) - 1
        time.sleep(0.3)
    s = pd.Series({pd.Timestamp(t, unit="ms").normalize(): v for t, v in out}).sort_index()
    s.name = "oi"
    return s


def fetch_binance_btc():
    out, start = [], ms("2017-09-01")
    while True:
        r = requests.get("https://api.binance.com/api/v3/klines",
                         params={"symbol": "BTCUSDT", "interval": "1d", "startTime": start, "limit": 1000}, timeout=20).json()
        if not r:
            break
        out += [(k[0], float(k[4])) for k in r]
        start = r[-1][0] + 1
        if len(r) < 1000:
            break
        time.sleep(0.3)
    s = pd.Series({pd.Timestamp(t, unit="ms").normalize(): v for t, v in out})
    s.name = "binance_btc"
    return s


def fetch_fx():
    df = pd.read_csv("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DEXKOUS", index_col=0, parse_dates=True)
    s = pd.to_numeric(df.iloc[:, 0], errors="coerce").dropna()
    s.name = "usdkrw"
    return s


def fetch_fng():
    r = requests.get("https://api.alternative.me/fng/", params={"limit": 0, "format": "json"}, timeout=20).json()
    s = pd.Series({pd.Timestamp(int(x["timestamp"]), unit="s").normalize(): float(x["value"]) for x in r["data"]})
    s.name = "fng"
    return s.sort_index()


def signals(idx, upbit_btc):
    """idx: 백테스트 날짜(업비트 일봉 날짜). 반환: 각 원자료를 판단 가능 시점 기준으로 정렬한 DataFrame."""
    d = pd.DataFrame(index=idx)
    f = cached("funding", fetch_funding)
    d["fund7_ann"] = f.reindex(idx).rolling(7, min_periods=5).mean() * 3 * 365
    oi = cached("oi", fetch_oi).reindex(idx)
    d["oi7"] = oi / oi.shift(7) - 1
    d["px7"] = upbit_btc / upbit_btc.shift(7) - 1
    bn = cached("binance_btc", fetch_binance_btc).reindex(idx)
    fx = cached("usdkrw", fetch_fx).reindex(pd.date_range(idx[0] - pd.Timedelta(days=10), idx[-1])).ffill()
    fx = fx.shift(1).reindex(idx)                                                     # 하루 늦게 사용
    prem = upbit_btc / (bn * fx) - 1
    d["prem7"] = prem.rolling(7, min_periods=5).mean()
    d["fng"] = cached("fng", fetch_fng).reindex(idx)
    return d


def overlay(sig, k, cfg):
    m = pd.Series(1.0, index=sig.index)
    if k == 1:
        hot = (sig.fund7_ann > cfg["f_th"]) | ((sig.oi7 > cfg["oi_th"]) & (sig.px7 < 0.05))
    elif k == 2:
        hot = sig.prem7 > cfg["k_th"]
    else:
        hot = sig.fng >= cfg["g_th"]
    m[hot.fillna(False)] = cfg["mult"]
    return m


_base_weights = sv.weights_at


def run_with(f, mult, p=b.BASE, **kw):
    """backtest_v5.run 을 그대로 쓰되 목표 비중에 그날의 배수를 곱한다 (전략 코드 무수정)."""
    def patched(pp, ind, i, dd=0.0):
        w = _base_weights(pp, ind, i, dd)
        return w * mult.get(ind["score"].index[i], 1.0)
    b.weights_at = patched
    try:
        return b.run(f, p, **kw)
    finally:
        b.weights_at = _base_weights


def st(eq):
    s = b.stats(eq)
    return s["cagr"], s["mdd"], s["sharpe"], s["calmar"]


def fmt(x):
    return f"{x[0] * 100:6.1f}% {x[1] * 100:6.1f}% Sh{x[2]:4.2f}"


def three(f, mult, p=b.BASE):
    return (st(run_with(f, mult, p)["eq"]), st(run_with(f, mult, p, end=IS_END)["eq"]),
            st(run_with(f, mult, p, start=OOS_START)["eq"]))


def combo_mult(sig, one, ks, cfgs=None, how="min"):
    """여러 오버레이 결합. min = 가장 강한 신호 하나만(겹쳐도 × 0.5), prod = 켜진 신호마다 곱함(겹치면 × 0.25)."""
    cfgs = cfgs or BASE_CFG
    mult = one.copy()
    for k in ks:
        m = overlay(sig, k, cfgs[k])
        mult = np.minimum(mult, m) if how == "min" else mult * m
    return mult


def report_combo(f, sig, one, base, base_c2, cost2, ks):
    name = " + ".join(NAMES[k] for k in ks)
    print(f"\n■ {name}")
    variants = [("기본 (겹쳐도 ×0.5)", BASE_CFG, "min"), ("곱하기 (겹치면 ×0.25)", BASE_CFG, "prod")]
    if 2 in ks:
        variants.append(("② 기준 7%로 완화", {**BASE_CFG, 2: dict(k_th=0.07, mult=0.5)}, "min"))
    for lab, cfgs, how in variants:
        mult = combo_mult(sig, one, ks, cfgs, how)
        res = three(f, mult)
        c2 = st(run_with(f, mult, cost2, start=OOS_START)["eq"])
        d_is, d_oos = res[1][0] - base[1][0], res[2][0] - base[2][0]
        gate = d_is > 0 and d_oos > 0 and c2[0] > base_c2[0] and min(res[1][1], res[2][1]) > -0.25
        print(f"  {lab:<20} 신호 {(mult < 1).mean() * 100:3.0f}% | 전체 {fmt(res[0])} | IS {fmt(res[1])} | OOS {fmt(res[2])} "
              f"| OOS 비용2배 {fmt(c2)}")
        print(f"  {'':<20} 차이 IS {d_is * 100:+.1f}%p, OOS {d_oos * 100:+.1f}%p, OOS Sharpe {res[2][2] - base[2][2]:+.2f}, "
              f"OOS MDD {(res[2][1] - base[2][1]) * 100:+.1f}%p → {'통과' if gate else '불통과'}")
    y0 = b.yearly(run_with(f, one)["eq"])
    y1 = b.yearly(run_with(f, combo_mult(sig, one, ks))["eq"])
    print("  연도별 v5→기본: " + " | ".join(f"{y}: {y0[y] * 100:+.0f}→{y1[y] * 100:+.0f}" for y in y0))


def main(combo=None):
    f = b.frames(b.load_data(universe=UNIVERSE, max_age=10**9))
    idx = f["close"].index                                      # 업비트 일봉 시각(09:00 KST = 00:00 UTC)
    norm = idx.normalize()
    btc = pd.Series(f["close"]["KRW-BTC"].to_numpy(), index=norm)
    sig = signals(norm, btc)
    sig.index = idx                                             # 백테스트 날짜로 되돌림
    print("데이터 범위:", ", ".join(f"{c} {sig[c].first_valid_index().date()}~" for c in sig.columns if c != "px7"))
    one = pd.Series(1.0, index=idx)
    base = three(f, one)
    cost2 = replace(b.BASE, fee=0.001, slip=0.002)
    base_c2 = st(run_with(f, one, cost2, start=OOS_START)["eq"])
    print("\n형식: 연수익 / 최대낙폭 / Sharpe")
    print(f"{'v5 기준':<26} 전체 {fmt(base[0])} | IS {fmt(base[1])} | OOS {fmt(base[2])} | OOS 비용2배 {fmt(base_c2)}")

    if combo:                                                   # 조합 모드: 예) [(1, 2), (2, 3)]
        for ks in combo:
            report_combo(f, sig, one, base, base_c2, cost2, ks)
        return
    for ks in [(1,), (2,), (3,)]:
        mult = one.copy()
        for k in ks:
            mult = np.minimum(mult, overlay(sig, k, BASE_CFG[k]))
        name = " + ".join(NAMES[k] for k in ks)
        res = three(f, mult)
        c2 = st(run_with(f, mult, cost2, start=OOS_START)["eq"])
        active = (mult < 1)
        print(f"\n■ {name} (기본 설정 {', '.join(str(BASE_CFG[k]) for k in ks)})")
        print(f"  신호 켜진 날: 전체 {active.mean() * 100:.0f}% | IS {active.loc[:IS_END].mean() * 100:.0f}% | "
              f"OOS {active.loc[OOS_START:].mean() * 100:.0f}%")
        print(f"  {'적용':<24} 전체 {fmt(res[0])} | IS {fmt(res[1])} | OOS {fmt(res[2])} | OOS 비용2배 {fmt(c2)}")
        d_is, d_oos = res[1][0] - base[1][0], res[2][0] - base[2][0]
        gate = (d_is > 0 and d_oos > 0 and c2[0] > base_c2[0] and min(res[1][1], res[2][1]) > -0.25)
        print(f"  차이: IS {d_is * 100:+.1f}%p, OOS {d_oos * 100:+.1f}%p, OOS Sharpe {res[2][2] - base[2][2]:+.2f}, "
              f"OOS MDD {(res[2][1] - base[2][1]) * 100:+.1f}%p → 1차 기준 {'통과' if gate else '불통과'}")
        if len(ks) == 1:
            k = ks[0]
            grid = {1: [dict(f_th=t, oi_th=o, mult=m) for t in (0.2, 0.3, 0.45) for o in (0.15, 0.25) for m in (0.5, 0.25)],
                    2: [dict(k_th=t, mult=m) for t in (0.02, 0.03, 0.05, 0.07, 0.10) for m in (0.5, 0.25)],
                    3: [dict(g_th=t, mult=m) for t in (70, 75, 80, 85, 90) for m in (0.5, 0.25)]}[k]
            ok = 0
            print("  민감도 (IS 연수익/MDD | OOS 연수익/MDD, * = IS·OOS 모두 v5 보다 연수익↑ & MDD -25% 이내)")
            for cfg in grid:
                mm = overlay(sig, k, cfg)
                i = st(run_with(f, mm, end=IS_END)["eq"])
                o = st(run_with(f, mm, start=OOS_START)["eq"])
                good = i[0] > base[1][0] and o[0] > base[2][0] and min(i[1], o[1]) > -0.25
                ok += good
                print(f"    {str(cfg):<44} {i[0] * 100:5.1f}/{i[1] * 100:5.1f} | {o[0] * 100:5.1f}/{o[1] * 100:5.1f} {'*' if good else ''}")
            print(f"  → 민감도 통과 {ok}/{len(grid)}")
            if k == 1:                                                         # 구성요소 참고
                for lab, cfg in [("펀딩만", dict(f_th=0.30, oi_th=9e9, mult=0.5)), ("OI만", dict(f_th=9e9, oi_th=0.20, mult=0.5))]:
                    r = three(f, overlay(sig, 1, cfg))
                    print(f"  (참고) {lab:<6} 전체 {fmt(r[0])} | IS {fmt(r[1])} | OOS {fmt(r[2])}")
            yr = run_with(f, mult)["eq"]
            y0, y1 = b.yearly(run_with(f, one)["eq"]), b.yearly(yr)
            print("  연도별 v5→적용: " + " | ".join(f"{y}: {y0[y] * 100:+.0f}→{y1[y] * 100:+.0f}" for y in y0))


if __name__ == "__main__":
    combos = [tuple(int(c) for c in a) for a in sys.argv[1:] if a.isdigit()]   # 예) 12 23 13 123
    main(combos or None)
