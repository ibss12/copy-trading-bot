"""Backtest the strategy on historical Yahoo Finance data and print performance stats."""

import argparse
from datetime import date, datetime, timedelta

from lumibot.backtesting import YahooDataBacktesting

import config
from strategies import SmaCrossover, SmartMoneySma


def _stat(value) -> str:
    if isinstance(value, dict):
        value = value.get("drawdown", next(iter(value.values()), None))
    return f"{value:.2%}" if isinstance(value, float) else str(value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=config.BACKTEST_START)
    parser.add_argument("--end", type=date.fromisoformat, default=config.BACKTEST_END)
    parser.add_argument("--budget", type=float, default=config.BACKTEST_BUDGET)
    parser.add_argument("--strategy", choices=["sma", "smart_money"], default=config.STRATEGY)
    parser.add_argument("--tearsheet", action="store_true", help="Save and open the HTML tearsheet / plots")
    args = parser.parse_args()
    config.validate()

    parameters = {**config.strategy_parameters(), "sleeptime": "1D"}
    strategy_class = SmaCrossover
    if args.strategy == "smart_money":
        strategy_class = SmartMoneySma
        since = args.start - timedelta(days=config.SIGNAL_LOOKBACK_DAYS)
        parameters["signal_history_start"] = since.isoformat()

    results = strategy_class.backtest(
        YahooDataBacktesting,
        datetime.combine(args.start, datetime.min.time()),
        datetime.combine(args.end, datetime.min.time()),
        budget=args.budget,
        benchmark_asset="SPY",
        parameters=parameters,
        show_plot=args.tearsheet,
        show_tearsheet=args.tearsheet,
        save_tearsheet=args.tearsheet,
        show_indicators=False,
    )

    print(f"\n=== {strategy_class.__name__} backtest {args.start} -> {args.end} (budget ${args.budget:,.0f}) ===")
    labels = {
        "total_return": "Total return",
        "cagr": "CAGR",
        "volatility": "Volatility",
        "sharpe": "Sharpe ratio",
        "max_drawdown": "Max drawdown",
        "romad": "Return / max drawdown",
    }
    for key, label in labels.items():
        if results and key in results:
            value = results[key]
            text = f"{value:.2f}" if key in ("sharpe", "romad") and isinstance(value, float) else _stat(value)
            print(f"{label:>22}: {text}")


if __name__ == "__main__":
    main()
