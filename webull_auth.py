"""Webull OAuth/token management.

Handles login, MFA, and token persistence.
Tokens are cached to avoid repeated MFA prompts.

webull-python library uses these internal attributes:
  _did, _access_token, _refresh_token, _token_expire, _uuid, _account_id
Login param for MFA is 'mfa' (not 'mfa_code').
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
        self._last_login_result: dict = {}
        TOKENS_DIR.mkdir(exist_ok=True)

        # Set device ID from env if provided
        device_id = os.getenv("WEBULL_DEVICE_ID", "")
        if device_id:
            self._wb._did = device_id

    @property
    def client(self):
        return self._wb

    @property
    def is_logged_in(self) -> bool:
        return self._logged_in

    @property
    def needs_mfa(self) -> bool:
        """Check if last login attempt requires MFA."""
        return self._last_login_result.get("extInfo", {}).get("verificationCode") == "MFA"

    def login(self, mfa_code: str | None = None) -> bool:
        """Login to Webull. Returns True on success.

        Flow:
        1. Try cached token first
        2. If no cache, attempt fresh login
        3. If MFA required, call request_mfa() then login(mfa_code=...)
        """
        email = os.getenv("WEBULL_EMAIL", "")
        password = os.getenv("WEBULL_PASSWORD", "")
        pin = os.getenv("WEBULL_TRADING_PIN", "")

        if not email or not password:
            logger.error("WEBULL_EMAIL and WEBULL_PASSWORD must be set")
            return False

        # Try cached token first
        if not mfa_code and self._load_token():
            logger.info("Logged in with cached token")
            self._logged_in = True
            if pin:
                try:
                    self._wb.get_trade_token(pin)
                    logger.info("Trade token acquired")
                except Exception:
                    logger.exception("Failed to get trade token")
            return True

        # Fresh login
        try:
            result = self._wb.login(
                username=email,
                password=password,
                mfa=mfa_code or "",
            )
            self._last_login_result = result if isinstance(result, dict) else {}

            if isinstance(result, dict) and "accessToken" in result:
                self._save_token()
                self._logged_in = True
                if pin:
                    try:
                        self._wb.get_trade_token(pin)
                        logger.info("Trade token acquired")
                    except Exception:
                        logger.exception("Failed to get trade token")
                logger.info("Webull login successful (account_type=%s)", self.account_type)
                return True
            else:
                # Check if MFA is needed
                if isinstance(result, dict):
                    msg = result.get("msg", "")
                    code = result.get("code", "")
                    logger.warning("Login response: code=%s msg=%s", code, msg)
                    if "mfa" in str(result).lower() or "verification" in str(result).lower():
                        logger.info("MFA required — call request_mfa() then login(mfa_code=...)")
                        return False
                logger.error("Webull login failed: %s", result)
                return False
        except Exception:
            logger.exception("Webull login error")
            return False

    def request_mfa(self) -> bool:
        """Request MFA verification code to be sent to email/phone."""
        email = os.getenv("WEBULL_EMAIL", "")
        if not email:
            logger.error("WEBULL_EMAIL not set")
            return False
        try:
            result = self._wb.get_mfa(email)
            logger.info("MFA code requested for %s: %s", email, result)
            return True
        except Exception:
            logger.exception("Failed to request MFA")
            return False

    def login_with_mfa(self, mfa_code: str) -> bool:
        """Complete login with MFA code."""
        return self.login(mfa_code=mfa_code)

    def refresh_token(self) -> bool:
        """Refresh the access token."""
        try:
            result = self._wb.refresh_login()
            if result:
                self._save_token()
                self._logged_in = True
                logger.info("Token refreshed")
                return True
            return False
        except Exception:
            logger.exception("Token refresh failed")
            return False

    def get_login_status(self) -> dict:
        """Get detailed auth status."""
        return {
            "logged_in": self._logged_in,
            "account_type": self.account_type,
            "device_id": self._wb._did,
            "has_cached_token": TOKEN_FILE.exists(),
            "has_access_token": bool(self._wb._access_token),
            "has_trade_token": bool(self._wb._trade_token),
            "last_result_keys": list(self._last_login_result.keys()) if self._last_login_result else [],
        }

    def _save_token(self):
        """Atomically save token to disk."""
        try:
            token_data = {
                "access_token": self._wb._access_token,
                "refresh_token": self._wb._refresh_token,
                "token_expire": self._wb._token_expire,
                "uuid": getattr(self._wb, "_uuid", ""),
                "did": self._wb._did,
                "account_id": getattr(self._wb, "_account_id", ""),
            }
            fd, tmp_path = tempfile.mkstemp(dir=str(TOKENS_DIR), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(token_data, f, indent=2)
                os.replace(tmp_path, str(TOKEN_FILE))
                logger.info("Token saved to %s", TOKEN_FILE)
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
                self._wb._access_token = data["access_token"]
                self._wb._refresh_token = data.get("refresh_token", "")
                self._wb._token_expire = data.get("token_expire", "")
                if data.get("uuid"):
                    self._wb._uuid = data["uuid"]
                if data.get("did"):
                    self._wb._did = data["did"]
                if data.get("account_id"):
                    self._wb._account_id = data["account_id"]
                return True
        except Exception:
            logger.exception("Failed to load cached token")
        return False

    def clear_token(self):
        """Delete cached token (forces fresh login)."""
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()
            logger.info("Cached token deleted")
        self._logged_in = False
        self._wb._access_token = ""
        self._wb._refresh_token = ""
