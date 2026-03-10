"""Finnhub news data connector — Company news with sentiment scoring.

Fetches recent news articles for a symbol and computes a sentiment-weighted
score (0-100) based on recency, volume, and keyword analysis.
Free tier: 30 API calls/minute. Caches per symbol for 5 minutes.
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta

import httpx

logger = logging.getLogger(__name__)

# Rate limit: max 30 calls/minute (free tier) => 2s between calls
_MIN_CALL_INTERVAL = 2.0
_CACHE_TTL = 300.0  # 5 minutes

# Sentiment keywords
_POSITIVE_KEYWORDS = {"upgrade", "beat", "surge", "rally", "breakout", "raises",
                       "strong", "growth", "record", "soars", "jumps", "bullish"}
_NEGATIVE_KEYWORDS = {"downgrade", "miss", "plunge", "cut", "warning", "weak",
                       "loss", "decline", "crash", "bearish", "disappoints", "slump"}

_instance = None


def get_finnhub_news() -> "FinnhubNews":
    """Singleton accessor, matching the project's get_* pattern."""
    global _instance
    if _instance is None:
        _instance = FinnhubNews()
    return _instance


class FinnhubNews:
    """Fetches company news from Finnhub and scores sentiment."""

    def __init__(self):
        self._api_key = os.getenv("FINNHUB_API_KEY", "")
        if not self._api_key:
            logger.warning("FINNHUB_API_KEY not set -- news scores will return 0")
        self._base_url = "https://finnhub.io/api/v1/company-news"
        self._cache: dict[str, tuple[float, list[dict]]] = {}  # symbol -> (timestamp, articles)
        self._last_api_call: float = 0.0
        self._lock = asyncio.Lock()
        logger.info("FinnhubNews initialized")

    async def _rate_limit(self):
        """Enforce rate limiting between API calls."""
        elapsed = time.time() - self._last_api_call
        if elapsed < _MIN_CALL_INTERVAL:
            await asyncio.sleep(_MIN_CALL_INTERVAL - elapsed)
        self._last_api_call = time.time()

    def _is_cache_fresh(self, symbol: str) -> bool:
        if symbol not in self._cache:
            return False
        ts, _ = self._cache[symbol]
        return (time.time() - ts) < _CACHE_TTL

    async def get_news(self, symbol: str, days_back: int = 1) -> list[dict]:
        """Fetch recent company news articles from Finnhub.

        Returns raw article dicts with keys: category, datetime, headline,
        id, image, related, source, summary, url.
        """
        symbol = symbol.upper()

        # Return cached if fresh
        if self._is_cache_fresh(symbol):
            _, articles = self._cache[symbol]
            return articles

        if not self._api_key:
            return []

        now = datetime.utcnow()
        date_to = now.strftime("%Y-%m-%d")
        date_from = (now - timedelta(days=days_back)).strftime("%Y-%m-%d")

        params = {
            "symbol": symbol,
            "from": date_from,
            "to": date_to,
            "token": self._api_key,
        }

        try:
            async with self._lock:
                await self._rate_limit()

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(self._base_url, params=params)
                resp.raise_for_status()
                articles = resp.json()

            if not isinstance(articles, list):
                articles = []

            # Cache result
            self._cache[symbol] = (time.time(), articles)
            logger.debug("Finnhub: fetched %d articles for %s", len(articles), symbol)
            return articles

        except Exception:
            logger.exception("Finnhub news fetch error for %s", symbol)
            # Return stale cache if available
            if symbol in self._cache:
                _, articles = self._cache[symbol]
                return articles
            return []

    async def get_news_score(self, symbol: str) -> float:
        """Compute a news sentiment score (0-100) for a symbol.

        Scoring logic:
        - Recency weighting: last hour = full (1.0), 6hrs = half (0.5), 24hrs = quarter (0.25)
        - Volume component: more articles = higher base (capped at 10)
        - Keyword sentiment: positive keywords boost, negative keywords reduce
        - Final score is clamped to 0-100
        """
        articles = await self.get_news(symbol, days_back=1)

        if not articles:
            return 0.0

        now_epoch = time.time()
        total_weight = 0.0
        sentiment_sum = 0.0
        counted = 0

        for article in articles[:20]:  # Process at most 20 articles
            # Article timestamp (Finnhub uses epoch seconds in 'datetime' field)
            article_ts = article.get("datetime", 0)
            if not article_ts:
                continue

            age_hours = (now_epoch - article_ts) / 3600.0
            if age_hours < 0:
                age_hours = 0

            # Recency weight
            if age_hours <= 1:
                recency = 1.0
            elif age_hours <= 6:
                recency = 0.5
            elif age_hours <= 24:
                recency = 0.25
            else:
                recency = 0.1

            # Keyword sentiment from headline + summary
            text = (article.get("headline", "") + " " + article.get("summary", "")).lower()
            pos_hits = sum(1 for kw in _POSITIVE_KEYWORDS if kw in text)
            neg_hits = sum(1 for kw in _NEGATIVE_KEYWORDS if kw in text)
            # Per-article sentiment: base 50 (neutral), +10 per positive, -10 per negative
            article_sentiment = 50.0 + (pos_hits * 10.0) - (neg_hits * 10.0)
            article_sentiment = max(0.0, min(100.0, article_sentiment))

            total_weight += recency
            sentiment_sum += article_sentiment * recency
            counted += 1

        if counted == 0 or total_weight == 0:
            return 0.0

        # Weighted average sentiment
        avg_sentiment = sentiment_sum / total_weight

        # Volume multiplier: more articles = more conviction, capped at 10
        volume_factor = min(counted / 10.0, 1.0)

        # Final score: blend sentiment with volume factor
        # Low article count dampens the score (1 article => 10% weight, 10+ => 100%)
        raw_score = avg_sentiment * (0.1 + 0.9 * volume_factor)

        return round(max(0.0, min(100.0, raw_score)), 1)
