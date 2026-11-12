"""
pit_universe.py — 생존 편향(종목 사후 선택) 제거 백테스트: 종목 목록을 '그 당시 알 수 있던 정보'로 매달 다시 고른다
    PYTHONIOENCODING=utf-8 python research/pit_universe.py            (처음 1회는 업비트 KRW 전 종목 일봉 다운로드, 10~20분)
방법
  · 대상 후보: 지금 업비트 KRW 마켓의 전 종목 (스테이블코인, 사용자 보유 XRP 제외)
  · 매월 1일, 전날까지의 데이터만으로 '상장 365일 이상 + 최근 90일 일평균 거래대금 상위 N' 을 그 달의 대상 종목으로 정한다
    (2026년에 '살아남아 거래가 많았던 17개'를 고른 지금 방식의 사후 선택을 없앰). 그 외 전략·비용은 실거래(LIVE)와 동일
  · 그 달 대상에서 빠진 보유 종목은 목표 비중 0 → 매도
한계: 업비트 API 는 상장폐지된 종목의 시세를 주지 않는다. 그래서 '지금까지 상장 유지된 종목' 안에서의 선택 편향만 없앤다.
      상장폐지 종목까지 넣으면 결과는 이보다 조금 더 나빠질 수 있다 (봇은 투자유의 지정 시 바로 매도하므로 영향은 제한적).
"""
import os
import sys
import time
from dataclasses import replace

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
import backtest_v5 as b  # noqa: E402
from strategy_v5 import LIVE, UNIVERSE, MARKET  # noqa: E402
from upbit_market import STABLECOINS  # noqa: E402

PIT_DIR = os.path.join("data", "pit")
EXCLUDE = {"KRW-XRP"}                      # 봇이 거래하지 않는 종목(bot_v5.PROTECTED)
MIN_LISTED = 365
LIQ_DAYS = 90
IS_END, OOS_START = "2022-12-31", "2023-01-01"
ORIG_IND = b.indicators
MASK = {"m": None}


def all_krw():
    import pyupbit
    ts = pyupbit.get_tickers(fiat="KRW")
    stable = {s if s.startswith("KRW-") else f"KRW-{s}" for s in STABLECOINS}
    return sorted(t for t in ts if t not in stable and t not in EXCLUDE)


def load_all(tickers):
    os.makedirs(PIT_DIR, exist_ok=True)
    data = {}
    for k, t in enumerate(tickers):
        fp = os.path.join(PIT_DIR, f"{t}_day.csv")
        main = os.path.join(b.DATA_DIR, f"{t}_day.csv")
        for path in (fp, main):
            if os.path.exists(path):
                data[t] = pd.read_csv(path, index_col=0, parse_dates=True)
                break
        else:
            print(f"[{k + 1}/{len(tickers)}] {t} 다운로드", flush=True)
            df = None
            for wait in (0, 5, 15):                        # 요청 제한(429) 대비 재시도
                time.sleep(wait)
                df = b._download(t)
                if df is not None and len(df) >= 30:
                    break
            if df is None or len(df) < 30:
                print(f"  {t} 데이터 없음/부족 → 제외", flush=True)
                continue
            df = df.iloc[:-1]
            df.to_csv(fp)
            data[t] = df
            time.sleep(0.2)
    return data


def pit_mask(f, top_n):
    """날짜×종목 bool: 그 달의 대상 종목. 매월 1일에 전날까지의 데이터로 결정, 한 달 유지."""
    close, value = f["close"], f["value"]
    listed = close.notna().cumsum()
    first = close.apply(lambda c: c.first_valid_index())
    old = first[first <= close.index[0] + pd.Timedelta(days=3)].index    # 데이터 시작 전부터 상장돼 있던 종목
    listed[old] += MIN_LISTED                                            # (일봉이 3000개까지만 받아져 상장일을 모름)
    liq = value.rolling(LIQ_DAYS, min_periods=LIQ_DAYS).mean()
    mask = pd.DataFrame(False, index=close.index, columns=close.columns)
    months = close.index.to_period("M")
    for mth in months.unique():
        rows = np.where(months == mth)[0]
        j = rows[0] - 1                                    # 전날
        if j < 0:
            continue
        ok = (listed.iloc[j] >= MIN_LISTED) & liq.iloc[j].notna()
        rank = liq.iloc[j][ok].sort_values(ascending=False)
        pick = list(rank.index[:top_n]) if top_n else list(rank.index)
        mask.iloc[rows, mask.columns.get_indexer(pick)] = True
    mask[MARKET] = True                                    # BTC 는 시장 국면 판단용 + 항상 대상
    return mask


