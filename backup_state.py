"""
backup_state.py — 봇 장부(bot_v5_state.json) 매일 백업 (서버 cron 이 매일 09:20 KST 에 실행, 추가 비용 없음)
    cd ~/bot && venv/bin/python backup_state.py            서버 백업 + 텔레그램 전송
    python backup_state.py --no-send                        서버 백업만
    python backup_state.py --send                           요일과 상관없이 텔레그램도 전송
  · 서버 ~/bot/backups/state_YYYY-MM-DD.json 에 복사, 최근 KEEP 개만 보관 (장부가 깨졌을 때 되돌리기용)
  · 매주 일요일에만 텔레그램에 파일로 무음 전송 (서버 디스크가 통째로 사라졌을 때 되살리기용, 서버 밖 사본)
  · 백업 실패는 요일과 상관없이 텔레그램으로 알림
장부는 읽기만 한다. 복구 방법은 deploy/README.md '장부 복구' 참고.
"""
import datetime as dt
import glob
import json
import os
import sys

import requests

import bot_v5                                          # .env 읽기 + 텔레그램 알림 공용

STATE_FILE = "bot_v5_state.json"
BACKUP_DIR = "backups"
KEEP = 30
SEND_WEEKDAY = 6                                       # 텔레그램 전송 요일 (월=0 ... 일=6)


def snapshot(state_file=STATE_FILE, backup_dir=BACKUP_DIR, today=None, keep=KEEP):
    """장부를 날짜 파일로 복사하고 오래된 백업을 정리한다. 장부가 깨져 있으면 백업하지 않는다(좋은 백업을 지키기 위해)."""
    with open(state_file, encoding="utf-8") as f:
        raw = f.read()
    json.loads(raw)                                    # 깨진 장부면 여기서 예외
    os.makedirs(backup_dir, exist_ok=True)
    path = os.path.join(backup_dir, f"state_{today or dt.date.today()}.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(raw)
    for old in sorted(glob.glob(os.path.join(backup_dir, "state_*.json")))[:-keep]:
        os.remove(old)
    return path


def send_file(path):
    tok, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return False
    with open(path, "rb") as f:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendDocument",
                          data={"chat_id": chat, "disable_notification": "true", "caption": "[v5] 장부 백업"},
                          files={"document": (os.path.basename(path), f)}, timeout=30)
    return r.ok


def should_send(today=None, argv=None):
    """텔레그램 전송 여부: --no-send 가 없고 전송 요일일 때만. --send 는 요일과 상관없이 전송."""
    argv = sys.argv if argv is None else argv
    if "--no-send" in argv:
        return False
    return "--send" in argv or (today or dt.date.today()).weekday() == SEND_WEEKDAY


def main():
    try:
        path = snapshot()
    except Exception as e:
        print(f"{dt.datetime.now():%Y-%m-%d %H:%M} 백업 실패: {e}", flush=True)
        if "--no-send" not in sys.argv:
            bot_v5.notify(f"⚠ 장부 백업 실패: {e}")
        raise SystemExit(1)
    try:
        sent = should_send() and send_file(path)
    except Exception:                                  # 텔레그램 장애여도 서버 백업은 이미 끝남
        sent = False
    print(f"{dt.datetime.now():%Y-%m-%d %H:%M} 백업 {path} | 텔레그램 {'전송' if sent else '안 함'}", flush=True)


if __name__ == "__main__":
    main()
