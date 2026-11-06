"""Exit rules (stop-loss, trailing stop, take-profit, short-SMA exit) and the entry filter. No network.

Run: python -m unittest tests.test_rules -v
"""

import unittest
from types import SimpleNamespace

from strategies.rules import Holding, RiskRules, SmaSignal, exit_decision, sma_decision, smart_money_decision
from strategies.sma_crossover import SmaCrossover

RULES = RiskRules(stop_loss_pct=0.08, trailing_stop_pct=0.10, take_profit_pct=0.20, exit_below_short_sma=True)


def signal(price, short_sma=90.0, long_sma=80.0, crossed_up=False, crossed_down=False) -> SmaSignal:
    return SmaSignal(price, price, short_sma, long_sma, crossed_up, crossed_down)


def who(action: str) -> str:
    return "someone"


class ExitDecisionTest(unittest.TestCase):
    def test_stop_loss(self):
        decision = exit_decision(signal(92.0), Holding(100.0, 100.0), RULES)
        self.assertEqual(decision.action, "sell")
        self.assertIn("stop-loss", decision.reason)
        self.assertIsNone(exit_decision(signal(92.5), Holding(100.0, 100.0), RULES))

    def test_trailing_stop(self):
        decision = exit_decision(signal(103.5), Holding(100.0, 115.0), RULES)
        self.assertIn("trailing stop", decision.reason)
        self.assertIsNone(exit_decision(signal(104.0), Holding(100.0, 115.0), RULES))

    def test_take_profit(self):
        decision = exit_decision(signal(120.0), Holding(100.0, 120.0), RULES)
        self.assertIn("take-profit", decision.reason)
        self.assertIsNone(exit_decision(signal(119.0), Holding(100.0, 119.0), RULES))

    def test_exit_below_short_sma(self):
        decision = exit_decision(signal(95.0, short_sma=96.0), Holding(95.0, 95.0), RULES)
        self.assertIn("below short SMA", decision.reason)
        self.assertIn("below short SMA", exit_decision(signal(95.0, short_sma=96.0), None, RULES).reason)
        off = RiskRules(exit_below_short_sma=False)
        self.assertIsNone(exit_decision(signal(95.0, short_sma=96.0), Holding(95.0, 95.0), off))

    def test_zero_turns_pct_exits_off(self):
        off = RiskRules(stop_loss_pct=0, trailing_stop_pct=0, take_profit_pct=0, exit_below_short_sma=False)
        for price in (50.0, 200.0):
            self.assertIsNone(exit_decision(signal(price), Holding(100.0, 300.0), off))

    def test_unknown_entry_only_uses_short_sma(self):
        self.assertIsNone(exit_decision(signal(91.0), None, RULES))


class DecisionWithExitsTest(unittest.TestCase):
    def test_sma_decision_sells_on_exit_before_cross(self):
        decision = sma_decision(signal(91.0), True, True, rules=RULES, holding=Holding(100.0, 100.0))
        self.assertIn("stop-loss", decision.reason)
        self.assertIsNone(sma_decision(signal(99.0), True, True, rules=RULES, holding=Holding(100.0, 100.0)))

    def test_smart_money_sells_on_exit_even_when_traders_buy(self):
        decision = smart_money_decision(
            signal(121.0), True, True, 3, True, who, rules=RULES, holding=Holding(100.0, 121.0)
        )
        self.assertIn("take-profit", decision.reason)

    def test_exits_only_apply_to_held_positions(self):
        self.assertIsNone(sma_decision(signal(50.0), False, True, rules=RULES, holding=Holding(100.0, 100.0)))

    def test_no_buy_that_short_sma_exit_would_sell_at_once(self):
        below = signal(89.0, crossed_up=True)
        self.assertIsNone(sma_decision(below, False, True, rules=RULES))
        self.assertIsNone(smart_money_decision(below, False, True, 2, True, who, rules=RULES))
        off = RiskRules(exit_below_short_sma=False)
        self.assertEqual(sma_decision(below, False, True, rules=off).action, "buy")

    def test_require_price_above_long_sma(self):
        rules = RiskRules(exit_below_short_sma=False, require_price_above_long_sma=True)
        under_long = signal(79.0, short_sma=78.0, long_sma=80.0, crossed_up=True)
        self.assertIsNone(sma_decision(under_long, False, True, rules=rules))
        self.assertIsNone(smart_money_decision(under_long, False, True, 2, False, who, rules=rules))
        self.assertEqual(sma_decision(signal(95.0, crossed_up=True), False, True, rules=rules).action, "buy")
        self.assertEqual(sma_decision(under_long, False, True, rules=RiskRules()).action, "buy")


class HoldingTrackingTest(unittest.TestCase):
    def strategy(self, position=None):
        return SimpleNamespace(holdings={}, get_position=lambda symbol: position)

    def test_first_seen_price_then_peak(self):
        bot = self.strategy(SimpleNamespace(quantity=5, avg_fill_price=None))
        self.assertEqual(SmaCrossover.track_holding(bot, "AAPL", 100.0), Holding(100.0, 100.0))
        self.assertEqual(SmaCrossover.track_holding(bot, "AAPL", 110.0), Holding(100.0, 110.0))
        self.assertEqual(SmaCrossover.track_holding(bot, "AAPL", 104.0), Holding(100.0, 110.0))

    def test_uses_position_average_entry(self):
        bot = self.strategy(SimpleNamespace(quantity=5, avg_fill_price=95.0))
        self.assertEqual(SmaCrossover.track_holding(bot, "AAPL", 100.0), Holding(95.0, 100.0))

    def test_forgets_sold_positions(self):
        bot = self.strategy(None)
        bot.holdings["AAPL"] = Holding(100.0, 120.0)
        self.assertIsNone(SmaCrossover.track_holding(bot, "AAPL", 100.0))
        self.assertEqual(bot.holdings, {})

    def test_buy_fills_set_average_entry(self):
        bot = self.strategy()
        order = SimpleNamespace(asset=SimpleNamespace(symbol="AAPL"), is_buy_order=lambda: True)
        SmaCrossover.on_filled_order(bot, SimpleNamespace(quantity=10), order, 100.0, 10, 1)
        SmaCrossover.on_filled_order(bot, SimpleNamespace(quantity=20), order, 110.0, 10, 1)
        self.assertEqual(bot.holdings["AAPL"], Holding(105.0, 110.0))


if __name__ == "__main__":
    unittest.main()
