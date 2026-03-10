"""Tests for the HFT scalper engine."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from ai.hft_scalper import HFTScalper, OpenTrade
from webull_market_data import Quote


@pytest.fixture
def scalper(tmp_path):
    """Create a scalper with temp config path."""
    with patch("ai.hft_scalper.CONFIG_PATH", tmp_path / "config.json"):
        with patch("ai.hft_scalper.get_gating") as mock_gating:
            mock_gating.return_value = MagicMock()
            s = HFTScalper()
            return s


def test_default_config(scalper):
    assert scalper.config["account_size"] == 500.0
    assert scalper.config["risk_percent"] == 2.0
    assert scalper.config["profit_target_percent"] == 2.5


def test_update_config(scalper):
    result = scalper.update_config({"account_size": 1000.0})
    assert result["account_size"] == 1000.0
    assert scalper.config["account_size"] == 1000.0


def test_enabled_not_persisted(scalper):
    """'enabled' must never be saved to config."""
    scalper.update_config({"enabled": True, "account_size": 750.0})
    assert "enabled" not in scalper.config or scalper.config.get("enabled") is not True
    assert scalper.config["account_size"] == 750.0


def test_position_sizing(scalper):
    # $500 account, 2% risk = $10 risk
    # At $10/share with 1.5% stop = $0.15 risk/share
    # $10 / $0.15 = 66 shares
    scalper.config["account_size"] = 500.0
    scalper.config["risk_percent"] = 2.0
    scalper.config["stop_loss_percent"] = 1.5
    qty = scalper._calculate_qty(10.0, scalper._calculate_risk_dollars())
    assert qty == 66


def test_position_sizing_zero_price(scalper):
    qty = scalper._calculate_qty(0.0, 10.0)
    assert qty == 0


def test_adaptive_stop():
    with patch("ai.hft_scalper.get_gating") as mock_gating:
        mock_gating.return_value = MagicMock()
        with patch("ai.hft_scalper.CONFIG_PATH", Path("/tmp/test_config.json")):
            s = HFTScalper()

    # Tight spread -> base stop
    q = Quote(symbol="TEST", bid=10.0, ask=10.05)
    stop = s._adaptive_stop_pct(q)
    assert 1.0 <= stop <= 3.0

    # Wide spread -> higher stop
    q2 = Quote(symbol="TEST", bid=10.0, ask=10.50)
    stop2 = s._adaptive_stop_pct(q2)
    assert stop2 > stop  # Wider spread = wider stop


def test_open_trade_pnl():
    trade = OpenTrade(
        symbol="AAPL",
        side="BUY",
        qty=100,
        entry_price=10.0,
        entry_time=0,
        order_id="123",
    )
    assert trade.pnl(10.50) == 50.0
    assert trade.pnl_pct(10.50) == 5.0
    assert trade.pnl(9.50) == -50.0
    assert trade.pnl_pct(9.50) == -5.0


def test_status(scalper):
    status = scalper.get_status()
    assert status["enabled"] is False
    assert status["running"] is False
    assert "config" in status


@pytest.mark.asyncio
async def test_start_requires_enabled(scalper):
    result = await scalper.start()
    assert result is False  # Not enabled


@pytest.mark.asyncio
async def test_start_and_stop(scalper):
    scalper.enable()
    assert scalper.enabled is True
    result = await scalper.start()
    assert result is True
    assert scalper.running is True

    result = await scalper.stop()
    assert result is True
    assert scalper.running is False
