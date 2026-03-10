"""Basic tests for the trading API server."""

import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Create test client with mocked Webull auth."""
    with patch("webull_trading_api.get_auth") as mock_auth:
        mock_instance = MagicMock()
        mock_instance.login.return_value = True
        mock_instance.is_logged_in = True
        mock_instance.account_type = "paper"
        mock_auth.return_value = mock_instance

        from webull_trading_api import app
        with TestClient(app) as c:
            yield c


def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"
    assert "timestamp" in data


def test_status(client):
    resp = client.get("/api/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["server"] == "webull_trading_bot"
    assert data["trading_phase"] in [
        "OFFHOURS", "DISCOVERY", "LIVE", "EXIT_ONLY", "SHADOW"
    ]


def test_worklist_placeholder(client):
    resp = client.get("/api/worklist")
    assert resp.status_code == 200
    data = resp.json()
    assert "symbols" in data


def test_scalper_status_placeholder(client):
    resp = client.get("/api/scalper/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is False


def test_trading_phase_logic():
    """Test the trading phase determination."""
    from webull_trading_api import _get_trading_phase
    from datetime import datetime
    import pytz

    ET = pytz.timezone("US/Eastern")

    # 3:00 AM = OFFHOURS
    t = datetime(2026, 3, 10, 3, 0, tzinfo=ET)
    assert _get_trading_phase(t) == "OFFHOURS"

    # 5:00 AM = DISCOVERY
    t = datetime(2026, 3, 10, 5, 0, tzinfo=ET)
    assert _get_trading_phase(t) == "DISCOVERY"

    # 8:00 AM = LIVE
    t = datetime(2026, 3, 10, 8, 0, tzinfo=ET)
    assert _get_trading_phase(t) == "LIVE"

    # 9:20 AM = EXIT_ONLY
    t = datetime(2026, 3, 10, 9, 20, tzinfo=ET)
    assert _get_trading_phase(t) == "EXIT_ONLY"

    # 10:00 AM = SHADOW
    t = datetime(2026, 3, 10, 10, 0, tzinfo=ET)
    assert _get_trading_phase(t) == "SHADOW"

    # 17:00 = OFFHOURS
    t = datetime(2026, 3, 10, 17, 0, tzinfo=ET)
    assert _get_trading_phase(t) == "OFFHOURS"
