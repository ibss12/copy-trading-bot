"""Plain-English "what should I do?" for each stock: BUY / SELL / HOLD / WATCH, with the reasons.

Mirrors the bot's rules (SMA trend + crossovers, confirmed by what the big traders you follow disclosed).
"""

CROSS_LOOKBACK_DAYS = 5


def _sma(values: list[float], window: int, end: int) -> float | None:
    if end < window:
        return None
    return sum(values[end - window : end]) / window


def recent_cross(closes: list[float], short: int, long: int, days: int = CROSS_LOOKBACK_DAYS) -> tuple[str, int] | None:
    """("up"|"down", sessions ago) for the latest SMA crossover within `days` sessions."""
    n = len(closes)
    for ago in range(days):
        end = n - ago
        s_now, l_now = _sma(closes, short, end), _sma(closes, long, end)
        s_prev, l_prev = _sma(closes, short, end - 1), _sma(closes, long, end - 1)
        if None in (s_now, l_now, s_prev, l_prev):
            return None
        if s_prev <= l_prev and s_now > l_now:
            return "up", ago
        if s_prev >= l_prev and s_now < l_now:
            return "down", ago
    return None


def advise(
    symbol: str,
    closes: list[float],
    short: int,
    long: int,
    score: int,
    traders: list[dict],
    held_qty: float,
) -> dict | None:
    """`traders`: recent big-trader filings for this stock ({"trader", "action", "disclosed_on"})."""
    s, lg = _sma(closes, short, len(closes)), _sma(closes, long, len(closes))
    if s is None or lg is None:
        return None
    trend_up = s > lg
    cross = recent_cross(closes, short, long)
    when = {0: "today", 1: "yesterday"}.get(cross[1], f"{cross[1]} sessions ago") if cross else ""
    reasons = [
        f"{short}-day average ${s:,.2f} is {'above' if trend_up else 'below'} the {long}-day ${lg:,.2f} "
        f"({'uptrend' if trend_up else 'downtrend'})."
    ]
    if cross:
        reasons.append(f"{'Golden cross (buy signal)' if cross[0] == 'up' else 'Death cross (sell signal)'} {when}.")
    buyers = sorted({t["trader"] for t in traders if t["action"] == "buy"})
    sellers = sorted({t["trader"] for t in traders if t["action"] == "sell"})
    if buyers:
        reasons.append("Bought recently (public filings): " + ", ".join(buyers) + ".")
    if sellers:
        reasons.append("Sold recently (public filings): " + ", ".join(sellers) + ".")

    owned = held_qty > 0
    if (cross and cross[0] == "down") or score < 0:
        action = "sell"
        why = "death cross" if cross and cross[0] == "down" else "big traders are net sellers"
        headline = f"Sell: {why}" if owned else f"Avoid: {why}"
    elif (score > 0 and trend_up) or (cross and cross[0] == "up" and score >= 0):
        action = "buy"
        why = "big traders buying + uptrend" if score > 0 else f"golden cross {when}"
        headline = f"Buy: {why}"
    elif trend_up:
        action = "hold"
        headline = "Hold: uptrend, no fresh buy signal" if owned else "Uptrend, wait for a fresh buy signal"
    else:
        action = "watch"
        headline = (
            "Watch: big traders buying but trend is still down" if score > 0 else "Watch: downtrend, stay out for now"
        )
    return {"symbol": symbol, "action": action, "headline": headline, "reasons": reasons, "owned": owned}