def pit_indicators(close, value, p):
    ind = ORIG_IND(close, value, p)
    m = MASK["m"]
    if m is not None:
        ind["liq"] = ind["liq"].where(m.reindex(index=ind["liq"].index, columns=ind["liq"].columns, fill_value=False),
                                      0.0)
    return ind


b.indicators = pit_indicators


def st(eq):
    s = b.stats(eq)
    return s["cagr"], s["mdd"], s["sharpe"], s["calmar"]


def evaluate(f, mask, cols=None, p=LIVE):
    MASK["m"] = mask
    out = []
    for kw in ({}, {"end": IS_END}, {"start": OOS_START}):
        out.append(st(b.run(f, p, cols=cols, **kw)["eq"]))
    out.append(st(b.run(f, replace(p, fee=0.001, slip=0.002), cols=cols, start=OOS_START)["eq"]))
    full = b.run(f, p, cols=cols)
    MASK["m"] = None
    return out, full


def fmt(name, out, full):
    a, i, o, c2 = out
    return (f"{name:<30}{a[0] * 100:7.1f}%/{a[1] * 100:6.1f}%{a[2]:6.2f}{i[0] * 100:8.1f}%"
            f"{o[0] * 100:8.1f}%/{o[1] * 100:6.1f}%{o[2]:6.2f}{c2[0] * 100:8.1f}%{full['gross'].mean() * 100:6.0f}%")


def main():
    tickers = all_krw()
    data = load_all(tickers)
    f = b.frames(data)
    print(f"\n대상 후보 {len(data)}종목 (현재 업비트 KRW 상장, 스테이블·XRP 제외)\n")
    print(f"{'설정':<30}{'전체 연/MDD':>16}{'Sh':>6}{'IS':>9}{'OOS 연/MDD':>16}{'Sh':>6}{'비용2배':>9}{'비중':>6}")
    base_out, base_full = evaluate(f, None, cols=UNIVERSE)
    print(fmt("지금 방식 (사후 선택 17종목)", base_out, base_full), flush=True)
    res = {}
    for n in (10, 17, 25, 40, 0):
        mask = pit_mask(f, n)
        out, full = evaluate(f, mask)
        res[n] = (out, full, mask)
        print(fmt(f"당시 거래대금 상위 {n if n else '전체'}", out, full), flush=True)

    out17, full17, mask17 = res[17]
    print("\n[당시 상위 17 기준 상세]")
    for s, e in [("2019-01-01", "2020-12-31"), ("2021-01-01", "2022-12-31"), ("2023-01-01", "2024-12-31"),
                 ("2025-01-01", None)]:
        MASK["m"] = None
        x0 = st(b.run(f, LIVE, cols=UNIVERSE, start=s, end=e)["eq"])
        MASK["m"] = mask17
        x1 = st(b.run(f, LIVE, start=s, end=e)["eq"])
        MASK["m"] = None
        print(f"  {s[:4]}~{(e or '현재')[:4]}: 사후선택 {x0[0] * 100:+6.1f}% (MDD {x0[1] * 100:5.1f})"
              f"  vs  당시선택 {x1[0] * 100:+6.1f}% (MDD {x1[1] * 100:5.1f})")
    y0, y1 = b.yearly(base_full["eq"]), b.yearly(full17["eq"])
    print("  연도별(사후 -> 당시): " + " | ".join(f"{y}: {y0[y] * 100:+.0f}->{y1.get(y, np.nan) * 100:+.0f}" for y in y0))
    m = mask17.loc["2019-01-01":]
    first = m.index[0]
    print(f"\n  {first:%Y-%m} 당시 상위 17: {', '.join(t[4:] for t in m.columns[m.loc[first]])}")
    last = m.index[-1]
    print(f"  {last:%Y-%m} 당시 상위 17: {', '.join(t[4:] for t in m.columns[m.loc[last]])}")
    held = (base_full["gross"] > 0).mean()
    print(f"  (참고) 지금 17종목 중 2019-01 당시 상위 17에 있던 종목: "
          f"{len(set(UNIVERSE) & set(m.columns[m.loc[first]]))}/{len(UNIVERSE)}, 투자 중인 날 비율 {held * 100:.0f}%")


if __name__ == "__main__":
    main()
