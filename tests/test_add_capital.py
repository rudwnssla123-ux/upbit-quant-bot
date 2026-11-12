import os
import unittest

from tests._stubs import install_pyupbit_stub

install_pyupbit_stub()
os.environ["DRY_RUN"] = "1"

import add_capital  # noqa: E402
from strategy_v5 import drawdown  # noqa: E402


class TestFlow(unittest.TestCase):
    def state(self):
        return {"cash": 200_000.0, "positions": {}, "eq_hist": [700_000.0, 800_000.0],
                "eq_log": [["2026-10-06", 700_000], ["2026-10-07", 800_000]], "start_capital": 700_000.0}

    def test_deposit_keeps_returns_and_drawdown(self):
        st = self.state()
        eq = 720_000.0
        dd0, r0 = drawdown(st["eq_hist"], eq, 180), eq / st["start_capital"] - 1
        add_capital.apply_flow(st, 280_000, eq)
        eq2 = eq + 280_000
        self.assertEqual(st["cash"], 480_000)
        self.assertAlmostEqual(drawdown(st["eq_hist"], eq2, 180), dd0)
        self.assertAlmostEqual(eq2 / st["start_capital"] - 1, r0)
        self.assertEqual(st["flows"][-1][1], 280_000)

    def test_target_amount(self):
        self.assertEqual(add_capital.amount_for_target(5_000_000, 702_345.6), 4_297_654)
        self.assertEqual(add_capital.amount_for_target(5_000_000, 5_100_000), -100_000)

    def test_withdraw_limited_to_cash(self):
        with self.assertRaises(ValueError):
            add_capital.apply_flow(self.state(), -300_000, 720_000)


if __name__ == "__main__":
    unittest.main()
