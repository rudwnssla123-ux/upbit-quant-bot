"""
strategy_v5.py — v5 공용 전략 로직 (backtest_v5.py 와 bot_v5.py 가 같은 코드를 쓴다)

모델: 앙상블 시계열 모멘텀 + 횡단면 모멘텀 로테이션 + 리스크 패리티 + 변동성 타기팅 + 낙폭 연동 디레버리징
  1) 추세 점수  score_i = mean_L 1[r_i(L) > 0],  L ∈ {10, 20, 40, 80, 160}일
     하나의 룩백에 베팅하지 않고 여러 룩백을 평균 → 파라미터 과최적화에 덜 민감하다.
  2) 후보: score > 0, 상장 후 충분한 이력, 30일 평균 거래대금 하한 충족
  3) 선택: 위험조정 모멘텀 m_i = r_i(60) / σ_i 상위 top_k 종목
  4) 가중치: w_i ∝ score_i / σ_i (변동성 역가중, 추세 강도 반영) → 합 1 로 정규화
  5) 변동성 타기팅: 최근 60일 공분산 Σ 로 포트폴리오 변동성 σ_p = sqrt(wᵀΣw) 를 구해
     w ← w · min(1, target_vol / σ_p)   (현물이므로 레버리지 없음, 총비중 ≤ 1)
  6) 시장 국면: BTC 종가가 50일선·100일선 위인 비율(0, 0.5, 1)을 곱한다
  7) 낙폭 연동: 최근 dd_win 일 계좌 고점 대비 낙폭 dd 에 대해 w ← w · max(dd_floor, 1 + dd / dd_scale)
  8) 종목당 상한 max_w, 리밸런싱 버퍼 buf (비중 변화가 buf 이하면 거래하지 않음 → 회전율·수수료 절감)
모든 신호는 '직전 완성 일봉'까지의 데이터만 쓴다 (룩어헤드 없음).
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

MARKET = "KRW-BTC"
# 2018~2026 업비트 KRW 마켓에서 거래대금이 꾸준히 컸던 종목.
# KRW-XRP: 계좌에서 따로 보유하는 종목이라 제외 (봇 물량과 섞이지 않게, bot_v5.PROTECTED).
#          봇에 맡기려면 이 목록에 다시 넣고 bot_v5.PROTECTED 에서 빼면 된다 (편입 시 백테스트 OOS +0.6~2.6%p).
UNIVERSE = ["KRW-BTC", "KRW-ETH", "KRW-SOL", "KRW-DOGE", "KRW-ADA", "KRW-AVAX", "KRW-LINK", "KRW-DOT",
            "KRW-TRX", "KRW-BCH", "KRW-ETC", "KRW-XLM", "KRW-ATOM", "KRW-NEAR", "KRW-SUI", "KRW-HBAR",
            "KRW-SHIB"]


@dataclass(frozen=True)
class Params:
    lookbacks: tuple = (10, 20, 40, 80, 160)   # 추세 점수 룩백(일)
    mom_n: int = 60                 # 위험조정 모멘텀 순위 룩백 (mom_ens 가 비어 있을 때만 사용)
    mom_ens: tuple = ()             # 비어 있지 않으면 이 기간들의 위험조정 모멘텀 '종목 간 순위(백분위)' 평균으로 순위
    top_k: int = 5                  # 동시 보유 최대 종목 수
    vol_span: int = 30              # 변동성 EWMA span
    cov_n: int = 60                 # 포트폴리오 공분산 추정 창
    target_vol: float = 0.30        # 연환산 목표 변동성
    regime_sma: tuple = (50, 100)   # BTC 시장 국면 이동평균
    dd_scale: float = 0.30          # 낙폭 30% 에서 비중 0 (단, dd_floor 이하로는 줄이지 않음)
    dd_floor: float = 0.25
    dd_win: int = 180               # 낙폭 기준 고점을 보는 기간(일)
    max_w: float = 0.35             # 종목당 최대 비중
    buf: float = 0.05               # 리밸런싱 버퍼(비중 절대값)
    entry_min: float = 0.0          # >0: 신규 종목은 목표 비중이 이 값 이상이면 버퍼와 무관하게 매수 (0 = 버퍼 규칙만)
    min_hist: int = 60              # 최장 룩백 이후 추가로 필요한 상장 일수
    min_liq: float = 5e9            # 30일 평균 일 거래대금 하한(원)
    fee: float = 0.0005             # 업비트 KRW 마켓 수수료
    slip: float = 0.001             # 가정 슬리피지

    @property
    def warmup(self):
        return max(max(self.lookbacks) + self.min_hist, max(self.regime_sma), self.cov_n) + 5


# 실거래 설정 (bot_v5.PARAMS 와 backtest_v5.BASE 가 함께 쓴다). v5.1: 순위 모멘텀 앙상블 (2026-10-12 검증)
LIVE = Params(mom_ens=(20, 30, 45, 60, 90))


def indicators(close, value, p):
    """close/value: 날짜×종목 DataFrame (상장 전은 NaN). 반환: 지표 DataFrame dict."""
    ret = close.pct_change(fill_method=None)
    score = sum((close / close.shift(n) - 1 > 0).astype(float) for n in p.lookbacks) / len(p.lookbacks)
    score = score.where(close.notna())
    vol = ret.ewm(span=p.vol_span, min_periods=20).std() * np.sqrt(365)
    btc = close[MARKET]
    regime = sum((btc > btc.rolling(n).mean()).astype(float) for n in p.regime_sma) / len(p.regime_sma)
    return {
        "ret": ret,
        "score": score,
        "vol": vol,
        "mom": momentum(close, vol, p),
        "liq": value.rolling(30).mean(),
        "hist": close.notna().cumsum(),
        "regime": regime,
    }


def momentum(close, vol, p):
    """종목 순위용 점수 (클수록 상위). 단일 기간이면 위험조정 수익률, 앙상블이면 기간별 종목 간 백분위 순위의 평균.
    한 기간에 기대지 않아 기간 선택 과최적화를 줄인다 (research/mom_ensemble.py 검증)."""
    if not p.mom_ens:
        return (close / close.shift(p.mom_n) - 1) / vol
    ranks = [((close / close.shift(n) - 1) / vol).rank(axis=1, pct=True) for n in p.mom_ens]
    return sum(ranks) / len(ranks)


def dd_multiplier(p, dd):
    """dd: 고점 대비 낙폭(≤0). 반환: 비중 배율."""
    if p.dd_scale <= 0 or dd >= 0:
        return 1.0
    return max(p.dd_floor, 1 + dd / p.dd_scale)


def weights_at(p, ind, i, dd=0.0):
    """ind 의 i 번째 행(완성 일봉)까지만 보고 목표 비중 Series 를 반환 (합 ≤ 1, 나머지는 현금)."""
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
    cov = ind["ret"].iloc[max(0, i - p.cov_n + 1):i + 1][sel].fillna(0).cov().to_numpy() * 365
    pv = float(np.sqrt(max(raw.to_numpy() @ cov @ raw.to_numpy(), 0)))
    scale = min(1.0, p.target_vol / pv) if pv > 0 else 0.0
    w[sel] = (raw * scale * g * dd_multiplier(p, dd)).clip(upper=p.max_w)
    return w


def target_weights(p, close, value, dd=0.0):
    """라이브용: 완성 일봉만 담긴 close/value 의 마지막 행 기준 목표 비중."""
    ind = indicators(close, value, p)
    return weights_at(p, ind, len(close) - 1, dd)


def apply_buffer(p, target, current):
    """비중 변화가 buf 이하이면 현재 비중 유지. 목표가 0 이 된 종목은 항상 전량 청산.
    entry_min > 0 이면 미보유 종목의 신규 매수는 목표 비중이 entry_min 이상일 때 허용(작은 비중 종목이 영영 안 사지는 문제)."""
    cols = target.index.union(current.index)
    t, c = target.reindex(cols, fill_value=0.0), current.reindex(cols, fill_value=0.0)
    move = ((t - c).abs() > p.buf) | ((t == 0) & (c > 0))
    if p.entry_min > 0:
        move |= (c == 0) & (t >= p.entry_min)
    return c.where(~move, t)


def drawdown(eq_hist, eq_now, win):
    """eq_hist: 과거 일별 계좌 평가액 리스트. 최근 win 개와 현재값 중 최고점 대비 낙폭."""
    hist = list(eq_hist[-win:]) + [eq_now]
    pk = max(hist)
    return eq_now / pk - 1 if pk > 0 else 0.0
