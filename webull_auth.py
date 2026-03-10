"""Webull OAuth/token management.

Handles login, MFA, and token persistence.
Tokens are cached to avoid repeated MFA prompts.
"""

import json
import logging
import os
import tempfile
from pathlib import Path

from webull import webull, paper_webull

logger = logging.getLogger(__name__)

TOKENS_DIR = Path("tokens")
TOKEN_FILE = TOKENS_DIR / "webull_token.json"

_instance = None


def get_auth() -> "WebullAuth":
    global _instance
    if _instance is None:
        _instance = WebullAuth()
    return _instance


class WebullAuth:
    def __init__(self):
        self.account_type: str = os.getenv("WEBULL_ACCOUNT_TYPE", "paper")
        self._wb = paper_webull() if self.account_type == "paper" else webull()
        self._logged_in = False
        TOKENS_DIR.mkdir(exist_ok=True)

    @property
    def client(self):
        return self._wb

    @property
    def is_logged_in(self) -> bool:
        return self._logged_in

    def login(self, mfa_code: str | None = None) -> bool:
        """Login to Webull. Returns True on success."""
        email = os.getenv("WEBULL_EMAIL", "")
        password = os.getenv("WEBULL_PASSWORD", "")
        device_id = os.getenv("WEBULL_DEVICE_ID", "")
        pin = os.getenv("WEBULL_TRADING_PIN", "")

        if not email or not password:
            logger.error("WEBULL_EMAIL and WEBULL_PASSWORD must be set")
            return False

        if device_id:
            self._wb.device_id = device_id

        # Try cached token first
        if self._load_token():
            logger.info("Logged in with cached token")
            self._logged_in = True
            if pin:
                self._wb.get_trade_token(pin)
            return True

        # Fresh login
        try:
            login_result = self._wb.login(email, password, mfa_code=mfa_code)
            if login_result:
                self._save_token()
                self._logged_in = True
                if pin:
                    self._wb.get_trade_token(pin)
                logger.info("Webull login successful (account_type=%s)", self.account_type)
                return True
            else:
                logger.error("Webull login failed")
                return False
        except Exception:
            logger.exception("Webull login error")
            return False

    def request_mfa(self) -> bool:
        """Request MFA code to be sent."""
        email = os.getenv("WEBULL_EMAIL", "")
        if not email:
            return False
        try:
            self._wb.get_mfa(email)
            logger.info("MFA code requested for %s", email)
            return True
        except Exception:
            logger.exception("Failed to request MFA")
            return False

    def refresh_token(self) -> bool:
        """Refresh the access token."""
        try:
            result = self._wb.refresh_login()
            if result:
                self._save_token()
                logger.info("Token refreshed")
                return True
            return False
        except Exception:
            logger.exception("Token refresh failed")
            return False

    def _save_token(self):
        """Atomically save token to disk."""
        try:
            token_data = {
                "access_token": getattr(self._wb, "access_token", None),
                "refresh_token": getattr(self._wb, "refresh_token", None),
                "token_expire": getattr(self._wb, "token_expire", None),
                "uuid": getattr(self._wb, "uuid", None),
                "device_id": self._wb.device_id,
            }
            fd, tmp_path = tempfile.mkstemp(dir=str(TOKENS_DIR), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(token_data, f, indent=2)
                os.replace(tmp_path, str(TOKEN_FILE))
            except Exception:
                os.unlink(tmp_path)
                raise
        except Exception:
            logger.exception("Failed to save token")

    def _load_token(self) -> bool:
        """Load cached token from disk."""
        if not TOKEN_FILE.exists():
            return False
        try:
            with open(TOKEN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("access_token"):
                self._wb.access_token = data["access_token"]
                self._wb.refresh_token = data.get("refresh_token", "")
                self._wb.token_expire = data.get("token_expire", "")
                self._wb.uuid = data.get("uuid", "")
                if data.get("device_id"):
                    self._wb.device_id = data["device_id"]
                return True
        except Exception:
            logger.exception("Failed to load cached token")
        return False
