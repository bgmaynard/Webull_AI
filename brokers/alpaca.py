"""Alpaca broker connector — Free commission-free trading with paper mode.

Uses alpaca-py SDK. All blocking calls wrapped in asyncio.to_thread().
"""

import asyncio
import logging
import os

from core.interfaces import BrokerInterface
from core.models import AccountInfo, OrderResult, OrderSide, OrderStatus, OrderType, Position

logger = logging.getLogger(__name__)


class AlpacaBroker(BrokerInterface):
    def __init__(self):
        api_key = os.getenv("ALPACA_API_KEY", "")
        api_secret = os.getenv("ALPACA_API_SECRET", "")
        paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"

        if not api_key or not api_secret:
            raise ValueError("ALPACA_API_KEY and ALPACA_API_SECRET are required")

        from alpaca.trading.client import TradingClient
        self._client = TradingClient(api_key, api_secret, paper=paper)
        self._paper = paper
        logger.info("Alpaca broker initialized (paper=%s)", paper)

    async def get_account(self) -> AccountInfo:
        try:
            acct = await asyncio.to_thread(self._client.get_account)
            return AccountInfo(
                account_id=str(acct.id),
                net_liquidation=float(acct.equity),
                buying_power=float(acct.buying_power),
                cash=float(acct.cash),
                day_trades_remaining=max(0, 3 - int(acct.daytrade_count or 0)),
                account_type="paper" if self._paper else "live",
            )
        except Exception:
            logger.exception("Failed to get Alpaca account")
            return AccountInfo()

    async def get_positions(self) -> list[Position]:
        try:
            positions = await asyncio.to_thread(self._client.get_all_positions)
            return [
                Position(
                    symbol=p.symbol,
                    qty=float(p.qty),
                    avg_cost=float(p.avg_entry_price),
                    market_value=float(p.market_value),
                    unrealized_pnl=float(p.unrealized_pl),
                    side="LONG" if p.side == "long" else "SHORT",
                )
                for p in positions
            ]
        except Exception:
            logger.exception("Failed to get Alpaca positions")
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
            from alpaca.trading.requests import (
                LimitOrderRequest,
                MarketOrderRequest,
                StopLimitOrderRequest,
                StopOrderRequest,
            )
            from alpaca.trading.enums import OrderSide as AlpSide, TimeInForce

            alp_side = AlpSide.BUY if side == OrderSide.BUY else AlpSide.SELL

            if order_type == OrderType.LIMIT:
                if limit_price is None:
                    return OrderResult(success=False, message="Limit price required")
                request = LimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=alp_side,
                    time_in_force=TimeInForce.DAY,
                    limit_price=limit_price,
                )
            elif order_type == OrderType.MARKET:
                request = MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=alp_side,
                    time_in_force=TimeInForce.DAY,
                )
            elif order_type == OrderType.STOP:
                request = StopOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=alp_side,
                    time_in_force=TimeInForce.DAY,
                    stop_price=stop_price,
                )
            elif order_type == OrderType.STOP_LIMIT:
                request = StopLimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=alp_side,
                    time_in_force=TimeInForce.DAY,
                    limit_price=limit_price,
                    stop_price=stop_price,
                )
            else:
                return OrderResult(success=False, message=f"Unknown order type: {order_type}")

            order = await asyncio.to_thread(self._client.submit_order, request)
            logger.info("Alpaca order placed: %s %d %s (id=%s)", side.value, qty, symbol, order.id)
            return OrderResult(success=True, order_id=str(order.id))

        except Exception as e:
            logger.exception("Alpaca order error")
            return OrderResult(success=False, message=str(e))

    async def cancel_order(self, order_id: str) -> bool:
        try:
            await asyncio.to_thread(self._client.cancel_order_by_id, order_id)
            logger.info("Alpaca order cancelled: %s", order_id)
            return True
        except Exception:
            logger.exception("Failed to cancel Alpaca order %s", order_id)
            return False

    async def get_order_status(self, order_id: str) -> OrderStatus | None:
        try:
            order = await asyncio.to_thread(self._client.get_order_by_id, order_id)
            return OrderStatus(
                order_id=str(order.id),
                symbol=order.symbol,
                side=order.side.value.upper(),
                qty=float(order.qty),
                filled_qty=float(order.filled_qty or 0),
                price=float(order.limit_price or order.filled_avg_price or 0),
                status=order.status.value.upper(),
                timestamp=str(order.submitted_at or ""),
            )
        except Exception:
            logger.exception("Failed to get Alpaca order status %s", order_id)
            return None

    async def get_open_orders(self) -> list[OrderStatus]:
        try:
            from alpaca.trading.requests import GetOrdersRequest
            from alpaca.trading.enums import QueryOrderStatus

            request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
            orders = await asyncio.to_thread(self._client.get_orders, request)
            return [
                OrderStatus(
                    order_id=str(o.id),
                    symbol=o.symbol,
                    side=o.side.value.upper(),
                    qty=float(o.qty),
                    filled_qty=float(o.filled_qty or 0),
                    price=float(o.limit_price or 0),
                    status=o.status.value.upper(),
                    timestamp=str(o.submitted_at or ""),
                )
                for o in orders
            ]
        except Exception:
            logger.exception("Failed to get Alpaca open orders")
            return []
