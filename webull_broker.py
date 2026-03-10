"""Webull broker abstraction — orders, positions, account info.

All methods use asyncio.to_thread() to avoid blocking the event loop.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from webull_auth import get_auth

logger = logging.getLogger(__name__)

_instance = None


def get_broker() -> "WebullBroker":
    global _instance
    if _instance is None:
        _instance = WebullBroker()
    return _instance


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LMT"
    MARKET = "MKT"
    STOP = "STP"
    STOP_LIMIT = "STP_LMT"


@dataclass
class Position:
    symbol: str
    qty: float
    avg_cost: float
    market_value: float
    unrealized_pnl: float
    side: str = "LONG"

    @property
    def current_price(self) -> float:
        if self.qty == 0:
            return 0.0
        return self.market_value / self.qty


@dataclass
class AccountInfo:
    account_id: str = ""
    net_liquidation: float = 0.0
    buying_power: float = 0.0
    cash: float = 0.0
    day_trades_remaining: int = 0
    account_type: str = "paper"


@dataclass
class OrderResult:
    success: bool
    order_id: str = ""
    message: str = ""


@dataclass
class OrderStatus:
    order_id: str
    symbol: str
    side: str
    qty: float
    filled_qty: float
    price: float
    status: str  # PENDING, FILLED, CANCELLED, FAILED
    timestamp: str = ""


class WebullBroker:
    def __init__(self):
        self._auth = get_auth()

    @property
    def _wb(self):
        return self._auth.client

    async def get_account(self) -> AccountInfo:
        """Get account summary."""
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
        """Get all open positions."""
        try:
            data = await asyncio.to_thread(self._wb.get_positions)
            if not data:
                return []
            positions = []
            for p in data:
                positions.append(Position(
                    symbol=p.get("ticker", {}).get("symbol", "???"),
                    qty=float(p.get("position", 0)),
                    avg_cost=float(p.get("costPrice", 0)),
                    market_value=float(p.get("marketValue", 0)),
                    unrealized_pnl=float(p.get("unrealizedProfitLoss", 0)),
                ))
            return positions
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
        """Place an order. Premarket = LIMIT only."""
        try:
            if order_type == OrderType.LIMIT and limit_price is None:
                return OrderResult(success=False, message="Limit price required for LIMIT orders")

            action = side.value

            def _place():
                if order_type == OrderType.LIMIT:
                    return self._wb.place_order(
                        stock=symbol,
                        action=action,
                        orderType="LMT",
                        enforce="GTC",
                        quant=qty,
                        price=limit_price,
                    )
                elif order_type == OrderType.MARKET:
                    return self._wb.place_order(
                        stock=symbol,
                        action=action,
                        orderType="MKT",
                        enforce="GTC",
                        quant=qty,
                    )
                elif order_type == OrderType.STOP:
                    return self._wb.place_order(
                        stock=symbol,
                        action=action,
                        orderType="STP",
                        enforce="GTC",
                        quant=qty,
                        stpPrice=stop_price,
                    )
                else:
                    return self._wb.place_order(
                        stock=symbol,
                        action=action,
                        orderType="STP LMT",
                        enforce="GTC",
                        quant=qty,
                        price=limit_price,
                        stpPrice=stop_price,
                    )

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
        """Cancel an open order."""
        try:
            result = await asyncio.to_thread(self._wb.cancel_order, order_id)
            logger.info("Cancel order %s: %s", order_id, result)
            return bool(result)
        except Exception:
            logger.exception("Failed to cancel order %s", order_id)
            return False

    async def get_order_status(self, order_id: str) -> OrderStatus | None:
        """Get status of a specific order."""
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
        """Get all open/pending orders."""
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
