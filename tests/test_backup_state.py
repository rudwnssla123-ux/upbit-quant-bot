import json
import os
import tempfile
import unittest

from tests._stubs import install_pyupbit_stub

install_pyupbit_stub()

import backup_state as bs  # noqa: E402


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state.json")
        self.dir = os.path.join(self.tmp.name, "backups")

    def tearDown(self):
        self.tmp.cleanup()

    def write_state(self, text):
        with open(self.state, "w", encoding="utf-8") as f:
            f.write(text)

    def test_copies_state_unchanged(self):
        self.write_state(json.dumps({"mode": "LIVE", "cash": 1.5}))
        path = bs.snapshot(self.state, self.dir, today="2026-10-15")
        self.assertTrue(path.endswith("state_2026-10-15.json"))
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"mode": "LIVE", "cash": 1.5})
        with open(self.state, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["cash"], 1.5)          # 원본은 그대로

    def test_keeps_only_recent(self):
        self.write_state("{}")
        for d in range(1, 6):
            bs.snapshot(self.state, self.dir, today=f"2026-10-0{d}", keep=3)
        self.assertEqual(sorted(os.listdir(self.dir)),
                         ["state_2026-10-03.json", "state_2026-10-04.json", "state_2026-10-05.json"])

    def test_broken_state_does_not_overwrite_backup(self):
        self.write_state('{"cash": 1}')
        bs.snapshot(self.state, self.dir, today="2026-10-15")
        self.write_state('{"cash": ')                             # 쓰다 만 장부
        with self.assertRaises(ValueError):
            bs.snapshot(self.state, self.dir, today="2026-10-15")
        with open(os.path.join(self.dir, "state_2026-10-15.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"cash": 1})

    def test_telegram_weekly(self):
        import datetime as dt
        sun, mon = dt.date(2026, 10, 18), dt.date(2026, 10, 19)
        self.assertTrue(bs.should_send(sun, []))
        self.assertFalse(bs.should_send(mon, []))
        self.assertTrue(bs.should_send(mon, ["--send"]))
        self.assertFalse(bs.should_send(sun, ["--no-send"]))

    def test_no_telegram_without_token(self):
        self.assertFalse(bs.send_file(self.state))


if __name__ == "__main__":
    unittest.main()
