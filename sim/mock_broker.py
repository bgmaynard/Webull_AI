"""Mock broker — Simulates order execution for paper trading.

Fills limit orders when price crosses, tracks positions and P&L.
"""

import logging
import random
import time
import uuid
from dataclasses import dataclass, field

from core.models import AccountInfo, OrderResult, OrderSide, OrderStatus, OrderType, Position

logger = logging.getLogger(__name__)


@dataclass
class MockOrder:
    order_id: str
    symbol: str
    side: str
    qty: int
    order_type: str
    limit_price: float
    stop_price: float
    status: str = "PENDING"
    filled_qty: int = 0
    fill_price: float = 0.0
    placed_at: float = field(default_factory=time.time)


@dataclass
class MockPosition:
    symbol: str
    qty: int
    avg_cost: float


class MockBroker:
    """Drop-in replacement for WebullBroker with simulated execution."""

    def __init__(self, starting_cash: float = 500.0):
        self._starting_cash = starting_cash
        self._cash = starting_cash
        self._positions: dict[str, MockPosition] = {}
        self._orders: dict[str, MockOrder] = {}
        self._filled_orders: list[MockOrder] = []
        self._market_data = None  # Set by caller

    def set_market_data(self, md):
        """Link to mock market data for price checks."""
        self._market_data = md

    async def get_account(self) -> AccountInfo:
        equity = self._cash + sum(
            p.qty * p.avg_cost for p in self._positions.values()
        )
        return AccountInfo(
            account_id="SIM-001",
            net_liquidation=round(equity, 2),
            buying_power=round(self._cash, 2),
            cash=round(self._cash, 2),
            day_trades_remaining=99,
            account_type="simulation",
        )

    async def get_positions(self) -> list[Position]:
        positions = []
        for p in self._positions.values():
            current_price = p.avg_cost  # Default
            if self._market_data:
                quote = self._market_data.get_cached_quote(p.symbol)
                if quote and quote.price > 0:
                    current_price = quote.price

            market_value = p.qty * current_price
            pnl = (current_price - p.avg_cost) * p.qty
            positions.append(Position(
                symbol=p.symbol,
                qty=p.qty,
                avg_cost=round(p.avg_cost, 2),
                market_value=round(market_value, 2),
                unrealized_pnl=round(pnl, 2),
            ))
        return positions

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        qty: int,
        order_type: OrderType = OrderType.LIMIT,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> OrderResult:
        order_id = uuid.uuid4().hex[:8]

        order = MockOrder(
            order_id=order_id,
            symbol=symbol,
            side=side.value,
            qty=qty,
            order_type=order_type.value,
            limit_price=limit_price or 0,
            stop_price=stop_price or 0,
        )

        # Simulate immediate fill for limit orders (realistic slippage)
        slippage = random.uniform(0, 0.005)  # 0-0.5% slippage

        if side == OrderSide.BUY:
            fill_price = (limit_price or 0) * (1 + slippage)
            cost = fill_price * qty
            if cost > self._cash:
                return OrderResult(success=False, message=f"Insufficient cash: need ${cost:.2f}, have ${self._cash:.2f}")

            self._cash -= cost
            order.status = "FILLED"
            order.filled_qty = qty
            order.fill_price = round(fill_price, 2)

            # Update position
            if symbol in self._positions:
                pos = self._positions[symbol]
                total_cost = pos.avg_cost * pos.qty + fill_price * qty
                pos.qty += qty
                pos.avg_cost = total_cost / pos.qty
            else:
                self._positions[symbol] = MockPosition(symbol=symbol, qty=qty, avg_cost=round(fill_price, 2))

        elif side == OrderSide.SELL:
            fill_price = (limit_price or 0) * (1 - slippage)

            if symbol not in self._positions or self._positions[symbol].qty < qty:
                return OrderResult(success=False, message=f"No position in {symbol} to sell")

            self._cash += fill_price * qty
            order.status = "FILLED"
            order.filled_qty = qty
            order.fill_price = round(fill_price, 2)

            pos = self._positions[symbol]
            pos.qty -= qty
            if pos.qty <= 0:
                del self._positions[symbol]

        self._orders[order_id] = order
        self._filled_orders.append(order)

        logger.info("SIM %s %d %s @ $%.2f (id=%s, cash=$%.2f)",
                     side.value, qty, symbol, order.fill_price, order_id, self._cash)

        return OrderResult(success=True, order_id=order_id)

    async def cancel_order(self, order_id: str) -> bool:
        order = self._orders.get(order_id)
        if order and order.status == "PENDING":
            order.status = "CANCELLED"
            return True
        return False

    async def get_order_status(self, order_id: str) -> OrderStatus | None:
        order = self._orders.get(order_id)
        if not order:
            return None
        return OrderStatus(
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            filled_qty=order.filled_qty,
            price=order.fill_price or order.limit_price,
            status=order.status,
            timestamp=str(int(order.placed_at)),
        )

    async def get_open_orders(self) -> list[OrderStatus]:
        return [
            OrderStatus(
                order_id=o.order_id, symbol=o.symbol, side=o.side,
                qty=o.qty, filled_qty=o.filled_qty,
                price=o.limit_price, status=o.status,
            )
            for o in self._orders.values() if o.status == "PENDING"
        ]

    def get_sim_stats(self) -> dict:
        realized_pnl = self._cash - self._starting_cash + sum(
            p.avg_cost * p.qty for p in self._positions.values()
        ) - self._starting_cash
        return {
            "starting_cash": self._starting_cash,
            "current_cash": round(self._cash, 2),
            "open_positions": len(self._positions),
            "total_fills": len(self._filled_orders),
            "realized_pnl": round(self._cash - self._starting_cash, 2) if not self._positions else "N/A (positions open)",
        }
