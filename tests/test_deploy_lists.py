import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME = {"bot_v5.py", "strategy_v5.py", "upbit_market.py", "monthly_check.py", "add_capital.py", "backtest_v5.py",
           "backup_state.py"}
DEPLOY_DOCS = ("deploy/upload.ps1", "deploy/setup_server.sh", ".claude/skills/deploy/SKILL.md")


def local_imports(path):
    """프로젝트 루트의 .py 모듈 중 path 가 import 하는 것."""
    mods = {f[:-3] for f in os.listdir(ROOT) if f.endswith(".py")}
    with open(os.path.join(ROOT, path), encoding="utf-8") as fh:
        found = set(re.findall(r"^\s*(?:from|import)\s+(\w+)", fh.read(), re.M))
    return {m + ".py" for m in found & mods}


class TestDeployLists(unittest.TestCase):
    """서버에 올리는 파일 목록에 실행에 필요한 모듈이 빠지면 배포 후 봇이 시작하지 못한다."""

    def test_runtime_modules_are_listed(self):
        need = set(RUNTIME)
        for f in RUNTIME:
            need |= local_imports(f)
        for doc in DEPLOY_DOCS:
            with open(os.path.join(ROOT, doc), encoding="utf-8") as fh:
                text = fh.read()
            for f in sorted(need):
                with self.subTest(doc=doc, file=f):
                    self.assertIn(f, text)

    def test_server_scripts_use_lf(self):
        """서버(리눅스)에서 실행되는 파일에 윈도우 줄바꿈(CRLF)이 섞이면 bash/systemd 가 실패한다."""
        for f in ("deploy/setup_server.sh", "bot_v5.service"):
            with open(os.path.join(ROOT, f), "rb") as fh:
                with self.subTest(file=f):
                    self.assertNotIn(b"\r", fh.read())


if __name__ == "__main__":
    unittest.main()
