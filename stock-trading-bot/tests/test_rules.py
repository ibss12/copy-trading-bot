"""Exit stack and entry filters in strategies/rules.py, plus their wiring. No network.

Run: python -m unittest tests.test_rules -v
"""

import unittest
from types import SimpleNamespace
from unittest import mock

import config
from strategies.rules import (
    NO_RULES,
    PositionState,
    SmaSignal,
    TradeRules,
    entry_blocked,
    exit_decision,
    rsi,
    sma_decision,
    smart_money_decision,
)

RULES = TradeRules(
    stop_loss_pct=0.08,
    trailing_stop_pct=0.10,
    take_profit_pct=0.20,
    exit_below_short_sma=True,
    require_price_above_long_sma=False,
    rsi_max_for_entry=None,
)


def signal(price=100.0, short_sma=95.0, long_sma=90.0, crossed_up=False, crossed_down=False, rsi_value=None):
    return SmaSignal(price, price, short_sma, long_sma, crossed_up, crossed_down, rsi_value)


def nobody(action: str) -> str:
    return ""


class ExitDecisionTest(unittest.TestCase):
    def test_stop_loss(self):
        decision = exit_decision(signal(92.0, short_sma=80), PositionState(100, 100), RULES)
        self.assertEqual(decision.action, "sell")
        self.assertIn("stop-loss", decision.reason)
        self.assertIsNone(exit_decision(signal(92.1, short_sma=80), PositionState(100, 100), RULES))

    def test_trailing_stop(self):
        decision = exit_decision(signal(108.0, short_sma=100), PositionState(100, 120), RULES)
        self.assertIn("trailing stop", decision.reason)
        self.assertIn("peak 120.00", decision.reason)
        self.assertIsNone(exit_decision(signal(108.1, short_sma=100), PositionState(100, 120), RULES))

    def test_trailing_stop_uses_todays_price_as_peak(self):
        self.assertIsNone(exit_decision(signal(130.0, short_sma=100), PositionState(120, 120), RULES))

    def test_take_profit(self):
        decision = exit_decision(signal(120.0), PositionState(100, 120), RULES)
        self.assertIn("take-profit", decision.reason)
        self.assertIsNone(exit_decision(signal(119.9), PositionState(100, 120), RULES))

    def test_below_short_sma(self):
        decision = exit_decision(signal(99.0, short_sma=100), PositionState(99, 99), RULES)
        self.assertIn("below short SMA", decision.reason)
        rules = TradeRules(None, None, None, exit_below_short_sma=False)
        self.assertIsNone(exit_decision(signal(99.0, short_sma=100), PositionState(99, 99), rules))

    def test_below_short_sma_works_without_entry_price(self):
        self.assertIn("below short SMA", exit_decision(signal(99.0, short_sma=100), None, RULES).reason)

    def test_disabled_rules_never_sell(self):
        self.assertIsNone(exit_decision(signal(10.0, short_sma=100), PositionState(100, 200), NO_RULES))

    def test_exit_runs_before_sma_cross_in_both_strategies(self):
        crash = signal(90.0, short_sma=80, crossed_down=True)
        position = PositionState(100, 100)
        self.assertIn("stop-loss", sma_decision(crash, True, True, position, RULES).reason)
        decision = smart_money_decision(crash, True, True, 1, True, nobody, position, RULES)
        self.assertIn("stop-loss", decision.reason)

    def test_old_behaviour_without_rules(self):
        self.assertIsNone(sma_decision(signal(10.0, short_sma=100), True, True))
        self.assertIn("crossed below", sma_decision(signal(crossed_down=True), True, True).reason)


