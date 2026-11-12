"""
PreToolUse 가드 — 실거래 봇 사고 방지 (Claude 의 도구 호출에만 적용, 사용자의 직접 작업은 막지 않음)
  1. PC 에서 bot_v5.py 실행 차단 (같은 계좌에 봇 2개 → 중복 주문)
  2. .env / *.pem 내용 열람·수정 차단 (값 확인은 deploy/env_check.py)
  3. 봇 장부 bot_v5_state.json 직접 수정·삭제·이동 차단 (자금 변경은 add_capital.py)
차단 시 permissionDecision=deny 와 이유를 반환한다. 그 외에는 아무것도 출력하지 않는다.
"""
import json
import os
import re
import sys

SECRET_NAME = re.compile(r"(^|[\\/])(\.env|[^\\/]*\.pem)$", re.I)
STATE_NAME = re.compile(r"(^|[\\/])bot_v5_state\.json$", re.I)

# python / py 가 '명령어 자리'(줄 처음, 공백·; & | ( 따옴표 뒤, 경로 끝)에 있을 때만. 'x.py y/bot_v5.py' 같은 문자열은 제외
RUN_BOT = re.compile(r"(?:^|[\s;&|(\"'\\/])(python[0-9.]*|py)(\.exe)?\s+(-[a-zA-Z]+\s+)*(\S*[\\/])?bot_v5\.py\b", re.I)
SECRET_REF = r"(\.env(?![\w.])|\.pem\b)"
SHOW_SECRET = re.compile(
    r"\b(cat|type|more|less|head|tail|grep|egrep|fgrep|rg|findstr|Get-Content|gc|Select-String|sls|bat|nl|"
    r"strings|od|xxd|hexdump|awk|sed|cut|sort|uniq|diff|Format-Hex|notepad|code)\b[^|;&\n]*" + SECRET_REF, re.I)
WRITE_SECRET = re.compile(r"(>>?|\btee\b|Set-Content|Add-Content|Out-File)\s*[^|;&\n]*" + SECRET_REF, re.I)
STATE_WRITE = re.compile(
    r"(sed\s+-i[^|;&\n]*bot_v5_state\.json|>>?\s*\S*bot_v5_state\.json|\btee\b[^|;&\n]*bot_v5_state\.json|"
    r"\b(rm|mv|unlink|truncate|Remove-Item|Move-Item|Rename-Item|del|ren|Set-Content|Out-File|Clear-Content)\b"
    r"[^|;&\n]*bot_v5_state\.json\b|\bcp\s+\S+\s+\S*bot_v5_state\.json\b|Copy-Item\s+\S+\s+\S*bot_v5_state\.json\b)",
    re.I)


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": "[upbit guard] " + reason}}))
    sys.exit(0)


def check(tool, inp):
    if tool in ("Read", "Edit", "Write", "NotebookEdit", "MultiEdit"):
        p = inp.get("file_path") or inp.get("notebook_path") or ""
        if SECRET_NAME.search(p):
            deny(f"{os.path.basename(p)} 는 비밀 파일입니다. 내용 확인은 'python deploy/env_check.py', "
                 "수정은 사용자가 직접 하도록 안내하세요.")
        if tool != "Read" and STATE_NAME.search(p):
            deny("bot_v5_state.json 은 실거래 봇 장부입니다. 직접 수정하지 말고 add_capital.py 또는 사용자 안내로 처리하세요.")
    elif tool == "Grep":
        p = (inp.get("path") or "") + " " + (inp.get("glob") or "")
        if re.search(SECRET_REF, p, re.I):
            deny(".env / .pem 내용 검색은 차단됩니다. 'python deploy/env_check.py' 를 사용하세요.")
    elif tool in ("Bash", "PowerShell"):
        cmd = inp.get("command") or ""
        if RUN_BOT.search(cmd):
            deny("PC 에서 bot_v5.py 를 실행하면 서버 봇과 같은 계좌로 중복 주문이 납니다. 봇은 서버(systemd)에서만 실행합니다. "
                 "로직 확인은 pytest 를 사용하세요.")
        if SHOW_SECRET.search(cmd) or WRITE_SECRET.search(cmd):
            deny(".env / .pem 내용을 출력하거나 고치는 명령은 차단됩니다. 값 점검은 'python deploy/env_check.py', "
                 "수정은 사용자가 직접 하도록 안내하세요. (scp 로 파일을 옮기는 것은 허용)")
        if STATE_WRITE.search(cmd):
            deny("bot_v5_state.json(봇 장부)을 덮어쓰거나 지우거나 옮기는 명령은 차단됩니다. "
                 "자금 변경은 add_capital.py, 모드 전환 등은 사용자에게 명령을 안내하세요.")


def main():
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    except ValueError:
        return
    check(data.get("tool_name", ""), data.get("tool_input") or {})


if __name__ == "__main__":
    main()
