"""
backtest_v5.py — strategy_v5 검증 (bot_v5.py 와 같은 전략 코드 사용)
    python backtest_v5.py            전체 리포트
    python backtest_v5.py --quick    요약·IS/OOS·연도별만

체결 모델: 직전 완성 일봉으로 목표 비중을 계산 → 다음 일봉 시가(09:00 KST)에 매도 먼저, 매수 나중 체결.
  수수료·슬리피지를 매 거래에 부과하고, 5,000원 미만 주문은 건너뛴다(전량 청산 제외).
검증 항목
  [1] 전체 요약 vs BTC 단순보유   [2] IS(2019~2022) / OOS(2023~) 분할   [3] 연도별
  [4] 비용 스트레스   [5] 파라미터 민감도(이웃 값에서도 성과가 유지되는지)
  [6] 종목 구성 강건성(생존편향 점검: 2018년 상장 종목만 / 무작위 부분집합)
  [7] 워크포워드(매년 직전 2년 성과로 파라미터 재선택 → 다음 해에 적용)
  [8] 블록 부트스트랩: 1년 보유 시 수익률 분포, 목표(연 20%) 달성 확률
주의: 과거 성과는 미래 수익을 보장하지 않는다. 일봉 시뮬레이션이라 장중 급락·호가 공백은 반영되지 않는다.
"""
import os
import sys
import time
from dataclasses import replace

import numpy as np
import pandas as pd

from strategy_v5 import LIVE, UNIVERSE, MARKET, indicators, weights_at, apply_buffer, drawdown

DATA_DIR = "data"
COUNT = 3000
START = "2019-01-01"
OOS_START = "2023-01-01"
START_KRW = 10_000_000
MIN_ORDER = 5000
BASE = LIVE                     # 실거래와 같은 설정


# ───────────── 데이터 ─────────────
def _download(t):
    import pyupbit
    best = None
    for attempt in range(3):                       # 일부만 받아지는 경우가 있어 재시도
        df = pyupbit.get_ohlcv(t, interval="day", count=COUNT, period=0.2)
        time.sleep(0.5 + attempt)
        if df is not None and (best is None or len(df) > len(best)):
            best = df
        if best is not None and len(best) >= COUNT - 5:
            break
    return best


def load_data(universe=UNIVERSE, max_age=86400):
    os.makedirs(DATA_DIR, exist_ok=True)
    data = {}
    for t in universe:
        fp = os.path.join(DATA_DIR, f"{t}_day.csv")
        if os.path.exists(fp) and time.time() - os.path.getmtime(fp) < max_age:
            data[t] = pd.read_csv(fp, index_col=0, parse_dates=True)
            continue
        print(f"{t} 다운로드 중...", flush=True)
        df = _download(t)
        if df is None or len(df) < 300:
            if os.path.exists(fp):
                print(f"  ⚠ {t} 다운로드 실패 → 기존 캐시 사용")
                data[t] = pd.read_csv(fp, index_col=0, parse_dates=True)
                continue
            raise SystemExit(f"{t} 데이터 다운로드 실패")
        df = df.iloc[:-1]                          # 진행 중인 마지막 일봉 제거
        df.to_csv(fp)
        data[t] = df
    return data


def frames(data):
    idx = data[MARKET].index
    for df in data.values():
        idx = idx.union(df.index)
    f = {k: pd.DataFrame({t: df[k] for t, df in data.items()}).reindex(idx)
         for k in ("open", "close", "value")}
    return f


