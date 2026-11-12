"""
monthly_check.py — bot_v5 월간 자동 점검 → 텔레그램 보고 (서버 cron 이 매월 1일 09:30 KST 에 실행)
    cd ~/bot && venv/bin/python monthly_check.py            점검 + 텔레그램 전송
    python monthly_check.py --no-send                        화면 출력만
점검 항목
  · 성과: 최근 30일 / 시작 이후 수익률, 최대낙폭, 현재 낙폭, BTC 대비
  · 중단 기준: 낙폭 -30% 초과(백테스트 최악 -20%의 1.5배), 1년 수익률 -8% 미만(부트스트랩 하위 5%)
  · 봇 상태: 서비스 실행 여부, 오늘 리밸런싱 기록, 최근 30일 오류·텔레그램 실패 횟수, 디스크
  · 종목: 업비트 투자유의/주의 지정, 상장폐지(마켓 없음)
  · 종목 목록 제안: 편입 후보(상장 1년+, 3개월 연속 일평균 거래대금 100억+, 경고 없음, 스테이블코인 제외)
                   제외 후보(3개월 연속 일평균 거래대금 50억 미만). 규칙은 upbit_market.py
  · 장부 대조(LIVE): 거래소 잔고 vs 봇 장부
  · 실전 vs 백테스트: 같은 기간을 실거래 설정(LIVE)으로 백테스트한 수익률과 실제 수익률 비교
  · 체결 비용(LIVE): 최근 30일 봇 주문(09:00~09:14)의 체결가 vs 그날 시가 (백테스트 가정 슬리피지와 비교)
전략 파라미터와 종목 목록은 절대 바꾸지 않는다. '알려주기·제안'만 한다 (반영은 사용자 승인 후 코드 수정·배포).
"""
import io
import os
import sys
import time
import contextlib
import shutil
import subprocess
import datetime as dt

import pandas as pd
import pyupbit
import requests

import backtest_v5
import bot_v5
from strategy_v5 import Params, UNIVERSE, MARKET, drawdown
from upbit_market import STABLECOINS, fetch_market_events, suggest_universe, window_means

P = Params()
DD_WARN, DD_STOP = -0.20, -0.30          # 백테스트 최대낙폭 / 그 1.5배
YEAR_STOP = -0.08                         # 1년 블록 부트스트랩 하위 5%
OK, WARN, STOP, INFO = "✅", "🟡", "🔴", "ℹ"
TRACK_GAP_WARN = -0.05                    # 실전이 같은 기간 백테스트보다 5%p 이상 나쁘면 주의
SLIP_WARN_BP = 30                         # 평균 체결 비용이 0.3% 를 넘으면 주의 (가정 0.1%)


def eq_series(st, eq_now, today):
    """(날짜 → 총자산) Series. eq_log 가 없던 시절 상태파일이면 마지막 eq_hist 하나로 대체."""
    log = st.get("eq_log") or []
    if not log and st.get("eq_hist") and st.get("last_day"):
        log = [[st["last_day"], st["eq_hist"][-1]]]
    s = pd.Series({pd.Timestamp(d): float(v) for d, v in log}, dtype=float)
    s.loc[pd.Timestamp(today)] = float(eq_now)
    return s.sort_index()


def perf(s, start_capital, today):
    today = pd.Timestamp(today)
    base30 = s[s.index <= today - pd.Timedelta(days=30)]
    ref30 = base30.iloc[-1] if len(base30) else start_capital
    days = (today - s.index[0]).days + 1
    allpts = pd.concat([pd.Series([start_capital], index=[s.index[0] - pd.Timedelta(days=1)]), s])
    base365 = s[s.index <= today - pd.Timedelta(days=365)]
    return {
        "eq": s.iloc[-1],
        "r30": s.iloc[-1] / ref30 - 1,
        "r_all": s.iloc[-1] / start_capital - 1,
        "mdd": float((allpts / allpts.cummax() - 1).min()),
        "days": days,
        "r365": (s.iloc[-1] / base365.iloc[-1] - 1) if len(base365) else None,
    }


def perf_checks(pf, dd_now):
    out = []
    if pf["mdd"] <= DD_STOP or dd_now <= DD_STOP:
        out.append((STOP, f"낙폭 {min(pf['mdd'], dd_now) * 100:.1f}% → 중단 기준(-30%) 초과. 봇 중지 후 재검토 권장"))
    elif pf["mdd"] <= DD_WARN or dd_now <= DD_WARN:
        out.append((WARN, f"낙폭 {min(pf['mdd'], dd_now) * 100:.1f}% → 백테스트 최악(-20%) 수준. 주의 관찰"))
    if pf["r365"] is not None and pf["r365"] < YEAR_STOP:
        out.append((STOP, f"최근 1년 수익률 {pf['r365'] * 100:+.1f}% → 기대 범위 하위 5% 미만. 재검토 권장"))
    return out


