"""테스트 공용 준비: 실제 외부 연동 차단 + pyupbit 가 없는 환경(CI 등)용 가짜 모듈."""
import os
import sys
import types

# bot_v5 는 import 시 프로젝트 .env 를 읽는다(이미 있는 환경변수는 덮어쓰지 않음).
# 테스트가 실제 텔레그램·헬스체크·업비트 키를 쓰지 않도록 빈 값/가상 모드로 먼저 고정한다.
SAFE_ENV = {"DRY_RUN": "1", "TELEGRAM_TOKEN": "", "TELEGRAM_CHAT_ID": "", "HEALTHCHECK_URL": "",
            "UPBIT_ACCESS": "", "UPBIT_SECRET": ""}


def isolate_env():
    os.environ.update(SAFE_ENV)


def install_pyupbit_stub():
    isolate_env()
    try:
        import pyupbit  # noqa: F401
    except ImportError:
        stub = types.ModuleType("pyupbit")
        stub.Upbit = object
        stub.get_ohlcv = lambda *a, **k: None
        stub.get_current_price = lambda *a, **k: None
        stub.get_orderbook = lambda *a, **k: None
        sys.modules["pyupbit"] = stub


isolate_env()                    # tests 패키지를 import 하는 순간에도 적용
