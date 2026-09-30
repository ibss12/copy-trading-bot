"""Run the strategy live against Alpaca PAPER trading (never real money)."""

import sys
from datetime import date, timedelta

from lumibot.brokers import Alpaca
from lumibot.traders import Trader

import config
from strategies import SmaCrossover, SmartMoneySma


def main() -> None:
    config.validate()
    if not config.ALPACA_API_KEY or not config.ALPACA_API_SECRET:
        sys.exit("Set ALPACA_API_KEY and ALPACA_API_SECRET (paper keys) in stock-trading-bot/.env")
    if config.ALPACA_CONFIG["PAPER"] is not True:
        sys.exit("Refusing to run: this bot only trades on Alpaca paper accounts.")

    broker = Alpaca(config.ALPACA_CONFIG)
    parameters = {**config.strategy_parameters(), "sleeptime": config.LIVE_SLEEPTIME}

    if config.STRATEGY == "smart_money":
        since = date.today() - timedelta(days=config.SIGNAL_LOOKBACK_DAYS + 7)
        strategy = SmartMoneySma(broker=broker, parameters={**parameters, "signal_history_start": since.isoformat()})
    else:
        strategy = SmaCrossover(broker=broker, parameters=parameters)

    trader = Trader()
    trader.add_strategy(strategy)
    trader.run_all()


if __name__ == "__main__":
    main()