def bot_checks(st, today_key):
    out = []
    try:
        active = subprocess.run(["systemctl", "is-active", "bot_v5"], capture_output=True, text=True).stdout.strip()
        if active != "active":
            out.append((STOP, f"봇 서비스 상태: {active or '알 수 없음'}"))
    except FileNotFoundError:
        pass                                                   # systemd 없는 환경(로컬 테스트)
    if st.get("last_day") != today_key:
        out.append((WARN, f"오늘({today_key}) 리밸런싱 기록 없음 (마지막 {st.get('last_day')})"))
    errs = tg = 0
    since = dt.datetime.now() - dt.timedelta(days=30)
    if os.path.exists(bot_v5.LOG_FILE):
        with open(bot_v5.LOG_FILE, encoding="utf-8", errors="replace") as f:
            for ln in f:
                try:
                    t = dt.datetime.strptime(ln[:19], "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    continue
                if t < since:
                    continue
                errs += " ERROR " in ln
                tg += "텔레그램 전송 실패" in ln
    if errs:
        out.append((WARN if errs < 20 else STOP, f"최근 30일 오류 로그 {errs}건 (journalctl -u bot_v5 로 확인)"))
    if tg:
        out.append((WARN, f"텔레그램 전송 실패 {tg}건"))
    free = shutil.disk_usage(".").free / 1e9
    if free < 1:
        out.append((WARN, f"디스크 여유 {free:.1f}GB"))
    return out


def universe_checks(held, events):
    """대상·보유 종목의 투자유의/주의/상장폐지 상태. (투자유의·상장폐지 종목은 봇이 매일 자동으로 매수 제외·매도한다)"""
    if not events:
        return [(WARN, "업비트 마켓 정보 조회 실패 (종목 경고 점검 생략)")]
    out = []
    for t in sorted(set(UNIVERSE) | set(held)):
        ev = events.get(t)
        if ev is None:
            out.append((STOP, f"{t} 업비트 KRW 마켓에 없음(상장폐지) → 봇이 자동 제외 중. 목록에서 삭제 필요"))
            continue
        if ev["warning"]:
            out.append((STOP, f"{t} 투자유의 지정 → 봇이 자동 제외 중(보유분은 매도됨). 해제되지 않으면 목록에서 삭제 검토"))
        if ev["caution"]:
            out.append((INFO, f"{t} 주의 지정: {', '.join(ev['caution'])}"))
    return out


def universe_suggestions(events):
    """종목 목록 편입·제외 후보 제안 (자동 반영 없음)."""
    if not events:
        return [(WARN, "업비트 마켓 정보 조회 실패 (종목 후보 계산 생략)")]
    ticks, markets = [], list(events)
    for i in range(0, len(markets), 100):
        try:
            r = requests.get("https://api.upbit.com/v1/ticker", params={"markets": ",".join(markets[i:i + 100])},
                             timeout=10).json()
            ticks += r if isinstance(r, list) else []
        except Exception:
            pass
        time.sleep(0.2)
    busy = {t["market"] for t in ticks if t.get("acc_trade_price_24h", 0) >= 3e9}       # 1차 필터: 24시간 30억+
    cands = sorted({m for m in busy if m.split("-")[1] not in STABLECOINS} | set(UNIVERSE))
    stats = {}
    for m in cands:
        df = pyupbit.get_ohlcv(m, interval="day", count=400)
        time.sleep(0.15)
        if df is None or len(df) < 2:
            continue
        vals = df["value"].iloc[:-1].tolist()                                           # 진행 중 일봉 제외
        stats[m] = {"days": len(df), "windows": window_means(vals), "max_day": max(vals[-90:])}
    adds, drops = suggest_universe(stats, UNIVERSE, events)
    out = []
    for m, s, pump in adds:
        w = "/".join(f"{v / 1e8:,.0f}" for v in s["windows"])
        days = "400+" if s["days"] >= 400 else s["days"]
        out.append((INFO, f"📥 편입 후보 {m}: 최근 3개월 일평균 거래대금 {w}억, 상장 {days}일"
                          + (" | ⚠ 하루 거래 급증 이력(펌핑 의심)" if pump else "")))
    for m, why in drops:
        if "미달" in why:                                   # 투자유의·상장폐지는 universe_checks 에서 이미 보고
            out.append((INFO, f"📤 제외 후보 {m}: {why}"))
    if any(c[1].startswith(("📥", "📤")) for c in out):
        out.append((INFO, "목록을 바꾸려면 Claude 에게 '종목 목록 반영해줘' → 백테스트 확인 → 승인 → /deploy. "
                          "자동으로 바뀌지는 않습니다."))
    return out


def ledger_checks(st):
    if bot_v5.DRY_RUN:
        return []
    out = []
    try:
        up = pyupbit.Upbit(os.environ["UPBIT_ACCESS"], os.environ["UPBIT_SECRET"])
        krw = float(up.get_balance("KRW") or 0)
        if krw + 1 < st["cash"]:
            out.append((WARN, f"실제 KRW {krw:,.0f}원 < 봇 장부 현금 {st['cash']:,.0f}원 (출금/수동 매수 여부 확인)"))
        for t, pos in st["positions"].items():
            bal = float(up.get_balance(t.split("-")[1]) or 0)
            if bal < pos["qty"] * 0.98:
                out.append((WARN, f"{t} 거래소 잔고 {bal:.6f} < 장부 {pos['qty']:.6f}"))
    except Exception as e:
        out.append((WARN, f"계좌 조회 실패: {e}"))
    return out


def recent_frames(tickers, count=400):
    """최근 일봉(진행 중 봉 제외)으로 백테스트용 frames. BTC 가 없으면 None."""
    data = {}
    for t in sorted(set(tickers)):
        df = pyupbit.get_ohlcv(t, interval="day", count=count)
        time.sleep(0.12)
        if df is not None and len(df) > 2:
            data[t] = df.iloc[:-1]
    return backtest_v5.frames(data) if MARKET in data else None


def tracking(st, f):
    """실전 수익률 vs 같은 기간 백테스트(실거래 설정). 반환 (시작일, 실전, 백테스트) 또는 None.
    실전: eq_log 첫 기록(그날 09:00) → 마지막 기록. 백테스트: 시작일 시가 매수 → 마지막 기록 전날 종가."""
    log = st.get("eq_log") or []
    if len(log) < 2 or f is None:
        return None
    start, last = log[0][0], log[-1][0]
    eq = backtest_v5.run(f, bot_v5.PARAMS, start=start)["eq"]
    eq = eq[eq.index.normalize() < pd.Timestamp(last)]
    if len(eq) < 1:
        return None
    return start, log[-1][1] / log[0][1] - 1, eq.iloc[-1] / backtest_v5.START_KRW - 1


def tracking_checks(st, f):
    r = tracking(st, f)
    if r is None:
        return [(INFO, "실전 vs 백테스트: 기록이 아직 부족함")]
    start, live, shadow = r
    gap = live - shadow
    lv = WARN if gap <= TRACK_GAP_WARN else INFO
    return [(lv, f"📐 실전 vs 백테스트 ({start}~): 실전 {live * 100:+.1f}% | 같은 기간 백테스트 {shadow * 100:+.1f}% "
                 f"| 차이 {gap * 100:+.1f}%p" + (" → 실전이 크게 뒤처짐, 원인 점검 필요" if lv == WARN else ""))]


def is_bot_order_time(created_at):
    """봇 리밸런싱 시간대(09:00~09:14 KST) 주문만 봇 주문으로 본다. created_at 예: 2026-10-13T09:01:02+09:00"""
    return len(created_at) >= 16 and created_at[11:13] == "09" and int(created_at[14:16]) < 15


def slippage_bp(fills, opens):
    """fills: [{t, day, side, funds, avg}], opens: {(ticker, 'YYYY-MM-DD'): 시가}.
    반환 (건수, 거래액 가중 평균 bp) - 양수 = 시가보다 불리하게 체결. 없으면 None."""
    rows = []
    for x in fills:
        op = opens.get((x["t"], x["day"]))
        if op:
            rows.append((x["funds"], (x["avg"] / op - 1) * (1 if x["side"] == "bid" else -1) * 1e4))
    if not rows:
        return None
    w = sum(a for a, _ in rows)
    return len(rows), sum(a * b for a, b in rows) / w


def bot_fills(up, tickers, since):
    fills = []
    for t in sorted(set(tickers)):
        with contextlib.redirect_stdout(io.StringIO()):
            lst = (up.get_order(t, state="done") or []) + (up.get_order(t, state="cancel") or [])
        for o in lst:
            c = o.get("created_at", "")
            if c[:10] < since or not is_bot_order_time(c):
                continue
            with contextlib.redirect_stdout(io.StringIO()):
                d = up.get_individual_order(o["uuid"])
            tr = d.get("trades") or []
            vol = sum(float(x.get("volume") or 0) for x in tr)
            funds = sum(float(x.get("funds") or 0) for x in tr)
            if vol > 0:
                fills.append({"t": t, "day": c[:10], "side": d.get("side"), "funds": funds, "avg": funds / vol})
    return fills


def execution_checks(st, f, today):
    if bot_v5.DRY_RUN or f is None:
        return []
    try:
        up = pyupbit.Upbit(os.environ["UPBIT_ACCESS"], os.environ["UPBIT_SECRET"])
        since = (today - dt.timedelta(days=30)).strftime("%Y-%m-%d")
        fills = bot_fills(up, set(UNIVERSE) | set(st["positions"]), since)
        opens = {(t, d.strftime("%Y-%m-%d")): v for t in f["open"].columns for d, v in f["open"][t].dropna().items()}
        r = slippage_bp(fills, opens)
    except Exception as e:
        return [(WARN, f"체결 비용 집계 실패: {e}")]
    if r is None:
        return [(INFO, "🧾 체결 비용: 최근 30일 봇 체결 없음")]
    n, bp = r
    lv = WARN if bp > SLIP_WARN_BP else INFO
    return [(lv, f"🧾 체결 비용 (최근 30일 {n}건): 시가 대비 평균 {bp:+.1f}bp (백테스트 가정 {bot_v5.PARAMS.slip * 1e4:.0f}bp)"
                 + (" → 가정보다 큼, 지정가 주문 검토" if lv == WARN else ""))]


def calendar_notes(today):
    m = today.month
    out = []
    if m == 1:
        out.append((INFO, "연간 재검토 시기: Claude 에게 '연간 점검 해줘' (워크포워드 기준 통과 시에만 파라미터 변경)"))
    elif m in (4, 7, 10):
        out.append((INFO, "분기 점검 시기: Claude 에게 '분기 점검 해줘' (새 데이터 백테스트, 종목 목록 재검토)"))
    return out


def build_report(today=None):
    today = today or dt.datetime.now(bot_v5.KST)
    today_key = bot_v5.day_key_now(today)
    st = bot_v5.load_state()
    held = list(st["positions"])
    prices = pyupbit.get_current_price(sorted(set(held) | {MARKET})) or {}
    if isinstance(prices, (int, float)):
        prices = {MARKET: prices}
    eq = bot_v5.equity(st, prices)
    start_cap = float(st.get("start_capital") or bot_v5.BOT_CAPITAL)
    s = eq_series(st, eq, today_key)
    pf = perf(s, start_cap, today_key)
    dd_now = drawdown(st.get("eq_hist", []), eq, P.dd_win) if st.get("eq_hist") else 0.0
    btc = pyupbit.get_ohlcv(MARKET, interval="day", count=31)
    btc30 = btc["close"].iloc[-1] / btc["close"].iloc[0] - 1 if btc is not None and len(btc) > 1 else float("nan")

    events = fetch_market_events()
    f = recent_frames(set(UNIVERSE) | set(held))
    checks = perf_checks(pf, dd_now) + bot_checks(st, today_key) + universe_checks(held, events) \
        + ledger_checks(st) + tracking_checks(st, f) + execution_checks(st, f, today) \
        + calendar_notes(today) + universe_suggestions(events)
    worst = STOP if any(c[0] == STOP for c in checks) else WARN if any(c[0] == WARN for c in checks) else OK
    verdict = {OK: "정상 — 할 일 없음", WARN: "주의 항목 있음", STOP: "조치 필요"}[worst]

    hold = ", ".join(f"{t.split('-')[1]} {pos['qty'] * prices.get(t, 0) / eq * 100:.0f}%"
                     for t, pos in st["positions"].items()) or "없음(전량 현금)"
    lines = [
        f"📋 월간 점검 {today:%Y-%m-%d} | {worst} {verdict}",
        "",
        f"총자산 {pf['eq']:,.0f}원 (시작 {start_cap:,.0f}원, 운용 {pf['days']}일)",
        f"최근 30일 {pf['r30'] * 100:+.1f}% (BTC {btc30 * 100:+.1f}%) | 시작 이후 {pf['r_all'] * 100:+.1f}%",
        f"최대낙폭 {pf['mdd'] * 100:.1f}% | 현재 낙폭(180일 고점) {dd_now * 100:.1f}%",
        f"현금 {st['cash'] / eq * 100:.0f}% | 보유 {hold}",
        "",
        "기대치(백테스트): 연 30~40%, 최대낙폭 -20%, 3개월 손실 구간은 흔함(약 40%)",
    ]
    if checks:
        lines += [""] + [f"{lv} {msg}" for lv, msg in checks]
    else:
        lines += ["", f"{OK} 모든 점검 항목 이상 없음"]
    return "\n".join(lines)


def main():
    report = build_report()
    print(report)
    os.makedirs("reports", exist_ok=True)
    with open(os.path.join("reports", f"monthly_{dt.date.today():%Y-%m}.txt"), "w", encoding="utf-8") as f:
        f.write(report + "\n")
    if "--no-send" not in sys.argv:
        bot_v5.notify(report)


if __name__ == "__main__":
    main()
