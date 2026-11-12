# 24시간 서버 운영 가이드 (bot_v5)

PC 를 계속 켜둘 필요 없이 클라우드 리눅스 서버에서 봇을 돌립니다. 서버가 재부팅되거나 봇이 죽어도 자동으로 다시 시작됩니다.

## 1. 서버 만들기 (직접, 약 10분)
**권장: AWS Lightsail 서울 리전** (가장 단순, 고정 IP 무료 제공)
**Create instance 화면 (위에서 아래 순서)**
| 항목 | 선택 |
|---|---|
| Instance location | **Change AWS Region** → **Seoul (ap-northeast-2)**, 영역은 기본(a) |
| Select a platform | **Linux/Unix** |
| Select a blueprint | **Operating System (OS) only** 탭 → **Ubuntu 24.04 LTS** (Linux apps 탭 아님: 그건 WordPress 등이 미리 깔린 이미지) |
| Launch script / SSH key pair | 건드리지 않음 (서울 기본 키 사용) |
| Automatic snapshots | 끔 (추가 요금) |
| Network type | **Dual-stack** (IPv6-only 는 업비트 IP 허용·IPv4 사이트 접속 문제로 쓰지 않음) |
| Instance plan | General purpose, **메모리 1GB** 플랜 권장 (512MB 도 동작, 스크립트가 스왑 추가) |
| Identify your instance | 이름 예: `upbit-bot` → **Create instance** |

**생성 후 (상태가 Running 이 되면)**
1. 고정 IP: 인스턴스 → **Networking** 탭 → **Attach static IP**(또는 홈 → Networking → Create static IP) → 서울, 이 인스턴스 선택 → 생성.
   인스턴스에 붙어 있는 동안 무료. 이 IP 를 업비트 API 허용 IP 와 업로드 명령에 쓴다.
2. 방화벽: 같은 Networking 탭 **IPv4 Firewall** 에서 **HTTP(80) 규칙 삭제**, **SSH(22)만** 남김. (IPv6 Firewall 도 동일)
3. 키 파일: 우측 상단 계정 메뉴 → **Account** → **SSH keys** 탭 → Default keys 의 **Seoul** 옆 다운로드 →
   `C:\keys\lightsail.pem` 처럼 **한글이 없는 경로**에 저장 (윈도우 ssh 가 한글 경로에서 실패하는 경우가 있음)
4. 접속 테스트(선택): Lightsail 인스턴스 화면의 **Connect using SSH**(브라우저 터미널)가 열리면 서버 정상

**EC2 로 만드는 경우** (화면에 '애플리케이션 및 OS 이미지'가 보이면 EC2 입니다)
| 항목 | 선택 |
|---|---|
| 리전(우측 상단) | 아시아 태평양(서울) |
| 애플리케이션 및 OS 이미지 | **Ubuntu** → Ubuntu Server 24.04 LTS (Amazon Linux 아님: 설치 스크립트가 apt·ubuntu 사용자 기준) |
| 인스턴스 유형 | t3.micro 또는 t2.micro (프리 티어 표시된 것) |
| 키 페어 | 새 키 페어 생성 → RSA, `.pem` → 내려받은 파일을 안전한 곳에 보관 |
| 네트워크 설정 | SSH 트래픽 허용 → 소스 **내 IP** (HTTP/HTTPS 허용은 체크 해제) |
| 스토리지 | 기본 8GB 그대로 |
생성 후 왼쪽 메뉴 **탄력적 IP → 탄력적 IP 주소 할당 → 인스턴스에 연결** 로 고정 IP 를 붙인다.
(공인 IPv4 는 AWS 에서 시간당 소액 과금될 수 있음. 집 IP 가 바뀌어 SSH 가 막히면 보안 그룹의 '내 IP'를 다시 지정)

> 대안: Oracle Cloud Always Free(무료, 가입·자원 확보가 까다로움), Vultr/DigitalOcean 서울·도쿄 리전. 기본 사용자명이 `ubuntu` 가 아니면 업로드 시 `-User` 로 지정.

## 2. 업비트 API 키 발급
업비트 → 마이페이지 → Open API 관리
- 권한: **자산조회, 주문하기**만 체크 (**출금하기는 절대 체크하지 않음**)
- 허용 IP: 1단계의 **고정 IP**
- Access / Secret 키를 `.env` 에 넣는다

