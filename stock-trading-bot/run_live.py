"""Run the strategy live on Alpaca PAPER trading or a Trading 212 PRACTICE account (never real money)."""

import sys
from datetime import date, timedelta

from lumibot.traders import Trader

import config
from strategies import SmaCrossover, SmartMoneySma


def make_broker():
    if config.BROKER == "trading212":
        from trading212.broker import Trading212Broker
        from trading212.client import validate_base_url
        from trading212.practice import bot_ledger, practice_client

        if not (config.TRADING212_API_KEY and config.TRADING212_API_SECRET):
            sys.exit("Set TRADING212_API_KEY and TRADING212_API_SECRET (practice account) in stock-trading-bot/.env")
        try:
            validate_base_url(config.TRADING212_BASE_URL)
        except ValueError as exc:
            sys.exit(str(exc))
        print("Trading on your Trading 212 PRACTICE account. The bot only ever sells shares it bought itself.")
        return Trading212Broker(practice_client(), bot_ledger())

    from lumibot.brokers import Alpaca

    if not config.ALPACA_API_KEY or not config.ALPACA_API_SECRET:
        sys.exit("Set ALPACA_API_KEY and ALPACA_API_SECRET (paper keys) in stock-trading-bot/.env")
    if config.ALPACA_CONFIG["PAPER"] is not True:
        sys.exit("Refusing to run: this bot only trades on Alpaca paper accounts.")
    return Alpaca(config.ALPACA_CONFIG)


def main() -> None:
    config.validate()
    broker = make_broker()
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
