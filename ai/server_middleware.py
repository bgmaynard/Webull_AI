"""Server middleware — Rate limiting and backpressure for the trading API."""

import logging
import time
from collections import defaultdict
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple per-IP rate limiter using sliding window."""

    def __init__(self, app, requests_per_second: int = 20, burst: int = 50):
        super().__init__(app)
        self.rps = requests_per_second
        self.burst = burst
        self._requests: dict[str, list[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next):
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()

        # Clean old entries (sliding 1-second window)
        window = now - 1.0
        self._requests[client_ip] = [
            t for t in self._requests[client_ip] if t > window
        ]

        if len(self._requests[client_ip]) >= self.burst:
            logger.warning("Rate limit exceeded for %s", client_ip)
            return JSONResponse(
                status_code=429,
                content={"error": "Rate limit exceeded", "retry_after": 1},
            )

        self._requests[client_ip].append(now)
        return await call_next(request)


class BackpressureMiddleware(BaseHTTPMiddleware):
    """Reject requests when server is under heavy load."""

    def __init__(self, app, max_concurrent: int = 100):
        super().__init__(app)
        self.max_concurrent = max_concurrent
        self._active = 0

    async def dispatch(self, request: Request, call_next):
        if self._active >= self.max_concurrent:
            logger.warning("Backpressure: rejecting request (%d active)", self._active)
            return JSONResponse(
                status_code=503,
                content={"error": "Server under load", "active_requests": self._active},
            )

        self._active += 1
        try:
            return await call_next(request)
        finally:
            self._active -= 1
