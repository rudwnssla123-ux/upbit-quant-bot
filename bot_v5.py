"""
Upbit 퀀트 포트폴리오 봇 v5 (일봉 리밸런싱) — strategy_v5.py 와 같은 폴더에 둘 것

전략: 앙상블 추세점수 + 위험조정 모멘텀 상위 종목 + 변동성 역가중 + 목표 변동성 30% + BTC 국면 필터
      + 계좌 낙폭 연동 비중 축소 (상세는 strategy_v5.py docstring, 검증은 backtest_v5.py)
동작: 매일 09:00 KST 새 일봉이 생기면 직전 완성 일봉으로 목표 비중 계산 → 매도 먼저, 매수 나중 (시장가)
      그 외 시간에는 1분마다 상태만 점검하고 헬스체크 핑을 보낸다.
설정 (환경변수 또는 같은 폴더의 .env)
  DRY_RUN=0 이면 실거래 (미설정/그 외 = 가상 체결)
  BOT_CAPITAL                         첫 시작(장부 없음) 때 봇이 운용할 원화 (기본 1,000,000). 이후 증액/감액은 add_capital.py
  UPBIT_ACCESS, UPBIT_SECRET          실거래 시. '출금 권한 없이, 서버 IP 제한'으로 발급할 것
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID    선택
  HEALTHCHECK_URL                     선택 (봇이 멈추면 외부 서비스가 알려주도록 5분마다 핑)
봇이 직접 산 수량만 관리한다(계좌에 따로 가진 코인은 장부 밖이라 건드리지 않음).
"""
import os
import json
import time
import logging
import traceback
import datetime as dt
from zoneinfo import ZoneInfo

import pandas as pd
import pyupbit
import requests

from strategy_v5 import LIVE, UNIVERSE, MARKET, target_weights, apply_buffer, drawdown
from upbit_market import fetch_market_events, warned_markets


