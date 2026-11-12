---
name: deploy
description: upbit_bot_project 의 코드/설정을 AWS Lightsail 실거래 서버에 안전하게 반영한다 (테스트 → 시간·장부 확인 → 업로드 → 비대화식 설치 → 로그 검증).
disable-model-invocation: true
argument-hint: "[메모: 무엇을 반영하는지]"
---

# /deploy — 실거래 서버 반영 절차

반영 내용: $ARGUMENTS

서버: `<SERVER>`, 경로 `~/bot`, 서비스 `bot_v5`. **`<SERVER>` 와 키 경로는 `CLAUDE.local.md` 의 값으로 바꿔 실행한다.**
**서버 명령은 반드시 PowerShell 도구의 ssh/scp 로** 실행한다 (Git Bash ssh 는 known_hosts 가 달라 실패).
아래 단계 중 하나라도 실패하면 즉시 멈추고 사용자에게 보고한다. 단계를 건너뛰지 않는다.

## 1. 로컬 사전 점검
1. `python -m pytest -q` 와 `python -m ruff check .` 가 모두 통과해야 한다.
2. `strategy_v5.py` 의 `Params` 기본값이 바뀌었다면: 백테스트 검증 결과(IS·OOS·비용 2배·이웃 파라미터, MDD -25% 이내)와
   **사용자의 명시적 승인**이 이 대화에 있어야 한다. 없으면 중단.
3. `python deploy/env_check.py` 로 `.env` 점검: 경고(⚠) 가 있으면 사용자에게 알리고, 모드(DRY/LIVE)를 기록한다.

## 2. 서버 상태 확인
PowerShell:
```
ssh -i C:\keys\lightsail.pem -o BatchMode=yes <SERVER> "date '+%H:%M'; systemctl is-active bot_v5; ~/bot/venv/bin/python -c \"import json;s=json.load(open('/home/ubuntu/bot/bot_v5_state.json'));print(s['mode'],s['last_day'],len(s['positions']))\""
```
- 서버 시각이 **08:50~09:15** 이면 중단하고 09:15 이후에 하자고 안내한다 (일일 리밸런싱과 겹침).
- 장부 `mode` 와 로컬 `.env` 모드가 다르면 중단 (모드가 다르면 봇이 시작을 거부한다. 전환은 deploy/README.md 6절을 사용자에게 안내).
- `last_day` 가 오늘 업비트 기준일(09:00 KST 이전이면 어제 날짜)이 아니면: 재시작 직후 리밸런싱 주문이 나간다는 점을 알리고 사용자 확인을 받는다.
- 모드가 LIVE 면 "실거래 봇을 재시작합니다"라고 사용자에게 한 번 확인받는다 (setup 스크립트의 y/N 질문은 비대화식이라 건너뛰어지므로 여기서 대신 확인).

## 3. 업로드
PowerShell (프로젝트 폴더에서):
```
scp -i C:\keys\lightsail.pem bot_v5.py strategy_v5.py upbit_market.py backtest_v5.py backup_state.py monthly_check.py add_capital.py requirements.txt bot_v5.service deploy\setup_server.sh .env <SERVER>:~/bot/
```
`deploy\upload.ps1` 는 마지막 단계에서 y/N 입력을 기다리며 멈추므로 Claude 는 사용하지 않는다.

## 4. 서버 설치·재시작 (비대화식)
```
ssh -i C:\keys\lightsail.pem -o BatchMode=yes <SERVER> "bash ~/bot/setup_server.sh < /dev/null 2>&1 | tail -25"
```
타임아웃은 600000ms 로 준다. `❌` 가 출력되면 중단하고 그대로 보고한다.

## 5. 검증
```
ssh -i C:\keys\lightsail.pem -o BatchMode=yes <SERVER> "systemctl is-active bot_v5; journalctl -u bot_v5 --since '-2 min' --no-pager -o cat | tail -8; grep -c '텔레그램 전송 실패' ~/bot/bot_v5_log.txt; crontab -l | grep -c monthly_check"
```
확인할 것:
- `active`
- 시작 로그에 `모드=LIVE`(또는 의도한 모드), 봇현금·포지션이 배포 전과 일치
- 텔레그램 실패 건수가 배포 전보다 늘지 않음
- 월간 점검 cron 1건

## 6. 보고
반영한 파일, 재시작 시각, 모드·봇현금·포지션 수, 이상 여부를 사용자에게 짧게 보고한다.
