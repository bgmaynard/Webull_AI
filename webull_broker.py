"""DEPRECATED: Import from core.models and core.registry instead.

This shim exists for backward compatibility during migration.
"""

from core.models import AccountInfo, OrderResult, OrderSide, OrderStatus, OrderType, Position
from core.registry import get_broker

__all__ = [
    "AccountInfo",
    "OrderResult",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "Position",
    "get_broker",
]