## 3. 헬스체크(선택이지만 권장): HEALTHCHECK_URL
봇이 **죽었을 때**는 봇 스스로 텔레그램을 보낼 수 없습니다. 그래서 외부 감시 서비스에 봇이 살아 있다는 신호를 주기적으로 보내고,
신호가 끊기면 그 서비스가 대신 알려주게 합니다 ("데드맨 스위치").
1. https://healthchecks.io 무료 가입 → **Add Check**
2. Period **5 minutes**, Grace **10 minutes** 로 설정
3. **Integrations** 에서 Telegram(또는 이메일) 연결
4. 체크 화면의 Ping URL(`https://hc-ping.com/xxxxxxxx-...`)을 `.env` 의 `HEALTHCHECK_URL=` 에 붙여넣기

봇은 정상 루프를 돌 때만 4~5분마다 이 URL 을 호출합니다. 서버 다운·봇 크래시·오류 반복 시 신호가 끊겨 알림이 옵니다.

## 4. 업로드 + 설치 (PC 에서 한 줄)
프로젝트 폴더에서 PowerShell:
```
powershell -ExecutionPolicy Bypass -File deploy\upload.ps1 -ServerHost <고정IP> -KeyFile C:\keys\lightsail.pem
```
자동으로 하는 일: 파일 업로드 → `.env` 점검(CRLF·주석 오류) → 파이썬/패키지 설치 → 시간대 KST → 업비트 시세·계좌 조회 확인
→ (실거래면 y/N 확인) → systemd 서비스 등록·시작. 코드를 고친 뒤 같은 명령을 다시 실행하면 업데이트됩니다(상태 파일 유지).

## 5. 운영 명령 (서버 접속: `ssh -i <키> ubuntu@<고정IP>`)
| 하고 싶은 것 | 명령 |
|---|---|
| 실시간 로그 | `journalctl -u bot_v5 -f` |
| 상태 확인 | `systemctl status bot_v5` |
| 중지 / 시작 | `sudo systemctl stop bot_v5` / `sudo systemctl start bot_v5` |
| 설정 변경 | `nano ~/bot/.env` → `sudo systemctl restart bot_v5` |
| 봇 장부 보기 | `cat ~/bot/bot_v5_state.json` |
| 장부 백업 확인 | `ls ~/bot/backups` (매일 09:20 자동, 30일 보관 + 매주 일요일 텔레그램에 파일로 무음 전송) |

### 장부 복구 (장부가 깨졌거나 서버를 새로 만들었을 때)
1. 서버에서 `sudo systemctl stop bot_v5`
2. 되살릴 백업 고르기: 서버가 살아 있으면 `ls ~/bot/backups`, 서버가 사라졌으면 텔레그램의 '장부 백업' 파일을 PC 로 받아
   `scp -i <키> state_YYYY-MM-DD.json ubuntu@<고정IP>:~/bot/`
3. `cp ~/bot/backups/state_YYYY-MM-DD.json ~/bot/bot_v5_state.json` (또는 2에서 올린 파일)
4. `sudo systemctl start bot_v5` 후 `cd ~/bot && venv/bin/python monthly_check.py --no-send` 로 '장부 대조'(거래소 잔고 vs 장부) 확인.
   백업 이후 매매는 장부에 없으므로 차이가 나면 Claude 에게 보여 주고 함께 맞춘다

## 6. 가상(DRY) → 실거래(LIVE) 전환
1. DRY 로 며칠~몇 주 운영하며 텔레그램 알림·매매 로그 확인
2. 서버에서 `sudo systemctl stop bot_v5`
3. `mv ~/bot/bot_v5_state.json ~/bot/bot_v5_state.dry.json` (모드가 다른 상태 파일이 있으면 봇이 시작을 거부함)
4. `nano ~/bot/.env` 에서 `DRY_RUN=0`, `BOT_CAPITAL=<운용금액>` 확인
5. PC 에서 4단계 업로드 명령을 다시 실행하거나(.env 도 PC 쪽 것으로 덮어씀 — PC 의 .env 도 같이 수정), 서버에서 `bash ~/bot/setup_server.sh`
6. 시작 직후 첫 리밸런싱 매수가 일어나므로 로그로 체결 확인

## 주의
- `.env` 에는 API 키가 들어 있으니 git·OneDrive 공유·메신저로 보내지 마세요. 서버에서는 600 권한으로 저장됩니다.
- 업로드는 `.env` 를 포함합니다. PC 쪽 `.env` 가 서버의 원본이라고 생각하고 관리하세요.
- 같은 계좌로 봇을 두 곳 이상(PC·다른 서버)에서 동시에 돌리지 마세요.
