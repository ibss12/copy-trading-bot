"""Lumibot broker for a Trading 212 practice (demo) account.

* Orders go to Trading 212's demo server only (see `client.validate_base_url`).
* The strategy only "sees" the shares the bot bought (tracked in `BotLedger`), so its exits can
  never sell shares you bought yourself in the Trading 212 app or from the dashboard.
* Trading 212 has no order stream, so fills are detected by polling pending orders and order history.
* Market orders are never retried (the endpoint is not idempotent). If a request fails without a clear
  answer, the order is held as "uncertain" (blocking a second order for that stock) until it shows up
  in Trading 212 or 10 minutes pass.
"""

import logging
import time
import traceback
import uuid

import yfinance as yf
from lumibot.brokers import Broker
from lumibot.entities import Asset, Order, Position
from lumibot.trading_builtins import PollingStream

from .client import OrderUncertain, Trading212Client, Trading212Error, symbol_from_ticker
from .ledger import BotLedger, split_position
from .yahoo_live import YahooLiveData

logger = logging.getLogger(__name__)

UNCERTAIN_TIMEOUT_S = 600
T212_ACTIVE = {"LOCAL", "UNCONFIRMED", "CONFIRMED", "NEW", "CANCELLING", "PARTIALLY_FILLED", "REPLACING"}


def order_side(row: dict) -> str:
    if row.get("side"):
        return "sell" if str(row["side"]).upper() == "SELL" else "buy"
    return "sell" if float(row.get("quantity") or 0) < 0 else "buy"


def order_ticker(row: dict) -> str:
    return row.get("ticker") or (row.get("instrument") or {}).get("ticker") or ""