# ───────────── 시뮬레이터 ─────────────
def run(f, p=BASE, start=START, end=None, cols=None):
    close, opn, value = f["close"], f["open"], f["value"]
    if cols is not None:
        close, opn, value = close[cols], opn[cols], value[cols]
    ind = indicators(close, value, p)
    idx = close.index
    cl, op = close.ffill().to_numpy(), opn.to_numpy()
    names = list(close.columns)
    i0 = max(p.warmup, idx.searchsorted(pd.Timestamp(start)))
    i1 = len(idx) if end is None else idx.searchsorted(pd.Timestamp(end), side="right")
    cash, qty = float(START_KRW), np.zeros(len(names))
    eqs, dates, gross, eq_hist = [], [], [], []
    turnover = 0.0
    for i in range(i0, i1):
        px = np.where(np.isnan(op[i]), cl[i - 1], op[i])
        px = np.where(np.isnan(px), 0.0, px)
        eq = cash + float(qty @ px)
        dd = drawdown(eq_hist, eq, p.dd_win) if eq_hist else 0.0
        tgt = weights_at(p, ind, i - 1, dd)
        cur = pd.Series(np.divide(qty * px, eq, out=np.zeros(len(names)), where=eq > 0), index=names)
        want = apply_buffer(p, tgt, cur).reindex(names).to_numpy()
        for j in np.argsort(want - cur.to_numpy()):          # 매도(비중 감소)부터
            diff = (want[j] - cur.iloc[j]) * eq
            if px[j] <= 0 or abs(diff) < 1e-9:
                continue
            if diff < 0:
                full = want[j] == 0
                if not full and -diff < MIN_ORDER:
                    continue
                q = qty[j] if full else min(qty[j], -diff / px[j])
                cash += q * px[j] * (1 - p.slip) * (1 - p.fee)
                qty[j] -= q
                turnover += q * px[j] / eq
            else:
                krw = min(diff, cash)
                if krw < MIN_ORDER:
                    continue
                cash -= krw
                qty[j] += krw * (1 - p.fee) / (px[j] * (1 + p.slip))
                turnover += krw / eq
        eq_c = cash + float(qty @ np.nan_to_num(cl[i]))
        eq_hist.append(eq_c)
        eqs.append(eq_c)
        dates.append(idx[i])
        gross.append(1 - cash / eq_c if eq_c > 0 else 0.0)
    di = pd.DatetimeIndex(dates)
    yrs = max(len(di) / 365, 1e-9)
    return {"eq": pd.Series(eqs, index=di), "gross": pd.Series(gross, index=di), "turnover": turnover / yrs}


# ───────────── 지표 ─────────────
def stats(eq):
    eq = eq.dropna()
    r = eq.pct_change().dropna()
    yrs = max(len(eq) / 365, 1e-9)
    tot = eq.iloc[-1] / eq.iloc[0] - 1
    cagr = (1 + tot) ** (1 / yrs) - 1 if tot > -1 else -1.0
    mdd = (eq / eq.cummax() - 1).min()
    calmar = cagr / abs(mdd) if mdd < 0 else float("nan")
    sharpe = r.mean() / r.std() * np.sqrt(365) if r.std() > 0 else 0.0
    down = r[r < 0].std()
    sortino = r.mean() / down * np.sqrt(365) if down > 0 else 0.0
    return {"tot": tot, "cagr": cagr, "mdd": mdd, "calmar": calmar, "sharpe": sharpe, "sortino": sortino,
            "vol": r.std() * np.sqrt(365)}


def yearly(eq):
    ye = eq.groupby(eq.index.year).last()
    prev, out = eq.iloc[0], {}
    for y, v in ye.items():
        out[y] = v / prev - 1
        prev = v
    return out


HEAD = f"{'':<34}{'연환산':>8}{'최대낙폭':>9}{'Calmar':>8}{'Sharpe':>8}{'Sortino':>8}{'연변동':>8}{'평균비중':>8}{'회전율':>7}"


def line(name, res):
    s = stats(res["eq"])
    g, to = res.get("gross"), res.get("turnover")
    gm = f"{g.mean() * 100:>7.0f}%" if g is not None else f"{'-':>8}"
    tos = f"{to:>7.1f}" if to is not None else f"{'-':>7}"
    print(f"{name:<34}{s['cagr'] * 100:>7.1f}%{s['mdd'] * 100:>8.1f}%{s['calmar']:>8.2f}{s['sharpe']:>8.2f}"
          f"{s['sortino']:>8.2f}{s['vol'] * 100:>7.1f}%{gm}{tos}", flush=True)


