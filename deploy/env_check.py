"""
env_check.py — .env 를 비밀값 출력 없이 점검한다.
    python deploy/env_check.py              형식 점검 + 텔레그램 토큰 유효성
    python deploy/env_check.py --offline    네트워크 확인 생략
출력: DRY_RUN / BOT_CAPITAL 값, 각 키의 입력 여부, 값 뒤 주석·따옴표·공백 문제, 텔레그램 인증·채팅 도달 여부.
"""
import os
import sys

KEYS = ["DRY_RUN", "BOT_CAPITAL", "UPBIT_ACCESS", "UPBIT_SECRET", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID",
        "HEALTHCHECK_URL"]
SHOW = {"DRY_RUN", "BOT_CAPITAL"}                      # 비밀이 아닌 값만 그대로 표시


def parse(path):
    env, problems = {}, []
    with open(path, encoding="utf-8") as f:
        for n, raw in enumerate(f, 1):
            s = raw.rstrip("\r\n")
            if not s.strip() or s.lstrip().startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            if " #" in v or "\t#" in v:
                problems.append(f"{n}행 {k}: 값 뒤 주석 (systemd 가 주석까지 값으로 읽음)")
            if v != v.strip():
                problems.append(f"{n}행 {k}: 값 앞뒤 공백")
            if v.strip()[:1] in "\"'":
                problems.append(f"{n}행 {k}: 따옴표로 감싸짐")
            env[k] = v.strip()
    return env, problems


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, ".env")
    if not os.path.exists(path):
        raise SystemExit(".env 없음 (.env.example 을 복사해 작성)")
    env, problems = parse(path)
    for k in KEYS:
        v = env.get(k, "")
        print(f"  {k:<17} {v if k in SHOW else ('입력됨' if v else '비어 있음')}")
    print(f"  모드: {'실거래(LIVE)' if env.get('DRY_RUN') == '0' else '가상(DRY)'}")
    for p in problems:
        print(f"  ⚠ {p}")
    if "--offline" in sys.argv or not env.get("TELEGRAM_TOKEN"):
        return
    import requests
    tok = env["TELEGRAM_TOKEN"]
    me = requests.get(f"https://api.telegram.org/bot{tok}/getMe", timeout=10).json()
    print(f"  텔레그램 토큰: {'정상 @' + me['result']['username'] if me.get('ok') else '거부됨 ' + me.get('description', '')}")
    if me.get("ok") and env.get("TELEGRAM_CHAT_ID"):
        c = requests.get(f"https://api.telegram.org/bot{tok}/getChat", params={"chat_id": env["TELEGRAM_CHAT_ID"]},
                         timeout=10).json()
        print(f"  텔레그램 채팅: {'도달 가능' if c.get('ok') else '불가 ' + c.get('description', '')}")


if __name__ == "__main__":
    main()
