import importlib.util
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUARD = os.path.join(ROOT, ".claude", "hooks", "guard.py")


def run(tool, **inp):
    out = subprocess.run([sys.executable, GUARD], input=json.dumps({"tool_name": tool, "tool_input": inp}).encode(),
                         capture_output=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"] if out.stdout.strip() else "pass"


class TestGuard(unittest.TestCase):
    def test_module_imports(self):
        spec = importlib.util.spec_from_file_location("guard", GUARD)
        spec.loader.exec_module(importlib.util.module_from_spec(spec))

    def test_blocks(self):
        cases = [
            ("Bash", {"command": "python bot_v5.py"}),
            ("PowerShell", {"command": "py -u .\\bot_v5.py"}),
            ("Bash", {"command": "cd x && python3 /c/proj/bot_v5.py &"}),
            ("Bash", {"command": "cat .env"}),
            ("Bash", {"command": "grep TOKEN .env"}),
            ("PowerShell", {"command": "Get-Content .env"}),
            ("PowerShell", {"command": "ssh -i k u@h \"cat ~/bot/.env\""}),
            ("Bash", {"command": "type C:\\keys\\lightsail.pem"}),
            ("Bash", {"command": "echo X=1 >> .env"}),
            ("Bash", {"command": "rm bot_v5_state.json"}),
            ("PowerShell", {"command": "ssh h \"mv ~/bot/bot_v5_state.json ~/x.json\""}),
            ("Bash", {"command": "sed -i s/a/b/ bot_v5_state.json"}),
            ("Bash", {"command": "echo {} > bot_v5_state.json"}),
            ("Read", {"file_path": "C:\\proj\\.env"}),
            ("Read", {"file_path": "C:/keys/lightsail.pem"}),
            ("Edit", {"file_path": "/home/ubuntu/bot/.env"}),
            ("Write", {"file_path": "bot_v5_state.json"}),
            ("Grep", {"pattern": "TOKEN", "path": ".env"}),
        ]
        for tool, inp in cases:
            with self.subTest(tool=tool, inp=inp):
                self.assertEqual(run(tool, **inp), "deny")

    def test_allows(self):
        cases = [
            ("Bash", {"command": "python -m pytest -q tests/test_bot_v5.py"}),
            ("Bash", {"command": "python -m ruff check bot_v5.py"}),
            ("Bash", {"command": "python deploy/env_check.py"}),
            ("Bash", {"command": "cat .env.example"}),
            ("PowerShell", {"command": "scp -i C:\\keys\\lightsail.pem .env ubuntu@h:~/bot/.env"}),
            ("PowerShell", {"command": "ssh -i C:\\keys\\lightsail.pem h \"bash ~/bot/setup_server.sh < /dev/null\""}),
            ("PowerShell", {"command": "ssh h \"python3 -c 'import json;print(json.load(open(\\\"bot_v5_state.json\\\"))[\\\"mode\\\"])'\""}),
            ("PowerShell", {"command": "ssh h \"cd ~/bot && venv/bin/python add_capital.py 300000\""}),
            ("Bash", {"command": "cp bot_v5_state.json backup.json"}),
            ("Bash", {"command": "sed -i 's/a/b/' README.md && python -m pytest -q"}),
            ("Bash", {"command": "sed -i 's/add_capital.py requirements.txt/bot_v5.py x/' SKILL.md"}),
            ("Read", {"file_path": "C:\\proj\\bot_v5.py"}),
            ("Read", {"file_path": "C:\\proj\\.env.example"}),
            ("Read", {"file_path": "bot_v5_state.json"}),
            ("Edit", {"file_path": "C:\\proj\\strategy_v5.py"}),
            ("Grep", {"pattern": "def ", "path": "bot_v5.py"}),
        ]
        for tool, inp in cases:
            with self.subTest(tool=tool, inp=inp):
                self.assertEqual(run(tool, **inp), "pass")


if __name__ == "__main__":
    unittest.main()
