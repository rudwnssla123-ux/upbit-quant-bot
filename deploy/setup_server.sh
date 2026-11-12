#!/usr/bin/env bash
# 우분투 서버에서 실행:  bash ~/bot/setup_server.sh
# 처음 설치와 코드 업데이트 모두 이 스크립트 하나로 한다 (상태 파일 bot_v5_state.json 은 건드리지 않음).
set -euo pipefail
BOT_DIR="$HOME/bot"
cd "$BOT_DIR"

say() { echo -e "\n▶ $*"; }
die() { echo -e "\n❌ $*" >&2; exit 1; }

for f in bot_v5.py strategy_v5.py upbit_market.py backtest_v5.py backup_state.py monthly_check.py add_capital.py requirements.txt bot_v5.service .env; do
  [ -f "$f" ] || die "$BOT_DIR/$f 가 없습니다. 업로드를 먼저 하세요."
done

say ".env 점검"
sed -i 's/\r$//' .env bot_v5.service              # 윈도우 메모장의 CRLF 제거 (키 끝에 \r 이 붙는 것 방지)
chmod 600 .env
if grep -nE '^[A-Za-z_]+=.*[[:space:]]#' .env; then
  die "위 줄처럼 값 뒤에 주석(#)이 있으면 systemd 가 주석까지 값으로 읽습니다. 주석을 별도 줄로 옮기세요."
fi
get() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }
DRY="$(get DRY_RUN)"; CAP="$(get BOT_CAPITAL)"
if [ "$DRY" = "0" ]; then
  [ -n "$(get UPBIT_ACCESS)" ] && [ -n "$(get UPBIT_SECRET)" ] || die "DRY_RUN=0 인데 UPBIT_ACCESS/UPBIT_SECRET 이 비어 있습니다."
  echo "  모드: 실거래(LIVE) | BOT_CAPITAL=${CAP:-1000000}"
else
  echo "  모드: 가상 체결(DRY)"
fi
[ -n "$(get TELEGRAM_TOKEN)" ] || echo "  ℹ 텔레그램 미설정 (알림 없음)"
[ -n "$(get HEALTHCHECK_URL)" ] || echo "  ℹ HEALTHCHECK_URL 미설정 (봇이 멈춰도 외부 알림 없음)"

say "시스템 패키지 설치 / 시간대 KST"
sudo apt-get update -y -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv python3-pip tzdata curl
sudo timedatectl set-timezone Asia/Seoul || true
sudo timedatectl set-ntp true || true

MEM=$(free -m | awk '/Mem:/{print $2}')
if [ "$MEM" -lt 1500 ] && [ "$(swapon --show | wc -l)" -eq 0 ]; then
  say "메모리 ${MEM}MB → 스왑 1GB 추가 (pandas 설치·실행 안정성)"
  sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile >/dev/null && sudo swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

say "파이썬 가상환경 / 패키지"
[ -d venv ] || python3 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt

say "업비트 연결 점검"
IP=$(curl -s --max-time 5 https://checkip.amazonaws.com || echo "확인 실패")
echo "  이 서버의 공인 IP: $IP  ← 업비트 API 키의 '허용 IP'에 이 주소를 등록해야 실거래 주문이 됩니다."
venv/bin/python - <<'PY'
import os
import bot_v5, pyupbit
px = pyupbit.get_current_price("KRW-BTC")
print(f"  시세 조회 OK (BTC {px:,.0f}원)" if px else "  ⚠ 시세 조회 실패")
if not bot_v5.DRY_RUN:
    try:
        up = pyupbit.Upbit(os.environ["UPBIT_ACCESS"], os.environ["UPBIT_SECRET"])
        krw = up.get_balance("KRW")
        if krw is None or isinstance(krw, dict):
            raise RuntimeError(krw)
        krw = float(krw)
        print(f"  계좌 조회 OK | KRW 잔고 {krw:,.0f}원 | BOT_CAPITAL {bot_v5.BOT_CAPITAL:,.0f}원")
        if krw < bot_v5.BOT_CAPITAL:
            print("  ⚠ KRW 잔고가 BOT_CAPITAL 보다 적습니다. 봇은 실제 잔고까지만 사용합니다.")
    except Exception as e:
        raise SystemExit(f"  ❌ 계좌 조회 실패: {e}\n     키 값, '자산조회·주문' 권한, 허용 IP 등록을 확인하세요.")
PY

if [ "$DRY" = "0" ] && [ -t 0 ]; then
  read -r -p $'\n실거래(LIVE)로 봇을 시작합니다. 시작 즉시 리밸런싱 매수가 일어날 수 있습니다. 계속할까요? (y/N) ' ans
  [ "$ans" = "y" ] || [ "$ans" = "Y" ] || die "취소했습니다. (설치는 완료됨, 서비스는 시작하지 않음)"
fi

say "systemd 서비스 등록 (부팅 시 자동 시작, 죽으면 10초 후 재시작)"
sed "s#/home/ubuntu/bot#$BOT_DIR#g; s#^User=ubuntu#User=$USER#" bot_v5.service \
  | sudo tee /etc/systemd/system/bot_v5.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable bot_v5 >/dev/null 2>&1
sudo systemctl restart bot_v5
sleep 8
systemctl --no-pager --lines=15 status bot_v5 || true

say "월간 자동 점검 등록 (매월 1일 09:30 KST, 결과는 텔레그램)"
CRON_LINE="30 9 1 * * cd $BOT_DIR && PYTHONIOENCODING=utf-8 $BOT_DIR/venv/bin/python monthly_check.py >> $BOT_DIR/monthly_check.log 2>&1"
( crontab -l 2>/dev/null | grep -v 'monthly_check.py' || true; echo "$CRON_LINE" ) | crontab -
crontab -l | grep monthly_check

say "봇 장부 매일 백업 등록 (매일 09:20 KST, 서버 backups/ 30일 + 일요일 텔레그램 파일)"
BACKUP_LINE="20 9 * * * cd $BOT_DIR && PYTHONIOENCODING=utf-8 $BOT_DIR/venv/bin/python backup_state.py >> $BOT_DIR/backup_state.log 2>&1"
( crontab -l 2>/dev/null | grep -v 'backup_state.py' || true; echo "$BACKUP_LINE" ) | crontab -
crontab -l | grep backup_state

cat <<'TXT'

✅ 완료. 자주 쓰는 명령
  실시간 로그     journalctl -u bot_v5 -f
  상태            systemctl status bot_v5
  중지 / 시작     sudo systemctl stop bot_v5   /   sudo systemctl start bot_v5
  .env 수정 후    nano ~/bot/.env   →   sudo systemctl restart bot_v5
  월간 점검 수동  cd ~/bot && venv/bin/python monthly_check.py
TXT
