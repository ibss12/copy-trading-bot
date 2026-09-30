"""Alpaca PAPER account access (balances, positions, orders). Never connects to a live account."""

import logging

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

logger = logging.getLogger(__name__)


def _f(value) -> float:
    return float(value) if value not in (None, "") else 0.0


def _order_dict(order) -> dict:
    return {
        "id": str(order.id),
        "symbol": order.symbol,
        "side": str(order.side.value),
        "qty": _f(order.qty),
        "filled_qty": _f(order.filled_qty),
        "filled_avg_price": _f(order.filled_avg_price),
        "type": str(order.order_type.value) if order.order_type else "",
        "status": str(order.status.value),
        "submitted_at": order.submitted_at.isoformat() if order.submitted_at else None,
        "filled_at": order.filled_at.isoformat() if order.filled_at else None,
    }


class PaperAccount:
    def __init__(self, api_key: str, api_secret: str):
        self.enabled = bool(api_key and api_secret)
        self.error: str | None = None
        self._client = TradingClient(api_key, api_secret, paper=True) if self.enabled else None

    def snapshot(self) -> dict:
        if self._client is None:
            return {"enabled": False}
        try:
            account = self._client.get_account()
            positions = self._client.get_all_positions()
            open_orders = self._client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=100))
            recent = self._client.get_orders(GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=25))
        except Exception as exc:
            self.error = str(exc)
            logger.warning("Alpaca paper account request failed: %s", exc)
            return {"enabled": True, "error": self.error}
        self.error = None
        equity, last_equity = _f(account.equity), _f(account.last_equity)
        return {
            "enabled": True,
            "error": None,
            "equity": equity,
            "last_equity": last_equity,
            "day_pl": equity - last_equity,
            "day_pl_pct": (equity / last_equity - 1) * 100 if last_equity else 0.0,
            "cash": _f(account.cash),
            "buying_power": _f(account.buying_power),
            "positions": [
                {
                    "symbol": p.symbol,
                    "qty": _f(p.qty),
                    "avg_entry_price": _f(p.avg_entry_price),
                    "current_price": _f(p.current_price),
                    "market_value": _f(p.market_value),
                    "unrealized_pl": _f(p.unrealized_pl),
                    "unrealized_plpc": _f(p.unrealized_plpc) * 100,
                    "intraday_pl": _f(p.unrealized_intraday_pl),
                    "weight_pct": _f(p.market_value) / equity * 100 if equity else 0.0,
                }
                for p in positions
            ],
            "open_orders": [_order_dict(o) for o in open_orders],
            "recent_orders": [_order_dict(o) for o in recent],
        }

    def submit_market_order(self, symbol: str, qty: float, side: str) -> dict:
        if self._client is None:
            raise RuntimeError("Alpaca paper keys are not configured")
        request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        return _order_dict(self._client.submit_order(request))

    def cancel_order(self, order_id: str) -> None:
        if self._client is None:
            raise RuntimeError("Alpaca paper keys are not configured")
        self._client.cancel_order_by_id(order_id)
