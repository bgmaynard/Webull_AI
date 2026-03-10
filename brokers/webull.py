"""Webull broker connector — Uses webull-python unofficial API.

Currently blocked by Webull (403). Kept as reference for when/if access returns.
"""

import asyncio
import logging

from core.interfaces import BrokerInterface
from core.models import AccountInfo, OrderResult, OrderSide, OrderStatus, OrderType, Position
from webull_auth import get_auth

logger = logging.getLogger(__name__)


class WebullBroker(BrokerInterface):
    def __init__(self):
        self._auth = get_auth()

    @property
    def _wb(self):
        return self._auth.client

    async def get_account(self) -> AccountInfo:
        try:
            data = await asyncio.to_thread(self._wb.get_account)
            if not data:
                return AccountInfo()
            return AccountInfo(
                account_id=str(data.get("secAccountId", "")),
                net_liquidation=float(data.get("netLiquidation", 0)),
                buying_power=float(data.get("dayBuyingPower", 0)),
                cash=float(data.get("totalCash", 0)),
                day_trades_remaining=int(data.get("dayTradingRemaining", 0)),
                account_type=self._auth.account_type,
            )
        except Exception:
            logger.exception("Failed to get account info")
            return AccountInfo()

    async def get_positions(self) -> list[Position]:
        try:
            data = await asyncio.to_thread(self._wb.get_positions)
            if not data:
                return []
            return [
                Position(
                    symbol=p.get("ticker", {}).get("symbol", "???"),
                    qty=float(p.get("position", 0)),
                    avg_cost=float(p.get("costPrice", 0)),
                    market_value=float(p.get("marketValue", 0)),
                    unrealized_pnl=float(p.get("unrealizedProfitLoss", 0)),
                )
                for p in data
            ]
        except Exception:
            logger.exception("Failed to get positions")
            return []

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        qty: int,
        order_type: OrderType = OrderType.LIMIT,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> OrderResult:
        try:
            if order_type == OrderType.LIMIT and limit_price is None:
                return OrderResult(success=False, message="Limit price required")

            action = side.value

            # Map generic OrderType to Webull-specific strings
            wb_order_types = {
                OrderType.LIMIT: "LMT",
                OrderType.MARKET: "MKT",
                OrderType.STOP: "STP",
                OrderType.STOP_LIMIT: "STP LMT",
            }

            def _place():
                kwargs = {
                    "stock": symbol,
                    "action": action,
                    "orderType": wb_order_types[order_type],
                    "enforce": "GTC",
                    "quant": qty,
                }
                if limit_price is not None:
                    kwargs["price"] = limit_price
                if stop_price is not None:
                    kwargs["stpPrice"] = stop_price
                return self._wb.place_order(**kwargs)

            result = await asyncio.to_thread(_place)
            if result and isinstance(result, dict):
                if result.get("success") or "orderId" in result:
                    order_id = str(result.get("orderId", result.get("data", {}).get("orderId", "")))
                    logger.info("Order placed: %s %s %s @ %s (id=%s)", action, qty, symbol, limit_price, order_id)
                    return OrderResult(success=True, order_id=order_id)
                else:
                    msg = result.get("msg", str(result))
                    logger.warning("Order rejected: %s", msg)
                    return OrderResult(success=False, message=msg)
            return OrderResult(success=False, message="Unexpected response")
        except Exception as e:
            logger.exception("Order placement error")
            return OrderResult(success=False, message=str(e))

    async def cancel_order(self, order_id: str) -> bool:
        try:
            result = await asyncio.to_thread(self._wb.cancel_order, order_id)
            return bool(result)
        except Exception:
            logger.exception("Failed to cancel order %s", order_id)
            return False

    async def get_order_status(self, order_id: str) -> OrderStatus | None:
        try:
            orders = await asyncio.to_thread(self._wb.get_history_orders, count=50)
            if not orders:
                return None
            for o in orders:
                if str(o.get("orderId", "")) == order_id:
                    return OrderStatus(
                        order_id=order_id,
                        symbol=o.get("ticker", {}).get("symbol", ""),
                        side=o.get("action", ""),
                        qty=float(o.get("totalQuantity", 0)),
                        filled_qty=float(o.get("filledQuantity", 0)),
                        price=float(o.get("lmtPrice", 0)),
                        status=o.get("statusStr", "UNKNOWN"),
                        timestamp=o.get("placedTime", ""),
                    )
            return None
        except Exception:
            logger.exception("Failed to get order status for %s", order_id)
            return None

    async def get_open_orders(self) -> list[OrderStatus]:
        try:
            orders = await asyncio.to_thread(self._wb.get_current_orders)
            if not orders:
                return []
            return [
                OrderStatus(
                    order_id=str(o.get("orderId", "")),
                    symbol=o.get("ticker", {}).get("symbol", ""),
                    side=o.get("action", ""),
                    qty=float(o.get("totalQuantity", 0)),
                    filled_qty=float(o.get("filledQuantity", 0)),
                    price=float(o.get("lmtPrice", 0)),
                    status=o.get("statusStr", "PENDING"),
                    timestamp=o.get("placedTime", ""),
                )
                for o in orders
            ]
        except Exception:
            logger.exception("Failed to get open orders")
            return []
