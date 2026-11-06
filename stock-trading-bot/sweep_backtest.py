"""Backtest a grid of strategy settings, rank them, and re-check the winner on a held-out period.

Example:
    python sweep_backtest.py --top 5
    python sweep_backtest.py --strategy smart_money --start 2023-01-01 --end 2024-12-31
"""

import argparse
import csv
import itertools
import math
import os
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from multiprocessing import get_context
from pathlib import Path

import config

METRICS = ("total_return", "cagr", "sharpe", "max_drawdown", "romad")
GRID_ARGS = {
    "sma_short_window": ("short-windows", int, "10,15,20"),
    "sma_long_window": ("long-windows", int, "30,50,100"),
    "stop_loss_pct": ("stop-losses", "pct", "0.05,0.08,0.12"),
    "trailing_stop_pct": ("trailing-stops", "pct", "0.08,0.10,0.15"),
    "take_profit_pct": ("take-profits", "pct", "0.15,0.20,0.30,none"),
    "exit_below_short_sma": ("exit-below-short-sma", "bool", "true,false"),
}


def _parse(kind, raw: str):
    raw = raw.strip().lower()
    if kind == "bool":
        return raw in ("1", "true", "yes", "on")
    if kind == "pct":
        return None if raw in ("none", "off", "0") else float(raw)
    return kind(raw)


def grid(values: dict[str, list]) -> list[dict]:
    names = list(values)
    combos = [dict(zip(names, combo)) for combo in itertools.product(*values.values())]
    return [c for c in combos if c.get("sma_short_window", 0) < c.get("sma_long_window", math.inf)]


def _metric(results: dict, key: str) -> float | None:
    value = (results or {}).get(key)
    if isinstance(value, dict):
        value = value.get("drawdown")
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(value) else value


def _silence_process() -> None:
    """Send this worker's output (Lumibot logs, progress bars) to /dev/null."""
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)


def run_one(strategy: str, overrides: dict, start: date, end: date, budget: float) -> dict:
    """One quiet backtest. Runs in a throwaway worker process (see `run_many`)."""
    os.environ["BACKTESTING_QUIET_LOGS"] = "true"
    os.environ["SHOW_PLOT"] = "False"
    _silence_process()
    from lumibot.backtesting import YahooDataBacktesting

    from strategies import SmaCrossover, SmartMoneySma

    parameters = {**config.strategy_parameters(), "sleeptime": "1D", **overrides}
    strategy_class = SmaCrossover
    if strategy == "smart_money":
        strategy_class = SmartMoneySma
        since = start - timedelta(days=config.SIGNAL_LOOKBACK_DAYS)
        parameters["signal_history_start"] = since.isoformat()
    with tempfile.TemporaryDirectory(prefix="sweep-") as tmp:
        os.chdir(tmp)  # Lumibot writes its per-run logs/ files relative to the working directory
        try:
            results = strategy_class.backtest(
                YahooDataBacktesting,
                datetime.combine(start, datetime.min.time()),
                datetime.combine(end, datetime.min.time()),
                budget=budget,
                benchmark_asset="SPY",
                parameters=parameters,
                show_plot=False,
                show_tearsheet=False,
                save_tearsheet=False,
                show_indicators=False,
                show_progress_bar=False,
                quiet_logs=True,
                save_stats_file=False,
            )
        except Exception as exc:  # one bad combination shouldn't stop the sweep
            return {"error": f"{exc.__class__.__name__}: {exc}"}
        finally:
            os.chdir(config.BASE_DIR)
    return {key: _metric(results, key) for key in METRICS}


def rank_key(row: dict) -> tuple[float, float]:
    def num(value):
        return value if isinstance(value, float) else -math.inf

    return (num(row.get("sharpe")), num(row.get("romad")))


def _fmt(key: str, value) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if key in ("sharpe", "romad"):
        return f"{value:.2f}"
    if key in METRICS or key.endswith("_pct"):
        return f"{value:.1%}" if isinstance(value, float) else "off"
    return str(value)


SHORT_NAMES = {
    "sma_short_window": "short",
    "sma_long_window": "long",
    "stop_loss_pct": "stop",
    "trailing_stop_pct": "trail",
    "take_profit_pct": "take",
    "exit_below_short_sma": "<shortSMA",
}


def print_table(rows: list[dict], columns: list[str]) -> None:
    headers = ["#"] + [SHORT_NAMES.get(c, c) for c in columns]
    body = [[str(i)] + [_fmt(c, row.get(c)) for c in columns] for i, row in enumerate(rows, 1)]
    widths = [max(len(r[i]) for r in [headers] + body) for i in range(len(headers))]
    for line in [headers] + body:
        print("  ".join(cell.rjust(width) for cell, width in zip(line, widths)))


