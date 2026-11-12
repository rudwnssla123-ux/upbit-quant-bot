# upbit_bot_project — Claude 작업 규칙

업비트 KRW 현물 퀀트 포트폴리오 봇 v5. **실제 돈으로 24시간 실거래 중**이다. 답변은 한국어로 한다.

## 구성
| 파일 | 역할 |
|---|---|
| `strategy_v5.py` | 전략 로직(앙상블 추세점수 → 위험조정 모멘텀 순위 상위 5 → 역변동성 비중 → 목표변동성 30% → BTC 국면 → 낙폭 연동). 실거래 설정은 `LIVE`(v5.1: 순위 모멘텀 20/30/45/60/90일 앙상블). 봇·백테스트 공용 |
| `bot_v5.py` | 실행 봇. 매일 09:00 KST 리밸런싱, 상태 `bot_v5_state.json`, 텔레그램·헬스체크 |
| `backtest_v5.py` | 검증 리포트 (`--quick` 요약, `BASE = LIVE`). 기준치(v5.1, 17종목): 연 47.0% / MDD -19.4%, OOS(2023~) 40.7%. 단 17종목은 사후 선택이라 낙관적. 생존편향 제거(`research/pit_universe.py`, 당시 거래대금 상위 17): 연 30.4% / MDD -21.9%, OOS 22.3% |
| `upbit_market.py` | 투자유의·상장폐지 조회(봇이 매일 자동 제외·매도), 종목 편입/제외 후보 규칙(월간 보고서가 제안만) |
| `monthly_check.py` | 서버 cron 매월 1일 09:30 → 텔레그램 보고 (알리기만 함) |
| `backup_state.py` | 서버 cron 매일 09:20 → 장부를 `~/bot/backups/`(30일) + 매주 일요일 텔레그램 파일로 백업 (읽기만 함). 복구는 `deploy/README.md` |
| `add_capital.py` | 자금 증액/감액. 서버에서 봇 정지 후 실행 |
| `deploy/` | `upload.ps1`(PC→서버), `setup_server.sh`(서버 설치), `env_check.py`(.env 안전 점검), 운영 가이드 |

## 명령
- 테스트: `python -m pytest -q` (61개 내외, ~5초) / 린트: `python -m ruff check .`
- 백테스트: `PYTHONIOENCODING=utf-8 python backtest_v5.py --quick` (윈도우 cp949 콘솔 대비)
- `.env` 점검(비밀값 미출력): `python deploy/env_check.py`

## 운영 환경
- 서버: AWS Lightsail, Ubuntu 24.04, 봇 경로 `~/bot`, 서비스 `bot_v5`. **서버 주소(`<SERVER>`)·키 경로·운용 자금·보호 종목 사유는 `CLAUDE.local.md`(git 제외)** 에 있다
- 서버 접속은 **PowerShell 도구의 ssh/scp** 를 쓴다 (Git Bash ssh 는 known_hosts 가 달라 실패)
- 업비트 API 키는 출금 권한 없이 서버 IP 로만 허용. 자금 변경은 서버에서 `add_capital.py --target <금액>`
- 대상 종목 17개. `bot_v5.PROTECTED` 종목은 사용자가 직접 보유하므로 봇이 거래하지 않는다(UNIVERSE 편입 금지)
- **이 저장소는 공개(GitHub)다.** 개인 정보(서버 IP, 금액, 보유 내역)는 `CLAUDE.local.md` 에만 쓰고, 커밋 전 개인정보가 없는지 확인한다

## 절대 규칙
1. **PC 에서 `bot_v5.py` 를 실행하지 않는다.** PC 의 `.env` 도 LIVE 라서 같은 계좌에 봇이 두 개가 된다. 로직 확인은 테스트나 임시 폴더의 DRY 상태로만.
2. **`.env`, `*.pem` 내용을 출력·읽기·수정하지 않는다.** 값 확인은 `deploy/env_check.py` 로만. 토큰·키를 채팅에 요청하지 않는다(사용자가 직접 파일에 입력).
3. **`bot_v5_state.json`(봇 장부)을 직접 수정·삭제·이동하지 않는다.** 자금 변경은 `add_capital.py`, 모드 전환 등은 사용자에게 명령을 안내한다.
4. **`strategy_v5.Params` 기본값과 `LIVE` 설정은 검증 없이 바꾸지 않는다.** 변경 시 기준: IS·OOS 모두 개선, 비용 2배에서도 유지, 이웃 파라미터에서 붕괴 없음, MDD -25% 이내. 통과해도 사용자 승인 후에만 배포.
5. **배포는 사용자가 요청했을 때 `/deploy` 절차로만** 한다. 08:50~09:15 KST 에는 재시작하지 않는다.
6. 서버 `.env` 는 systemd EnvironmentFile 이라 **값 뒤 주석 금지**(주석은 별도 줄).

## 코드 스타일
- ruff 설정은 `pyproject.toml` (줄 120자, `import datetime as dt`, `pandas as pd`, `numpy as np`)
- 전략은 '직전 완성 일봉'만 사용(룩어헤드 금지). 새 로직에는 `tests/` 에 테스트 추가
- 콘솔 출력 문자열에 em dash(—) 등 cp949 미지원 문자를 쓰지 않는다