def bh(f, t, start=START, end=None):
    c = f["close"][t].loc[start:end].dropna()
    return {"eq": c / c.iloc[0] * START_KRW, "gross": pd.Series(1.0, index=c.index), "turnover": 0.0}


def sep(title):
    print("\n" + "=" * 100 + f"\n{title}")


# ───────────── 리포트 ─────────────
def main(quick=False):
    data = load_data()
    f = frames(data)
    print("데이터 범위")
    for t, df in data.items():
        print(f"  {t:<10} {df.index[0].date()} ~ {df.index[-1].date()} ({len(df)}일)")

    base = run(f)
    sep(f"[1] 전체 요약 ({base['eq'].index[0].date()} ~ {base['eq'].index[-1].date()})\n" + HEAD)
    line("v5 (기본 설정)", base)
    line("BTC 단순보유(참고)", bh(f, MARKET))
    line("ETH 단순보유(참고)", bh(f, "KRW-ETH"))

    sep("[2] IS / OOS 분할 - 파라미터는 IS 구간 연구로 정했고, OOS 는 그 후 구간\n" + HEAD)
    line(f"IS  {START} ~ 2022-12-31", run(f, end="2022-12-31"))
    line(f"OOS {OOS_START} ~", run(f, start=OOS_START))
    line("BTC 단순보유 OOS", bh(f, MARKET, start=OOS_START))

    sep("[3] 연도별 수익률")
    yb, yv = yearly(base["eq"]), yearly(bh(f, MARKET)["eq"])
    for y in yb:
        print(f"  {y}: v5 {yb[y] * 100:+7.1f}%   BTC {yv.get(y, float('nan')) * 100:+7.1f}%")
    eq = base["eq"]
    r90 = (eq / eq.shift(90) - 1).dropna()
    print(f"  롤링 90일 수익률: 최악 {r90.min() * 100:+.1f}% | 중앙 {r90.median() * 100:+.1f}% | "
          f"손실 구간 비율 {(r90 < 0).mean() * 100:.0f}%")
    if quick:
        return

    sep("[4] 비용 스트레스\n" + HEAD)
    for name, pp in [("기준 (수수료 0.05%, 슬립 0.1%)", BASE),
                     ("슬리피지 0.3%", replace(BASE, slip=0.003)),
                     ("슬리피지 0.5%", replace(BASE, slip=0.005)),
                     ("슬립 0.5% + 수수료 0.1%", replace(BASE, slip=0.005, fee=0.001))]:
        line(name, run(f, pp))

    sep("[5] 파라미터 민감도 - 이웃 값에서도 연 20% 이상이 유지되는지 확인\n" + HEAD)
    for name, pp in [("기준", BASE),
                     ("target_vol 0.20", replace(BASE, target_vol=0.20)),
                     ("target_vol 0.25", replace(BASE, target_vol=0.25)),
                     ("target_vol 0.40", replace(BASE, target_vol=0.40)),
                     ("top_k 3", replace(BASE, top_k=3)),
                     ("top_k 7", replace(BASE, top_k=7)),
                     ("lookbacks (20,40,80)", replace(BASE, lookbacks=(20, 40, 80))),
                     ("lookbacks (5,10,20,40,80,160,240)", replace(BASE, lookbacks=(5, 10, 20, 40, 80, 160, 240))),
                     ("mom_n 30", replace(BASE, mom_n=30)),
                     ("mom_n 90", replace(BASE, mom_n=90)),
                     ("regime_sma (100,)", replace(BASE, regime_sma=(100,))),
                     ("regime_sma (50,100,200)", replace(BASE, regime_sma=(50, 100, 200))),
                     ("dd_win 90", replace(BASE, dd_win=90)),
                     ("dd_win 365", replace(BASE, dd_win=365)),
                     ("dd_scale 0.20", replace(BASE, dd_scale=0.20)),
                     ("dd_scale 0.40", replace(BASE, dd_scale=0.40)),
                     ("낙폭 연동 OFF", replace(BASE, dd_scale=0.0)),
                     ("buf 0.02", replace(BASE, buf=0.02)),
                     ("buf 0.10", replace(BASE, buf=0.10)),
                     ("max_w 0.25", replace(BASE, max_w=0.25))]:
        line(name, run(f, pp))

    sep("[6] 종목 구성 강건성 (생존편향 점검)\n" + HEAD)
    old = [t for t in UNIVERSE if data[t].index[0] <= pd.Timestamp("2018-12-31")]
    line(f"2018년 이전 상장 {len(old)}종목만", run(f, cols=old))
    rng = np.random.default_rng(42)
    alts = [t for t in UNIVERSE if t != MARKET]
    sub = []
    for _ in range(12):
        cols = [MARKET] + list(rng.choice(alts, size=len(alts) // 2, replace=False))
        sub.append(stats(run(f, cols=cols)["eq"]))
    cg = np.array([s["cagr"] for s in sub])
    md = np.array([s["mdd"] for s in sub])
    print(f"무작위 절반 구성 12회: 연환산 최소 {cg.min() * 100:.1f}% / 중앙 {np.median(cg) * 100:.1f}% / "
          f"최대 {cg.max() * 100:.1f}% | 최대낙폭 최악 {md.min() * 100:.1f}%")

    sep("[7] 워크포워드 - 매년 초, 직전 2년 Calmar 최고 설정을 골라 그 해에 적용 (선택 시점 이후 데이터 미사용)")
    grid = {f"tv{tv}_k{k}": replace(BASE, target_vol=tv, top_k=k) for tv in (0.2, 0.3, 0.4) for k in (3, 5, 7)}
    rets = {name: run(f, pp)["eq"].pct_change() for name, pp in grid.items()}
    rets = pd.DataFrame(rets).dropna(how="all")
    wf, picks = [], []
    for y in range(rets.index[0].year + 2, rets.index[-1].year + 1):
        past = rets.loc[f"{y - 2}-01-01":f"{y - 1}-12-31"]
        cal = {}
        for c in rets.columns:
            e = (1 + past[c].fillna(0)).cumprod()
            s = stats(e)
            cal[c] = s["calmar"] if np.isfinite(s["calmar"]) else -9
        best = max(cal, key=cal.get)
        picks.append(f"{y}:{best}")
        wf.append(rets.loc[str(y), best])
    wf = pd.concat(wf).fillna(0)
    wf_eq = (1 + wf).cumprod() * START_KRW
    print("  선택: " + "  ".join(picks))
    print(HEAD)
    line("워크포워드 (스티칭)", {"eq": wf_eq})
    line("같은 기간 고정 기본 설정", {"eq": base["eq"].loc[wf_eq.index[0]:]})

    sep("[8] 블록 부트스트랩 (30일 블록, 5,000회) - 1년 운용 시 수익률 분포")
    r = base["eq"].pct_change().dropna().to_numpy()
    block, n_sim, out = 30, 5000, []
    for _ in range(n_sim):
        starts = rng.integers(0, len(r) - block, size=365 // block + 1)
        path = np.concatenate([r[s:s + block] for s in starts])[:365]
        out.append(np.prod(1 + path) - 1)
    out = np.array(out)
    q = np.quantile(out, [0.05, 0.25, 0.5, 0.75, 0.95])
    print(f"  5% {q[0] * 100:+.1f}% | 25% {q[1] * 100:+.1f}% | 중앙 {q[2] * 100:+.1f}% | 75% {q[3] * 100:+.1f}% | "
          f"95% {q[4] * 100:+.1f}%")
    print(f"  1년 손실 확률 {(out < 0).mean() * 100:.0f}% | 연 20% 이상 확률 {(out >= 0.2).mean() * 100:.0f}%")
    print("\n※ 모두 과거 데이터 기반 추정치이며 미래 수익을 보장하지 않습니다.")


if __name__ == "__main__":
    main(quick="--quick" in sys.argv)