def load_dotenv(path=".env"):
    """의존성 없이 .env 를 읽는다. 이미 설정된 환경변수는 덮어쓰지 않는다."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for raw in f:
            s = raw.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            v = v.split(" #", 1)[0].strip().strip('"').strip("'")
            if v:
                os.environ.setdefault(k.strip(), v)


load_dotenv()

# ───────────── 설정 ─────────────
DRY_RUN = os.environ.get("DRY_RUN", "1") != "0"
VIRTUAL_START_KRW = 10_000_000          # DRY_RUN 가상 시드
BOT_CAPITAL = float(os.environ.get("BOT_CAPITAL", 1_000_000))
MODE = "DRY" if DRY_RUN else "LIVE"
PARAMS = LIVE                           # strategy_v5.LIVE (backtest_v5.py 로 검증). 바꾸면 먼저 백테스트로 재검증할 것

# 사용자가 직접 보유해 봇이 건드리면 안 되는 종목. UNIVERSE 에 들어가면 시작 단계에서 중단한다.
# 예: KRW-XRP 를 계좌에서 따로 보유 → 봇 물량과 섞이지 않도록 거래 대상에서 뺀다.
PROTECTED = {"KRW-XRP"}
assert not (PROTECTED & set(UNIVERSE)), f"보호 종목이 UNIVERSE 에 포함됨: {PROTECTED & set(UNIVERSE)}"

MIN_ORDER_KRW = 5500                    # 업비트 최소 주문 5,000원 + 여유
HISTORY_DAYS = 400                      # 지표 계산용 일봉 수 (warmup 보다 충분히 길게)
EQ_HIST_KEEP = 400
EQ_LOG_KEEP = 2000
ALERT_COOLDOWN = 1800
FILL_WAIT_SEC = 15
OHLCV_TRIES = 3                         # 일봉 조회 재시도 횟수
SKIP_AFTER_MIN = 20                     # 09:00 후 이 시간까지는 미보유 종목 일봉도 기다린다 (그 뒤엔 빼고 진행)
STALL_HOURS = 1                        # 09:00 후 이 시간이 지나도 당일 리밸런싱이 안 되면 경고
STALL_COOLDOWN = 3 * 3600

STATE_FILE = "bot_v5_state.json"
LOG_FILE = "bot_v5_log.txt"
LOOP_SEC = 60
TAG = "[DRY]" if DRY_RUN else "[LIVE]"
HEALTHCHECK_URL = os.environ.get("HEALTHCHECK_URL")
KST = ZoneInfo("Asia/Seoul")


def day_key_now(now=None):
    """업비트 일봉 기준일(09:00 KST 시작). now 는 테스트용(시간대 있는 datetime)."""
    now = (now or dt.datetime.now(KST)).astimezone(KST)
    return str((now - dt.timedelta(hours=9)).date())


# ───────────── 로그 / 알림 ─────────────
def notify(msg):
    tok, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          data={"chat_id": chat, "text": f"{TAG} {msg}"[:3500]}, timeout=10)
        if not r.ok:                                   # 잘못된 chat_id 등은 예외 없이 400 으로 온다
            desc = r.json().get("description", r.status_code) if r.headers.get("content-type", "").startswith(
                "application/json") else r.status_code
            print(f"텔레그램 전송 실패: {desc}", flush=True)
            logging.warning(f"텔레그램 전송 실패: {desc}")
    except Exception:
        logging.warning("텔레그램 전송 실패")


def log(msg, level="info", alert=False):
    print(f"[{dt.datetime.now(KST):%m-%d %H:%M:%S}] {msg}", flush=True)
    getattr(logging, level)(msg)
    if alert:
        notify(msg)


_last_alert = {}


def throttled_error(key, msg, cooldown=ALERT_COOLDOWN):
    now = time.time()
    send = now - _last_alert.get(key, 0) > cooldown
    if send:
        _last_alert[key] = now
    log(msg, "error", alert=send)


_last_ping = 0.0


def ping():
    global _last_ping
    if not HEALTHCHECK_URL or time.time() - _last_ping < 240:
        return
    _last_ping = time.time()
    try:
        requests.get(HEALTHCHECK_URL, timeout=10)
    except Exception:
        logging.warning("헬스체크 핑 실패")


# ───────────── 상태 ─────────────
def new_state():
    return {"mode": MODE, "cash": float(VIRTUAL_START_KRW if DRY_RUN else BOT_CAPITAL),
            "positions": {}, "last_day": None, "eq_hist": [], "eq_log": [],
            "start_capital": float(VIRTUAL_START_KRW if DRY_RUN else BOT_CAPITAL)}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return new_state()


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


# ───────────── 브로커 ─────────────
class Broker:
    """st['cash'] = 봇 전용 현금 장부 (DRY/LIVE 공통). LIVE 에서는 실제 체결 전후 잔고 차이로 장부를 갱신한다."""

    def __init__(self, st):
        self.st = st
        self.up = None
        if not DRY_RUN:
            self.up = pyupbit.Upbit(os.environ["UPBIT_ACCESS"], os.environ["UPBIT_SECRET"])

    def krw(self):
        cash = self.st["cash"]
        if DRY_RUN:
            return cash
        return min(cash, float(self.up.get_balance("KRW") or 0))

    def bal(self, ticker):
        return float(self.up.get_balance(ticker.split("-")[1]) or 0)

    def _wait_change(self, ticker, before, up):
        end = time.time() + FILL_WAIT_SEC
        while True:
            time.sleep(1.0)
            now = self.bal(ticker)
            changed = now > before if up else now < before
            if changed or time.time() > end:
                return now

    def _order_fill(self, res):
        """주문 응답의 uuid 로 체결 결과를 조회. 반환: (체결 수량, 체결 금액, 수수료), 확인 못 하면 None.
        시장가 매수는 남은 금액이 환불되며 state 가 cancel 로 끝날 수 있어 done/cancel 모두 완료로 본다."""
        uuid = res.get("uuid") if isinstance(res, dict) else None
        if not uuid:
            return None
        end = time.time() + FILL_WAIT_SEC
        while time.time() < end:
            time.sleep(1.0)
            try:
                o = self.up.get_individual_order(uuid)
            except Exception:
                continue
            if isinstance(o, dict) and o.get("state") in ("done", "cancel"):
                funds = sum(float(t.get("funds") or 0) for t in o.get("trades") or [])
                return float(o.get("executed_volume") or 0), funds, float(o.get("paid_fee") or 0)
        return None

    def buy(self, ticker, krw, price):
        """krw 원어치 매수. 반환: 산 수량(0 이면 실패)."""
        p = PARAMS
        if DRY_RUN:
            self.st["cash"] -= krw
            return krw * (1 - p.fee) / (price * (1 + p.slip))
        coin0, krw0 = self.bal(ticker), float(self.up.get_balance("KRW") or 0)
        res = self.up.buy_market_order(ticker, krw / (1 + p.fee))     # 수수료는 주문액 외 별도 차감
        if not res or (isinstance(res, dict) and res.get("error")):
            throttled_error(f"buy_{ticker}", f"매수 주문 실패 {ticker}: {res}")
            return 0.0
        fill = self._order_fill(res)
        if fill:
            qty, funds, fee = fill
            self.st["cash"] -= funds + fee
            return qty
        # 주문 조회 실패 시 잔고 변화로 추정
        coin1 = self._wait_change(ticker, coin0, up=True)
        spent = krw0 - float(self.up.get_balance("KRW") or 0)
        self.st["cash"] -= spent if spent > 0 else krw
        qty = max(coin1 - coin0, 0.0)
        if qty <= 0 and spent > 0:                                     # 돈은 나갔는데 코인 잔고 반영이 늦음
            qty = max(self.bal(ticker) - coin0, 0.0) or spent * (1 - p.fee) / price
            throttled_error(f"buyfill_{ticker}", f"매수 체결 확인 지연 {ticker}: 수량 {qty} 로 기록 (다음 시작 시 잔고 대조)")
        return qty

    def sell(self, ticker, qty, price):
        """qty 만큼 매도(거래소 잔고 한도). 반환: 판 수량(0 이면 실패)."""
        p = PARAMS
        if DRY_RUN:
            self.st["cash"] += qty * price * (1 - p.slip) * (1 - p.fee)
            return qty
        coin0, krw0 = self.bal(ticker), float(self.up.get_balance("KRW") or 0)
        q = min(qty, coin0)
        if q <= 0:
            return 0.0
        res = self.up.sell_market_order(ticker, q)
        if not res or (isinstance(res, dict) and res.get("error")):
            throttled_error(f"sell_{ticker}", f"매도 주문 실패 {ticker}: {res}")
            return 0.0
        fill = self._order_fill(res)
        if fill:
            sold, funds, fee = fill
            self.st["cash"] += funds - fee
            return sold
        # 주문 조회 실패 시 잔고 변화로 추정
        coin1 = self._wait_change(ticker, coin0, up=False)
        sold = max(coin0 - coin1, 0.0)
        got = float(self.up.get_balance("KRW") or 0) - krw0
        if sold <= 0 and got > 0:                                      # 대금은 들어왔는데 코인 잔고 반영이 늦음
            sold = q                                                   # (장부에 코인과 현금이 이중으로 남지 않게)
        if sold < q * 0.5:
            throttled_error(f"sellfill_{ticker}", f"매도 체결 미확인 {ticker}: 잔고 변화 {sold}/{q}")
        self.st["cash"] += got if got > 0 else sold * price * (1 - p.fee)
        return sold


def equity(st, prices):
    return st["cash"] + sum(pos["qty"] * prices.get(t, 0.0) for t, pos in st["positions"].items())


# ───────────── 시작 시 잔고 대조 ─────────────
def reconcile(st, broker):
    if DRY_RUN:
        return
    log(f"시작 점검 | 실제 KRW {float(broker.up.get_balance('KRW') or 0):,.0f}원 | "
        f"봇 장부 {st['cash']:,.0f}원 | 봇 포지션 {list(st['positions'])}")
    for t, pos in list(st["positions"].items()):
        bal = broker.bal(t)
        if bal >= pos["qty"] * 0.98:
            continue
        log(f"⚠ {t}: 거래소 잔고({bal}) < 봇 기록({pos['qty']}) → 잔고 기준으로 보정", "warning", alert=True)
        if bal <= 0:
            del st["positions"][t]
        else:
            pos["qty"] = bal


# ───────────── 데이터 ─────────────
def get_daily(t, tries=OHLCV_TRIES):
    """일봉 조회. pyupbit 는 모든 오류(429 등)를 삼키고 None 을 주므로 간격을 늘려 재시도한다."""
    for k in range(tries):
        df = pyupbit.get_ohlcv(t, interval="day", count=HISTORY_DAYS)
        time.sleep(0.15 + k)
        if df is not None and len(df) >= 2:
            return df
    return None


def minutes_into_day(now=None):
    """업비트 일봉 시작(09:00 KST) 후 지난 분."""
    now = (now or dt.datetime.now(KST)).astimezone(KST)
    return (now - dt.timedelta(hours=9)).hour * 60 + now.minute


def fetch_frames(day_key, universe=UNIVERSE, held=(), skip_ok=False):
    """완성 일봉만 담은 close/value DataFrame. 새 일봉이 아직 없으면 None (다음 루프에 재시도).
    held: 봇 보유 종목. 이 종목이나 BTC 데이터가 없으면 판단을 보류한다
    (제외하면 목표 비중 0 → 일시적 조회 실패만으로 전량 매도되기 때문).
    미보유 종목도 처음에는 기다린다(빼면 그날 순위가 달라짐). skip_ok(09:00 후 SKIP_AFTER_MIN 경과)부터는
    조회 실패·일봉 지연 종목을 빼고 진행한다 (거래정지 등으로 그 종목 때문에 하루를 통째로 놓치지 않도록)."""
    want = dt.date.fromisoformat(day_key)
    close, value = {}, {}
    for t in universe:
        df = get_daily(t)
        must = t == MARKET or t in held
        if df is None:
            if must:
                log(f"{t} 데이터 조회 실패 (보유/기준 종목), 일일 판단 보류", "warning")
                return None
            if not skip_ok:
                log(f"{t} 데이터 조회 실패 (미보유 종목), 재시도 예정", "warning")
                return None
            log(f"{t} 데이터 조회 실패 {SKIP_AFTER_MIN}분 넘게 지속 → 이번 리밸런싱에서 제외 (미보유 종목)", "warning")
            continue
        if df.index[-1].date() != want:
            if must or not skip_ok:
                log(f"{t} 새 일봉이 아직 없음, 재시도 예정")
                return None
            log(f"{t} 새 일봉이 {SKIP_AFTER_MIN}분 넘게 없음 → 이번 리밸런싱에서 제외 (미보유 종목)", "warning")
            continue
        df = df.iloc[:-1]                                  # 진행 중인 오늘 일봉 제거
        close[t], value[t] = df["close"], df["value"]
    return pd.DataFrame(close).sort_index(), pd.DataFrame(value).sort_index()


def market_screen(st):
    """업비트 투자유의·상장폐지 점검. 반환: 오늘 매수 대상에서 뺄 투자유의 종목 set.
    · 투자유의 종목은 목표 비중 0 → 보유 중이면 리밸런싱에서 전량 매도된다 (상장폐지 전 유예기간 안에 정리).
    · 봇 보유 종목이 KRW 마켓에서 사라졌으면(이미 상장폐지) 거래할 수 없으므로 장부에서 빼고 알린다.
    · 조회 실패 시 직전 성공 결과(st['warned'])를 그대로 쓴다."""
    events = fetch_market_events()
    if not events or len(events) < 50:                 # 비정상 응답이면 판단 근거로 쓰지 않음
        log("업비트 마켓 경고 정보 조회 실패 → 직전 투자유의 목록 사용", "warning")
        return set(st.get("warned", []))
    for t in list(st["positions"]):
        if t not in events:
            log(f"⚠ {t} 가 업비트 KRW 마켓에 없음(상장폐지) → 거래 불가라 봇 장부에서 제외. 업비트 앱에서 잔고 확인 필요",
                "warning", alert=True)
            del st["positions"][t]
            save_state(st)
    warned = warned_markets(events, set(UNIVERSE) | set(st["positions"]))
    warned |= {t for t in UNIVERSE if t not in events}       # 상장폐지된 대상 종목도 매수 대상에서 제외
    new = warned - set(st.get("warned", []))
    if new:
        held = sorted(new & set(st["positions"]))
        log(f"⚠ 투자유의 지정: {', '.join(sorted(new))} → 매수 대상에서 제외"
            + (f" (보유 중인 {', '.join(held)} 는 이번 리밸런싱에서 전량 매도)" if held else ""), "warning", alert=True)
    st["warned"] = sorted(warned)
    return warned


# ───────────── 리밸런싱 ─────────────
def plan_orders(p, st, prices, target):
    """반환: (eq, [(ticker, krw 차이)]) — 음수는 매도, 양수는 매수. 매도가 먼저 오도록 정렬."""
    eq = equity(st, prices)
    if eq <= 0:
        return eq, []
    cur = pd.Series({t: pos["qty"] * prices.get(t, 0.0) / eq for t, pos in st["positions"].items()}, dtype=float)
    want = apply_buffer(p, target, cur)
    orders = []
    for t in want.index:
        diff = (want[t] - cur.get(t, 0.0)) * eq
        if abs(diff) > 1e-6:
            orders.append((t, float(diff), bool(want[t] == 0)))
    orders.sort(key=lambda o: o[1])
    return eq, orders


def rebalance(st, broker, prices, target):
    p = PARAMS
    eq, orders = plan_orders(p, st, prices, target)
    for t, diff, full in orders:
        px = prices.get(t)
        if not px:
            log(f"{t} 현재가 없음 → 건너뜀", "warning")
            continue
        if diff < 0:
            pos = st["positions"][t]
            qty = pos["qty"] if full else min(pos["qty"], -diff / px)
            if qty * px < MIN_ORDER_KRW:
                if full:
                    log(f"{t} 잔량 {qty * px:,.0f}원은 최소주문 미만 → 봇 관리에서 제외(거래소에 소량 남음)", "warning")
                    del st["positions"][t]
                    save_state(st)
                continue
            sold = broker.sell(t, qty, px)
            if sold <= 0:
                continue
            pos["cost"] = pos.get("cost", 0.0) * max(0.0, 1 - sold / pos["qty"])   # 남은 수량만큼의 매입원가
            pos["qty"] -= sold
            if full or pos["qty"] * px < MIN_ORDER_KRW / 2:
                del st["positions"][t]
            log(f"매도 {t} {sold * px:,.0f}원 ({'전량' if full else '비중 축소'})", alert=True)
            save_state(st)                                     # 거래 직후 저장
        else:
            krw = min(diff, broker.krw() * 0.995)
            if krw < MIN_ORDER_KRW:
                continue
            qty = broker.buy(t, krw, px)
            if qty <= 0:
                continue
            pos = st["positions"].setdefault(t, {"qty": 0.0, "cost": 0.0})
            pos["qty"] += qty
            pos["cost"] += krw
            log(f"매수 {t} {krw:,.0f}원 @ {px:,.4f}", alert=True)
            save_state(st)


def daily(st, broker, day_key):
    p = PARAMS
    warned = market_screen(st)
    universe = [t for t in UNIVERSE if t not in warned or t == MARKET]   # BTC 는 시장 국면 판단용으로 항상 조회
    fr = fetch_frames(day_key, universe=universe, held=set(st["positions"]) - warned,
                      skip_ok=minutes_into_day() >= SKIP_AFTER_MIN)
    if fr is None:
        return False
    close, value = fr
    tickers = sorted(set(close.columns) | set(st["positions"]))
    prices = pyupbit.get_current_price(tickers)
    if isinstance(prices, (int, float)):
        prices = {tickers[0]: prices}
    if not prices or any(t not in prices for t in st["positions"]):
        log("현재가 조회 실패, 재시도 예정", "warning")
        return False
    eq = equity(st, prices)
    dd = drawdown(st["eq_hist"], eq, p.dd_win) if st["eq_hist"] else 0.0
    target = target_weights(p, close, value, dd)
    if MARKET in warned:
        target[MARKET] = 0.0
    regime = target.sum()
    picks = ", ".join(f"{t.split('-')[1]} {w * 100:.1f}%" for t, w in target[target > 0].sort_values(ascending=False).items())
    log(f"목표 비중 | 낙폭 {dd * 100:.1f}% | 총 {regime * 100:.1f}% | {picks or '전량 현금'}")
    rebalance(st, broker, prices, target)
    eq = equity(st, prices)
    st["eq_hist"] = (st["eq_hist"] + [eq])[-EQ_HIST_KEEP:]
    st["eq_log"] = (st.get("eq_log", []) + [[day_key, round(eq)]])[-EQ_LOG_KEEP:]   # 월간 점검용 (날짜, 총자산)
    held = ", ".join(f"{t.split('-')[1]} {pos['qty'] * prices[t] / eq * 100:.1f}%" for t, pos in st["positions"].items())
    log(f"일일 요약 | 총자산 {eq:,.0f}원 | 현금 {st['cash'] / eq * 100:.0f}% | 보유 {held or '없음'}", alert=True)
    return True


def check_stall(st, day_key, now=None):
    """09:00 후 STALL_HOURS 가 지나도 당일 리밸런싱이 안 됐으면 경고 (헬스체크 핑은 계속 나가므로 별도 알림 필요).
    반환: 경고 대상이면 True."""
    now = (now or dt.datetime.now(KST)).astimezone(KST)
    if st["last_day"] == day_key or (now - dt.timedelta(hours=9)).hour < STALL_HOURS:
        return False
    throttled_error("stall", f"⚠ 당일({day_key}) 리밸런싱이 09:00 후 {STALL_HOURS}시간 넘게 안 되고 있음 "
                             f"(마지막 {st['last_day']}). 'journalctl -u bot_v5 -n 50' 으로 원인 확인", STALL_COOLDOWN)
    return True


# ───────────── 메인 ─────────────
def main():
    logging.basicConfig(filename=LOG_FILE, level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(message)s")
    st = load_state()
    if st.get("mode", "DRY") != MODE:
        raise SystemExit(f"상태파일({STATE_FILE})은 {st.get('mode')} 모드용입니다. 현재 모드는 {MODE}. "
                         f"파일을 백업/삭제한 뒤 다시 시작하세요.")
    st.setdefault("start_capital", float(VIRTUAL_START_KRW if DRY_RUN else BOT_CAPITAL))
    st.setdefault("eq_log", [])
    broker = Broker(st)
    reconcile(st, broker)
    save_state(st)
    log(f"v5 시작 | 모드={MODE} | 봇현금={st['cash']:,.0f} | 목표변동성={PARAMS.target_vol:.0%} | "
        f"종목 {len(UNIVERSE)}개", alert=True)

    errors = 0
    while True:
        try:
            day_key = day_key_now()
            if st["last_day"] != day_key:
                if daily(st, broker, day_key):
                    st["last_day"] = day_key
                    save_state(st)
                else:
                    check_stall(st, day_key)
            ping()
            errors = 0
        except Exception:
            errors += 1
            log(f"오류 {errors}회 연속\n{traceback.format_exc()}", "error", alert=(errors in (3, 10, 30)))
            check_stall(st, day_key_now())
            time.sleep(min(300, 5 * 2 ** errors))
        time.sleep(LOOP_SEC)


if __name__ == "__main__":
    main()
