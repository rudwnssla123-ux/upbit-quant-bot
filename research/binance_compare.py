"""
binance_compare.py — 같은 v5.1 전략을 바이낸스 시장 조건에서 백테스트해 업비트와 비교 (실거래 코드 변경 없음)
    PYTHONIOENCODING=utf-8 python research/binance_compare.py
U 업비트 v5.1 (17종목, 원화)
A 바이낸스 같은 17종목 (USDT, 수수료 0.1% + 슬리피지 0.03%) → 원화 환산도 표시
B 바이낸스 확대 유니버스 (현재 상장된 대형 코인 추가 - 생존편향 큼, 참고용)
C A + 하락장(BTC 50·100일선 모두 아래)에서 BTC 무기한 선물 매도 헤지 (목표 변동성 15%, 최대 50%, 펀딩 반영, 수수료 0.05%)
엔진(NautilusTrader 등)은 수익률을 바꾸지 않는다. 전략·시장 조건 차이만 비교한다.
"""
import os
import sys
import time
from dataclasses import replace

import numpy as np
import pandas as pd
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
import backtest_v5 as b  # noqa: E402
from strategy_v5 import LIVE, UNIVERSE  # noqa: E402

CACHE = os.path.join("data", "binance")
IS_END, OOS_START = "2022-12-31", "2023-01-01"
USDKRW_REF = 1350.0
EXTRA = ["BNB", "LTC", "UNI", "FIL", "ICP", "APT", "ARB", "OP", "INJ", "AAVE", "FET", "RENDER", "PEPE", "XRP"]


def klines(sym):
    os.makedirs(CACHE, exist_ok=True)
    fp = os.path.join(CACHE, f"{sym}.csv")
    if os.path.exists(fp) and time.time() - os.path.getmtime(fp) < 86400:
        return pd.read_csv(fp, index_col=0, parse_dates=True)
    out, start = [], 1500000000000
    while True:
        r = requests.get("https://api.binance.com/api/v3/klines",
                         params={"symbol": sym, "interval": "1d", "startTime": start, "limit": 1000}, timeout=20).json()
        if not isinstance(r, list) or not r:
            break
        out += r
        start = r[-1][0] + 1
        if len(r) < 1000:
            break
        time.sleep(0.25)
    if not out:
        return None
    df = pd.DataFrame([(k[0], float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[7])) for k in out],
                      columns=["t", "open", "high", "low", "close", "value"])
    df.index = pd.to_datetime(df.t, unit="ms")
    df = df.drop(columns="t").iloc[:-1]                                  # 진행 중 일봉 제거
    df.to_csv(fp)
    return df


def binance_frames(bases):
    data, miss = {}, []
    for base in bases:
        sym = f"{base}USDT"
        df = klines(sym)
        if df is None or len(df) < 300:
            miss.append(base)
            continue
        data[f"KRW-{base}"] = df                                          # 전략 코드가 쓰는 이름 형식 유지
    return data, miss


def st(eq):
    s = b.stats(eq)
    return s["cagr"], s["mdd"], s["sharpe"]


def fmt(x):
    return f"{x[0] * 100:5.1f}%/{x[1] * 100:6.1f}% Sh{x[2]:4.2f}"


def hedge_overlay(eq, btc_close, fund_daily):
    """A 의 일별 수익률에 하락장 BTC 선물 숏 수익을 더한다 (전일까지 정보로 다음날 비중 결정)."""
    r = eq.pct_change().fillna(0)
    bc = btc_close.reindex(eq.index).ffill()
    br = bc.pct_change().fillna(0)
    ma50, ma100 = bc.rolling(50).mean(), bc.rolling(100).mean()
    bear = (bc < ma50) & (bc < ma100)
    vol = br.ewm(span=30, min_periods=20).std() * np.sqrt(365)
    w = (0.15 / vol).clip(upper=0.5).where(bear, 0.0).fillna(0).shift(1).fillna(0)   # 판단은 전날 종가
    fund = fund_daily.reindex(eq.index.normalize()).fillna(0).to_numpy() * 3             # 8시간 펀딩 × 3 = 하루
    short_ret = w * (-br) + w * fund - w.diff().abs().fillna(0) * 0.0005
    out = (1 + r + short_ret).cumprod() * eq.iloc[0]
    return out, (w > 0).mean()


