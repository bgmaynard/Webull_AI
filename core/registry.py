"""Registry — Config-driven broker and market data factory.

Reads BROKER_PROVIDER and MARKET_DATA_PROVIDER from env to instantiate
the correct connector. Eliminates monkey-patching.
"""

import logging
import os

from core.interfaces import BrokerInterface, MarketDataInterface

logger = logging.getLogger(__name__)

_broker_instance: BrokerInterface | None = None
_market_data_instance: MarketDataInterface | None = None


def get_broker() -> BrokerInterface:
    global _broker_instance
    if _broker_instance is None:
        provider = os.getenv("BROKER_PROVIDER", "sim").lower()

        if provider == "alpaca":
            from brokers.alpaca import AlpacaBroker
            _broker_instance = AlpacaBroker()
            logger.info("Broker: Alpaca")

        elif provider == "webull":
            from brokers.webull import WebullBroker
            _broker_instance = WebullBroker()
            logger.info("Broker: Webull")

        elif provider == "sim":
            from sim.mock_broker import MockBroker
            starting_cash = float(os.getenv("SIM_STARTING_CASH", "500"))
            _broker_instance = MockBroker(starting_cash=starting_cash)
            logger.info("Broker: Simulation (cash=$%.0f)", starting_cash)

        else:
            raise ValueError(f"Unknown BROKER_PROVIDER: {provider}")

    return _broker_instance


def get_market_data() -> MarketDataInterface:
    global _market_data_instance
    if _market_data_instance is None:
        provider = os.getenv("MARKET_DATA_PROVIDER", "sim").lower()

        if provider == "polygon":
            from data.polygon import PolygonMarketData
            _market_data_instance = PolygonMarketData()
            logger.info("Market data: Polygon.io")

        elif provider == "webull":
            from data.webull import WebullMarketData
            _market_data_instance = WebullMarketData()
            logger.info("Market data: Webull")

        elif provider == "sim":
            from sim.mock_market_data import MockMarketData
            _market_data_instance = MockMarketData()
            logger.info("Market data: Simulation")

        else:
            raise ValueError(f"Unknown MARKET_DATA_PROVIDER: {provider}")

    return _market_data_instance


def get_provider_name() -> str:
    """Get the active broker provider name."""
    return os.getenv("BROKER_PROVIDER", "sim").lower()


def is_sim_mode() -> bool:
    return get_provider_name() == "sim"


def wire_sim_market_data(broker, market_data):
    """Link mock broker to mock market data (sim mode only)."""
    if hasattr(broker, "set_market_data"):
        broker.set_market_data(market_data)
