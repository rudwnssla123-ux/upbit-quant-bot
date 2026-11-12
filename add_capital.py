"""
add_capital.py — 봇 운용 자금 증액/감액 (서버에서 봇을 멈춘 상태로 실행)
    sudo systemctl stop bot_v5
    venv/bin/python add_capital.py 300000        # 30만원 증액 (업비트에 먼저 입금해 둘 것)
    venv/bin/python add_capital.py -100000       # 10만원 감액 (봇 장부 현금에서만 뺄 수 있음)
    venv/bin/python add_capital.py --target 5000000   # 봇 총자산(현금+코인 평가액)이 500만원이 되도록 차액만큼 증액/감액
    sudo systemctl start bot_v5
동작
  · 봇 장부 현금(cash)을 금액만큼 조정한다. 늘어난 현금은 다음 09:00 리밸런싱 때 목표 비중대로 투자된다.
  · 과거 총자산 기록(eq_hist, eq_log)과 시작자본을 '입금 직전 총자산 대비 같은 비율'로 키운다
    → 입출금이 수익률·낙폭·낙폭 연동 비중 계산을 왜곡하지 않는다 (펀드 기준가 방식).
  · 실행 전 상태파일을 bot_v5_state.json.bak-<시각> 으로 백업한다.
"""
import sys
import time
import shutil
import subprocess

import pyupbit

import bot_v5


def amount_for_target(target, eq_before):
    """봇 총자산을 target 으로 맞추는 데 필요한 입출금액 (양수 = 증액)."""
    return round(target - eq_before)


def apply_flow(st, amount, eq_before):
    """장부에 입출금 반영. eq_before: 반영 직전 총자산(현재가 기준)."""
    if eq_before <= 0:
        raise ValueError("현재 총자산이 0 이하입니다")
    if amount < 0 and -amount > st["cash"]:
        raise ValueError(f"감액 {-amount:,.0f}원 > 봇 장부 현금 {st['cash']:,.0f}원 (먼저 코인 비중이 줄어야 함)")
    k = (eq_before + amount) / eq_before
    st["cash"] += amount
    st["eq_hist"] = [v * k for v in st.get("eq_hist", [])]
    st["eq_log"] = [[d, round(v * k)] for d, v in st.get("eq_log", [])]
    st["start_capital"] = float(st.get("start_capital") or bot_v5.BOT_CAPITAL) * k
    st.setdefault("flows", []).append([bot_v5.day_key_now(), amount])
    return k


def main():
    args = sys.argv[1:]
    target = None
    if len(args) == 2 and args[0] == "--target":
        target = float(args[1].replace(",", ""))
    elif len(args) == 1:
        amount = float(args[0].replace(",", ""))
    else:
        raise SystemExit(__doc__)
    try:
        active = subprocess.run(["systemctl", "is-active", "bot_v5"], capture_output=True, text=True).stdout.strip()
    except FileNotFoundError:
        active = "unknown"
    if active == "active":
        raise SystemExit("봇이 실행 중입니다. 먼저 'sudo systemctl stop bot_v5' 후 다시 실행하세요.")

    st = bot_v5.load_state()
    prices = pyupbit.get_current_price(list(st["positions"])) if st["positions"] else {}
    if isinstance(prices, (int, float)):
        prices = {list(st["positions"])[0]: prices}
    if st["positions"] and (not prices or any(t not in prices for t in st["positions"])):
        raise SystemExit("현재가 조회 실패, 잠시 후 다시 실행하세요.")
    eq = bot_v5.equity(st, prices)
    if target is not None:
        amount = amount_for_target(target, eq)
        print(f"목표 총자산 {target:,.0f}원 - 현재 봇 총자산 {eq:,.0f}원 = {amount:+,.0f}원")

    if not bot_v5.DRY_RUN and amount > 0:
        up = pyupbit.Upbit(bot_v5.os.environ["UPBIT_ACCESS"], bot_v5.os.environ["UPBIT_SECRET"])
        krw = float(up.get_balance("KRW") or 0)
        if krw < st["cash"] + amount:
            raise SystemExit(f"업비트 KRW 잔고 {krw:,.0f}원 < 봇 장부 현금 {st['cash']:,.0f} + 증액 {amount:,.0f}원. "
                             f"먼저 입금하세요.")

    backup = f"{bot_v5.STATE_FILE}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy(bot_v5.STATE_FILE, backup)
    k = apply_flow(st, amount, eq)
    bot_v5.save_state(st)
    print(f"완료: {amount:+,.0f}원 | 총자산 {eq:,.0f} → {eq + amount:,.0f}원 | 장부 현금 {st['cash']:,.0f}원 | "
          f"기록 배율 {k:.4f} | 백업 {backup}")
    print("이제 'sudo systemctl start bot_v5'. 늘어난 현금은 다음 09:00 리밸런싱 때 투자됩니다.")


if __name__ == "__main__":
    main()
