"""DEPRECATED: Import from core.models and core.registry instead.

This shim exists for backward compatibility during migration.
"""

from core.models import Quote
from core.registry import get_market_data

__all__ = [
    "Quote",
    "get_market_data",
]
