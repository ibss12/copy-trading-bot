"""Print recent buys/sells disclosed by the people you follow (no trading)."""

import argparse
import logging
from datetime import date, timedelta

import config
from signals import load_signal_feed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=config.SIGNAL_LOOKBACK_DAYS, help="How far back to look")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    today = date.today()
    feed = load_signal_feed(config.WATCHLIST, today - timedelta(days=args.days))
    signals = feed.active(today, args.days)
    print(f"\n=== Smart-money moves disclosed in the last {args.days} days ({len(signals)}) ===")
    for signal in reversed(signals):
        print(f"- {signal.describe()}")

    scores = feed.net_scores(today, args.days)
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    print("\nNet score (buyers - sellers):", ", ".join(f"{s} {v:+d}" for s, v in ranked) or "none")


if __name__ == "__main__":
    main()