class Trading212Broker(Broker):
    NAME = "Trading212"
    POLL_EVENT = PollingStream.POLL_EVENT

    def __init__(
        self,
        client: Trading212Client,
        ledger: BotLedger,
        data_source=None,
        polling_interval: float = 20.0,
        connect_stream: bool = True,
    ):
        self.client = client
        self.ledger = ledger
        self.polling_interval = polling_interval
        self.currency = "USD"
        self._fx = (1.0, 0.0)
        self._tickers: dict[str, str] = {}
        self._uncertain: dict[str, tuple[float, str]] = {}
        self._waiting_logged: set[str] = set()
        self.stream = None
        super().__init__(
            name=self.NAME,
            connect_stream=connect_stream,
            data_source=data_source or YahooLiveData(),
            config={"MARKET": "NYSE"},
        )

    # ------------------------------------------------------------------ helpers
    def _ticker(self, symbol: str) -> str:
        if symbol not in self._tickers:
            ticker = self.client.ticker_for(symbol)
            if not ticker:
                raise Trading212Error(f"{symbol} is not available on Trading 212")
            self._tickers[symbol] = ticker
        return self._tickers[symbol]

    def _fx_to_usd(self, currency: str) -> float:
        if currency == "USD":
            return 1.0
        rate, fetched = self._fx
        if time.time() - fetched > 3600:
            try:
                rate = float(yf.Ticker(f"{currency}USD=X").fast_info["last_price"])
                self._fx = (rate, time.time())
            except Exception:
                logger.warning("Couldn't fetch %s->USD rate, using %.4f", currency, rate)
        return rate

    def held_totals(self) -> dict[str, float]:
        """Total shares per symbol in the account (yours + the bot's)."""
        totals: dict[str, float] = {}
        for row in self.client.positions():
            symbol = symbol_from_ticker(order_ticker(row))
            totals[symbol] = totals.get(symbol, 0.0) + float(row.get("quantity") or 0)
        return totals

    def bot_quantity(self, symbol: str) -> float:
        bot_count = self.ledger.bot_shares().get(symbol, 0.0)
        return split_position(self.held_totals().get(symbol, 0.0), bot_count)[0]

    # ------------------------------------------------------------------ balances / positions
    def _get_balances_at_broker(self, quote_asset: Asset, strategy):
        try:
            summary = self.client.account_summary()
        except Exception as exc:
            logger.error("Couldn't read Trading 212 account summary: %s", exc)
            return None
        self.currency = summary.get("currency") or "USD"
        fx = self._fx_to_usd(self.currency)
        cash = float((summary.get("cash") or {}).get("availableToTrade") or 0) * fx
        total = float(summary.get("totalValue") or 0) * fx
        return cash, total - cash, total

    def _pull_positions(self, strategy) -> list[Position]:
        try:
            totals = self.held_totals()
        except Exception as exc:
            logger.warning("Couldn't read Trading 212 positions, keeping last known: %s", exc)
            return list(self._filled_positions.get_list())
        bot = self.ledger.reconcile(totals)
        name = self._strategy_name_from_input(strategy) or ""
        return [Position(name, Asset(symbol), qty) for symbol, qty in bot.items() if qty > 0]

    def _pull_position(self, strategy, asset: Asset):
        for position in self._pull_positions(strategy):
            if position.asset.symbol == asset.symbol:
                return position
        return None

    def get_historical_account_value(self) -> dict:
        return {"hourly": None, "daily": None}

    # ------------------------------------------------------------------ orders
    def _submit_order(self, order: Order) -> Order:
        symbol = order.asset.symbol
        if order.asset.asset_type != Asset.AssetType.STOCK or order.order_type != Order.OrderType.MARKET:
            self._dispatch_error(order, "Only market orders for stocks are supported on Trading 212")
            return order
        try:
            ticker = self._ticker(symbol)
            quantity = float(order.quantity)
            if order.is_sell_order():
                owned = self.bot_quantity(symbol)
                if owned <= 0:
                    raise Trading212Error(f"the bot owns no {symbol} shares (your own shares are never sold)")
                quantity = min(quantity, owned)
                order.quantity = quantity
            response = self.client.market_order(ticker, quantity if order.is_buy_order() else -quantity)
        except OrderUncertain as exc:
            order.identifier = f"uncertain-{uuid.uuid4().hex[:10]}"
            order.status = Order.OrderStatus.SUBMITTED
            self._uncertain[order.identifier] = (time.time(), self._tickers.get(symbol, ""))
            self._unprocessed_orders.append(order)
            self._emit(self.NEW_ORDER, order=order)
            logger.warning(
                "Trading 212 did not confirm the %s %s order (%s). Not retrying; waiting to see if it went through.",
                order.side,
                symbol,
                exc,
            )
            return order
        except Exception as exc:
            self._dispatch_error(order, f"Trading 212 order failed for {symbol}: {exc}")
            return order

        order.identifier = str(response["id"])
        self.ledger.record_bot_order(order.identifier)
        order.status = Order.OrderStatus.SUBMITTED
        order.update_raw(response)
        self._unprocessed_orders.append(order)
        self._emit(self.NEW_ORDER, order=order)
        return order

    def _dispatch_error(self, order: Order, message: str) -> None:
        logger.error(message)
        if not order.identifier:
            order.identifier = f"rejected-{uuid.uuid4().hex[:10]}"
        self._unprocessed_orders.append(order)
        self._emit(self.ERROR_ORDER, order=order, error_msg=message)

    def cancel_order(self, order: Order) -> None:
        if order.identifier in self._uncertain:
            return
        self.client.cancel_order(int(order.identifier))

    def _modify_order(self, order: Order, limit_price=None, stop_price=None):
        raise NotImplementedError("Trading 212 market orders can't be modified")

    def _parse_broker_order(self, response: dict, strategy_name: str, strategy_object=None) -> Order:
        order = Order(
            strategy_name,
            Asset(symbol_from_ticker(order_ticker(response))),
            abs(float(response.get("quantity") or 0)),
            order_side(response),
            order_type=Order.OrderType.MARKET,
            identifier=str(response["id"]),
        )
        status = str(response.get("status", "NEW")).upper()
        order.status = {
            "FILLED": Order.OrderStatus.FILLED,
            "CANCELLED": Order.OrderStatus.CANCELED,
            "REJECTED": Order.OrderStatus.ERROR,
            "PARTIALLY_FILLED": Order.OrderStatus.PARTIALLY_FILLED,
        }.get(status, Order.OrderStatus.NEW)
        order.update_raw(response)
        return order

    def _pull_broker_order(self, identifier: str):
        if not str(identifier).isdigit():
            return None
        return self.client.order(int(identifier))

    def _pull_broker_all_orders(self) -> list[dict]:
        """Open orders the bot placed (orders you place yourself are ignored)."""
        return [row for row in self.client.pending_orders() if self.ledger.is_bot_order(row["id"])]

    # ------------------------------------------------------------------ polling
    def _final_state(self, order: Order) -> tuple[str, float, float] | None:
        """(status, filled quantity, average price) from order history, or None if not reported yet."""
        ticker = self._tickers.get(order.asset.symbol) or self._ticker(order.asset.symbol)
        matches = [
            item for item in self.client.history_orders(ticker=ticker) if str(item["order"]["id"]) == order.identifier
        ]
        if not matches:
            return None
        row = matches[0]["order"]
        status = str(row.get("status", "")).upper()
        if status in T212_ACTIVE:
            return None
        fills = [m.get("fill") for m in matches if m.get("fill")]
        filled = abs(float(row.get("filledQuantity") or sum(abs(float(f["quantity"])) for f in fills) or 0))
        if fills:
            price = sum(float(f["price"]) * abs(float(f["quantity"])) for f in fills) / max(
                sum(abs(float(f["quantity"])) for f in fills), 1e-9
            )
        else:
            price = abs(float(row.get("filledValue") or 0)) / filled if filled else 0.0
        return status, filled, price

    def _resolve_uncertain(self, pending: list[dict]) -> None:
        for order in self.get_tracked_orders():
            if order.identifier not in self._uncertain or not order.is_active():
                continue
            since, ticker = self._uncertain[order.identifier]
            known = self.ledger.snapshot()
            taken = set(known["bot_orders"]) | set(known["manual_orders"])
            candidates = [
                row
                for row in pending
                if order_ticker(row) == ticker and str(row["id"]) not in taken and row.get("initiatedFrom") == "API"
            ]
            if not candidates and ticker:
                candidates = [
                    item["order"]
                    for item in self.client.history_orders(ticker=ticker, limit=10)
                    if str(item["order"]["id"]) not in taken and item["order"].get("initiatedFrom") == "API"
                ]
            if candidates:
                new_id = str(candidates[0]["id"])
                del self._uncertain[order.identifier]
                order.identifier = new_id
                self.ledger.record_bot_order(new_id)
                logger.warning("The unconfirmed %s %s order did go through (id %s)", order.side, order.symbol, new_id)
            elif time.time() - since > UNCERTAIN_TIMEOUT_S:
                del self._uncertain[order.identifier]
                self._emit(self.ERROR_ORDER, order=order, error_msg=f"{order.side} {order.symbol} order was not placed")

    def do_polling(self) -> None:
        try:
            self.sync_positions(None)
            pending = self.client.pending_orders()
        except Exception as exc:
            logger.warning("Trading 212 poll skipped: %s", exc)
            return
        pending_ids = {str(row["id"]) for row in pending}
        self._resolve_uncertain(pending)
        for order in self.get_tracked_orders():
            if not order.is_active() or order.identifier in self._uncertain:
                continue
            if order.identifier in pending_ids:
                continue
            try:
                final = self._final_state(order)
            except Exception as exc:
                logger.warning("Couldn't read Trading 212 order history: %s", exc)
                return
            if final is None:
                if order.identifier not in self._waiting_logged:
                    self._waiting_logged.add(order.identifier)
                    logger.info("Waiting for Trading 212 to report the result of order %s", order.identifier)
                continue
            status, filled, price = final
            if filled > 0:
                signed = filled if order.is_buy_order() else -filled
                self.ledger.apply_fill(order.identifier, order.asset.symbol, signed)
                self._emit(self.FILLED_ORDER, order=order, price=price, filled_quantity=filled)
            elif status == "CANCELLED":
                self._emit(self.CANCELED_ORDER, order=order)
            else:
                self._emit(self.ERROR_ORDER, order=order, error_msg=f"Trading 212 status {status}")

    # ------------------------------------------------------------------ stream
    def _get_stream_object(self):
        return PollingStream(self.polling_interval)

    def _register_stream_events(self):
        broker = self

        @broker.stream.add_action(broker.POLL_EVENT)
        def on_poll():
            broker.do_polling()

        @broker.stream.add_action(broker.NEW_ORDER)
        def on_new(order):
            broker._safe_process(order, broker.NEW_ORDER)

        @broker.stream.add_action(broker.FILLED_ORDER)
        def on_fill(order, price, filled_quantity):
            broker._safe_process(order, broker.FILLED_ORDER, price=price, filled_quantity=filled_quantity)

        @broker.stream.add_action(broker.CANCELED_ORDER)
        def on_cancel(order):
            broker._safe_process(order, broker.CANCELED_ORDER)

        @broker.stream.add_action(broker.ERROR_ORDER)
        def on_error(order, error_msg):
            broker._on_error(order, error_msg)

    def _on_error(self, order: Order, error_msg: str) -> None:
        if order.is_active():
            self._safe_process(order, self.ERROR_ORDER, error=error_msg)
        order.set_error(error_msg)

    def _emit(self, event: str, **payload) -> None:
        if self.stream is not None:
            self.stream.dispatch(event, **payload)
        elif event == self.ERROR_ORDER:
            self._on_error(payload["order"], payload["error_msg"])
        else:
            self._safe_process(payload.pop("order"), event, **payload)

    def _safe_process(self, order: Order, event: str, **kwargs) -> None:
        try:
            self._process_trade_event(order, event, **kwargs)
        except Exception:
            logger.error(traceback.format_exc())

    def _run_stream(self):
        self._stream_established()
        self.stream._run()