def run_many(jobs: list[tuple[str, dict, date, date, float]], workers: int, label: str) -> list[dict]:
    rows: list[dict] = [{} for _ in jobs]
    # A fresh process per backtest: Lumibot keeps global state between runs.
    with ProcessPoolExecutor(workers, mp_context=get_context("spawn"), max_tasks_per_child=1) as pool:
        futures = {pool.submit(run_one, *job): i for i, job in enumerate(jobs)}
        for done, future in enumerate(as_completed(futures), 1):
            i = futures[future]
            try:
                rows[i] = future.result()
            except Exception as exc:
                rows[i] = {"error": f"{exc.__class__.__name__}: {exc}"}
            if done % max(1, len(jobs) // 20) == 0 or done == len(jobs):
                print(f"  {label}: {done}/{len(jobs)} backtests done", flush=True)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--strategy", choices=["sma", "smart_money"], default="sma")
    parser.add_argument("--start", type=date.fromisoformat, default=config.BACKTEST_START)
    parser.add_argument("--end", type=date.fromisoformat, default=config.BACKTEST_END)
    parser.add_argument(
        "--validate-start", type=date.fromisoformat, help="Held-out period (default: the year after --end)"
    )
    parser.add_argument("--validate-end", type=date.fromisoformat)
    parser.add_argument("--budget", type=float, default=config.BACKTEST_BUDGET)
    parser.add_argument("--top", type=int, default=10, help="How many of the best settings to print")
    parser.add_argument("--workers", type=int, default=max(1, min(8, (os.cpu_count() or 2) - 1)))
    parser.add_argument("--csv", type=Path, help="Results file (default: logs/sweep_<strategy>_<start>_<end>.csv)")
    for name, (flag, _, default) in GRID_ARGS.items():
        parser.add_argument(f"--{flag}", default=default, help=f"Comma-separated values for {name} (default {default})")
    args = parser.parse_args()
    config.validate()

    validate_start = args.validate_start or args.end + timedelta(days=1)
    validate_end = args.validate_end or min(date.today(), validate_start + timedelta(days=365))
    if validate_end <= validate_start:
        parser.error("Held-out period is empty: pass --validate-start/--validate-end")
    if validate_start <= args.end and validate_end >= args.start:
        print("Warning: the held-out period overlaps the sweep period, so it is not truly out-of-sample.")

    values = {
        name: [_parse(kind, v) for v in getattr(args, flag.replace("-", "_")).split(",")]
        for name, (flag, kind, _) in GRID_ARGS.items()
    }
    combos = grid(values)
    columns = list(GRID_ARGS)
    print(
        f"Sweeping {len(combos)} settings for {args.strategy} on {args.start} -> {args.end} "
        f"with {args.workers} workers..."
    )
    jobs = [(args.strategy, combo, args.start, args.end, args.budget) for combo in combos]
    rows = [{**combo, **metrics} for combo, metrics in zip(combos, run_many(jobs, args.workers, "sweep"))]
    failed = [r for r in rows if r.get("error")]
    ranked = sorted((r for r in rows if not r.get("error")), key=rank_key, reverse=True)

    out = args.csv or config.BASE_DIR / "logs" / f"sweep_{args.strategy}_{args.start}_{args.end}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        writer = csv.DictWriter(f, ["rank", "strategy", *columns, *METRICS, "error"])
        writer.writeheader()
        for rank, row in enumerate(ranked + failed, 1):
            writer.writerow({"rank": rank if not row.get("error") else "", "strategy": args.strategy, **row})

    print(f"\n=== Top {min(args.top, len(ranked))} of {len(ranked)} by Sharpe (tiebreak return/max drawdown) ===")
    print_table(ranked[: args.top], columns + list(METRICS))
    if failed:
        print(f"\n{len(failed)} runs failed, e.g. {failed[0]['error']}")
    print(f"\nAll results: {out}")
    if not ranked:
        return

    best = {name: ranked[0][name] for name in columns}
    current = {name: config.strategy_parameters()[name] for name in columns}
    print(
        f"\nRe-running the winner and your current settings on the held-out period {validate_start} -> {validate_end}..."
    )
    periods = [(args.start, args.end, "sweep"), (validate_start, validate_end, "held-out")]
    checks = run_many(
        [(args.strategy, best, validate_start, validate_end, args.budget)]
        + [(args.strategy, current, start, end, args.budget) for start, end, _ in periods],
        min(3, args.workers),
        "held-out",
    )
    table = [
        {"settings": "best", "period": f"sweep {args.start}..{args.end}", **ranked[0]},
        {"settings": "best", "period": f"held-out {validate_start}..{validate_end}", **best, **checks[0]},
    ] + [
        {"settings": "current", "period": f"{label} {start}..{end}", **current, **result}
        for (start, end, label), result in zip(periods, checks[1:])
    ]
    print("\n=== Best vs current settings: sweep period vs held-out period (watch for overfitting) ===")
    print_table(table, ["settings", "period", *columns, *METRICS])
    for row in table:
        if row.get("error"):
            print(f"{row['settings']} / {row['period']} failed: {row['error']}")


if __name__ == "__main__":
    main()
