"""pytest 가 어떤 테스트보다 먼저 읽는 파일: 실제 .env 의 텔레그램·업비트 값 사용 차단."""
from tests._stubs import isolate_env

isolate_env()