class EntryFilterTest(unittest.TestCase):
    def test_exit_below_short_sma_blocks_new_buys(self):
        below = signal(price=94.0, short_sma=95.0, long_sma=90.0, crossed_up=True)
        rules = TradeRules()
        self.assertEqual(entry_blocked(below, rules), "price 94.00 below short SMA 95.00")
        self.assertIsNone(sma_decision(below, False, True, None, rules))
        self.assertIsNone(smart_money_decision(below, False, True, 1, True, nobody, None, rules))

    def test_exit_below_short_sma_does_not_block_when_disabled(self):
        below = signal(price=94.0, short_sma=95.0, long_sma=90.0, crossed_up=True)
        rules = TradeRules(exit_below_short_sma=False)
        self.assertIsNone(entry_blocked(below, rules))
        self.assertEqual(sma_decision(below, False, True, None, rules).action, "buy")
        self.assertEqual(sma_decision(below, False, True).action, "buy")

    def test_rsi_filter_blocks_overbought_buys(self):
        rules = TradeRules(rsi_max_for_entry=70)
        hot = signal(crossed_up=True, rsi_value=75.0)
        self.assertIn("RSI 75", entry_blocked(hot, rules))
        self.assertIsNone(sma_decision(hot, False, True, None, rules))
        self.assertIsNone(smart_money_decision(hot, False, True, 2, True, nobody, None, rules))
        calm = signal(crossed_up=True, rsi_value=65.0)
        self.assertEqual(sma_decision(calm, False, True, None, rules).action, "buy")

    def test_no_rsi_filter_by_default(self):
        self.assertEqual(sma_decision(signal(crossed_up=True, rsi_value=95.0), False, True, None, RULES).action, "buy")

    def test_require_price_above_long_sma(self):
        rules = TradeRules(require_price_above_long_sma=True)
        below = signal(price=95.0, short_sma=94, long_sma=96, crossed_up=True)
        self.assertIn("not above long SMA", entry_blocked(below, rules))
        self.assertIsNone(sma_decision(below, False, True, None, rules))
        self.assertEqual(sma_decision(below, False, True, None, TradeRules()).action, "buy")

    def test_rsi_values(self):
        self.assertIsNone(rsi([1.0] * 14))
        self.assertEqual(rsi([float(i) for i in range(1, 20)]), 100.0)
        self.assertEqual(rsi([float(i) for i in range(20, 1, -1)]), 0.0)
        self.assertAlmostEqual(rsi([10.0, 11.0] * 10), 50.0, delta=5)

    def test_signal_carries_rsi(self):
        rising = SmaSignal.from_closes([float(i) for i in range(1, 30)], 5, 10)
        self.assertEqual(rising.rsi, 100.0)
        self.assertIsNone(SmaSignal.from_closes([1.0] * 11, 5, 10).rsi)


class StrategyTrackingTest(unittest.TestCase):
    """`SmaCrossover.position_state` on a stand-in object (no broker needed)."""

    def make(self, quantity, avg_fill_price=None):
        from strategies.sma_crossover import SmaCrossover

        position = SimpleNamespace(quantity=quantity, avg_fill_price=avg_fill_price) if quantity else None
        bot = SimpleNamespace(position_states={}, get_position=lambda symbol: position)
        return bot, lambda price: SmaCrossover.position_state(bot, "AAPL", price)

    def test_entry_from_broker_average_then_peak_tracks(self):
        bot, track = self.make(5, avg_fill_price=100.0)
        self.assertEqual(track(110.0), PositionState(100.0, 110.0))
        self.assertEqual(track(105.0), PositionState(100.0, 110.0))

    def test_first_seen_price_without_average(self):
        _, track = self.make(5)
        self.assertEqual(track(50.0), PositionState(50.0, 50.0))

    def test_cleared_when_not_held(self):
        bot, track = self.make(0)
        bot.position_states["AAPL"] = PositionState(1, 1)
        self.assertIsNone(track(50.0))
        self.assertEqual(bot.position_states, {})


class ConfigTest(unittest.TestCase):
    def test_parameters_include_rules(self):
        params = config.strategy_parameters()
        for name in TradeRules.__dataclass_fields__:
            self.assertIn(name, params)
        self.assertEqual(TradeRules.from_parameters(params), config.trade_rules())

    def test_optional_float(self):
        with mock.patch.dict("os.environ", {"X": "off", "Y": "0", "Z": "0.05", "W": ""}):
            self.assertIsNone(config._optional_float("X", 0.1))
            self.assertIsNone(config._optional_float("Y", 0.1))
            self.assertEqual(config._optional_float("Z", 0.1), 0.05)
            self.assertEqual(config._optional_float("W", 0.1), 0.1)

    def test_validate_rejects_bad_values(self):
        for name, value in (
            ("STOP_LOSS_PCT", 1.5),
            ("TRAILING_STOP_PCT", -0.1),
            ("TAKE_PROFIT_PCT", -0.2),
            ("RSI_MAX_FOR_ENTRY", 150.0),
        ):
            with self.subTest(name=name), mock.patch.object(config, name, value):
                with self.assertRaisesRegex(ValueError, name):
                    config.validate()


if __name__ == "__main__":
    unittest.main()
