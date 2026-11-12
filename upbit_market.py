"""
upbit_market.py — 업비트 마켓 정보(투자유의·주의·상장 여부)와 대상 종목(UNIVERSE) 편입/제외 후보 규칙

bot_v5.py   : 매일 리밸런싱 때 투자유의 종목을 매수 대상에서 빼고, 보유 중이면 전량 매도한다.
monthly_check.py : 매월 편입·제외 후보를 텔레그램으로 제안한다 (자동 반영하지 않음. 사용자 승인 후 코드 수정·배포).
"""
import requests

MARKET_ALL_URL = "https://api.upbit.com/v1/market/all"

# 가격이 원화/달러에 고정된 코인: 추세 전략 대상이 아님
STABLECOINS = {"USDT", "USDC", "USDE", "USDS", "DAI", "FDUSD", "TUSD", "PYUSD", "USD1", "RLUSD"}

# 편입 후보: 상장 1년 이상, 최근 3개월(30일×3 구간) 모두 하루 평균 거래대금 100억 이상, 투자유의·주의 없음
ADD_MIN_DAYS = 365
ADD_MIN_VALUE = 10e9
# 제외 후보: 최근 3개월 모두 하루 평균 거래대금 50억 미만 (전략의 min_liq 와 같은 기준이 3개월 지속)
DROP_MAX_VALUE = 5e9
# 펌핑 의심: 최근 90일 중 하루 거래대금 최대값이 90일 평균의 이 배수 이상
PUMP_RATIO = 10.0


def fetch_market_events(timeout=10):
    """반환: {market: {"warning": bool, "caution": [사유...]}} (KRW 마켓만). 조회 실패 시 None."""
    try:
        res = requests.get(MARKET_ALL_URL, params={"is_details": "true"}, timeout=timeout)
        rows = res.json()
        if not isinstance(rows, list):
            return None
    except Exception:
        return None
    out = {}
    for m in rows:
        if not str(m.get("market", "")).startswith("KRW-"):
            continue
        ev = m.get("market_event") or {}
        out[m["market"]] = {"warning": bool(ev.get("warning")),
                            "caution": sorted(k for k, v in (ev.get("caution") or {}).items() if v)}
    return out


def warned_markets(events, tickers):
    """tickers 중 투자유의 지정 종목."""
    return {t for t in tickers if events.get(t, {}).get("warning")}


def window_means(values, n=3, size=30):
    """완성 일봉 거래대금 리스트(오래된 → 최근)에서 최근 size 일 구간 n 개의 평균 (최근 구간이 먼저)."""
    out = []
    for k in range(n):
        seg = values[len(values) - (k + 1) * size: len(values) - k * size]
        if len(seg) < size:
            break
        out.append(sum(seg) / size)
    return out


def suggest_universe(stats, universe, events):
    """
    stats: {market: {"days": 상장 일수(최대 조회수), "windows": [최근, 그 전, 그 전] 30일 평균 거래대금,
                     "max_day": 최근 90일 하루 최대 거래대금}}
    반환: (편입 후보 [(market, stat, pump)], 제외 후보 [(market, 사유)])
    """
    adds, drops = [], []
    for m, s in stats.items():
        w = s.get("windows") or []
        if m in universe:
            if len(w) == 3 and all(v < DROP_MAX_VALUE for v in w):
                drops.append((m, "3개월 연속 거래대금 기준 미달 (" + "/".join(f"{v / 1e8:.0f}억" for v in w) + ")"))
            continue
        if m.split("-")[1] in STABLECOINS:
            continue
        ev = events.get(m, {})
        if ev.get("warning") or ev.get("caution"):
            continue
        if s.get("days", 0) < ADD_MIN_DAYS or len(w) < 3 or not all(v >= ADD_MIN_VALUE for v in w):
            continue
        pump = s.get("max_day", 0) >= PUMP_RATIO * (sum(w) / 3)
        adds.append((m, s, pump))
    for m in universe:
        if events and m not in events:
            drops.append((m, "업비트 KRW 마켓에 없음(상장폐지)"))
        elif events.get(m, {}).get("warning"):
            drops.append((m, "투자유의 지정"))
    adds.sort(key=lambda a: -a[1]["windows"][0])
    return adds, drops