def main():
    up = b.frames(b.load_data(universe=UNIVERSE, max_age=10**9))
    bases = [t[4:] for t in UNIVERSE]
    dA, missA = binance_frames(bases)
    dB, missB = binance_frames(bases + EXTRA)
    print("바이낸스 데이터 없음:", missA or "없음", "| 확대 시 없음:", [m for m in missB if m not in missA] or "없음")
    fA, fB = b.frames(dA), b.frames(dB)
    pB = replace(LIVE, fee=0.001, slip=0.0003, min_liq=5e9 / USDKRW_REF)
    fx = pd.read_csv(os.path.join("data", "ext", "usdkrw.csv"), index_col=0, parse_dates=True).iloc[:, 0]

    rows = {}
    rows["U 업비트 v5.1 (원화)"] = {k: b.run(up, LIVE, **kw)["eq"] for k, kw in
                                  [("all", {}), ("is", {"end": IS_END}), ("oos", {"start": OOS_START})]}
    for name, f in [("A 바이낸스 같은 17종목", fA), ("B 바이낸스 확대 30종목", fB)]:
        rows[name + " (USDT)"] = {k: b.run(f, pB, **kw)["eq"] for k, kw in
                                  [("all", {}), ("is", {"end": IS_END}), ("oos", {"start": OOS_START})]}
    fund = pd.read_csv(os.path.join("data", "ext", "funding.csv"), index_col=0, parse_dates=True).iloc[:, 0]
    hedged, active = {}, 0
    for k, kw in [("all", {}), ("is", {"end": IS_END}), ("oos", {"start": OOS_START})]:
        hedged[k], act = hedge_overlay(rows["A 바이낸스 같은 17종목 (USDT)"][k], fA["close"]["KRW-BTC"], fund)
        active = act if k == "all" else active
    rows["C A + 하락장 BTC 선물 헤지 (USDT)"] = hedged
    for src in ["A 바이낸스 같은 17종목 (USDT)", "C A + 하락장 BTC 선물 헤지 (USDT)"]:
        conv = {}
        for k, eq in rows[src].items():
            x = fx.reindex(pd.date_range(eq.index[0].normalize() - pd.Timedelta(days=10), eq.index[-1].normalize())).ffill()
            conv[k] = eq * x.reindex(eq.index.normalize()).to_numpy()
        rows[src.replace("(USDT)", "(원화 환산)")] = conv

    print(f"\n{'버전':<34}{'전체 연수익/최대낙폭':>22}{'IS ~2022':>22}{'OOS 2023~':>22}")
    for name, r in rows.items():
        print(f"{name:<34}{fmt(st(r['all'])):>22}{fmt(st(r['is'])):>22}{fmt(st(r['oos'])):>22}")
    print(f"\n(C 헤지가 켜진 날 비율 {active * 100:.0f}%)")
    print("\n연도별 수익률")
    for name in ["U 업비트 v5.1 (원화)", "A 바이낸스 같은 17종목 (원화 환산)", "C A + 하락장 BTC 선물 헤지 (원화 환산)",
                 "B 바이낸스 확대 30종목 (USDT)"]:
        y = b.yearly(rows[name]["all"])
        print(f"  {name:<34} " + " | ".join(f"{k}: {v * 100:+.0f}%" for k, v in y.items()))
    up_bh = up["close"]["KRW-BTC"].loc["2019-03-09":].dropna()
    print(f"\n(참고) 같은 기간 BTC 단순 보유: 업비트 원화 연 {((up_bh.iloc[-1] / up_bh.iloc[0]) ** (365 / len(up_bh)) - 1) * 100:.1f}%")


if __name__ == "__main__":
    main()
