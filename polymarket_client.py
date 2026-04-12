"""Async Polymarket client — Gamma (metadata) + CLOB (order books).

This is the Phase 1.1 foundation module. Everything in later phases
(market discovery, snapshot loop, backtest replay, live executor) talks to
Polymarket through this one class so we have a single place to reason about
retries, rate limits, parsing, and error handling.

Read-only today. Write operations (order placement) land in Phase 3 and will
live in a separate `ExecutionClient` that composes this one.

Design notes
------------
- `httpx.AsyncClient` with HTTP/2 and connection pooling.
- Concurrency is bounded by an `asyncio.Semaphore`; we don't implement a
  per-second token bucket because Polymarket doesn't publish a hard rate
  limit and we'd rather be gentle (low semaphore) than complex.
- Retries are manual exponential backoff (0.5s, 1s, 2s) on network errors
  and 5xx. 4xx errors (including 429) are raised immediately so the caller
  can decide what to do.
- Parsing is defensive: Gamma returns some fields as JSON-encoded strings,
  some as native types, and occasionally drops optional fields entirely.
- Zero global state. Create one client per async context.

Example
-------
    async with PolymarketClient() as client:
        markets = await client.get_markets(limit=20, min_liquidity=50_000)
        token_ids = [m.yes_token_id for m in markets if m.yes_token_id]
        books = await client.get_orderbooks(token_ids)
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx

logger = logging.getLogger(__name__)

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"

DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_CONCURRENCY = 8
DEFAULT_USER_AGENT = "polymarket-arb-bot/0.1 (+personal research)"
RETRY_BACKOFF_SEC = (0.5, 1.0, 2.0)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PolymarketError(Exception):
    """Base exception for all polymarket_client errors."""


class PolymarketNetworkError(PolymarketError):
    """Raised when the HTTP layer fails after all retries."""


class PolymarketAPIError(PolymarketError):
    """Raised when the API returns a non-retryable error status."""

    def __init__(self, status_code: int, url: str, body: str):
        self.status_code = status_code
        self.url = url
        self.body = body
        super().__init__(f"{status_code} from {url}: {body[:200]}")


class PolymarketParseError(PolymarketError):
    """Raised when a response body cannot be parsed into expected shape."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderBookLevel:
    """One level of an order book: price in USD (0..1) and size in shares."""

    price: float
    size: float


@dataclass
class OrderBook:
    """Normalized L2 order book for one CLOB token.

    `bids` sorted DESC by price (best bid first).
    `asks` sorted ASC by price (best ask first).
    Empty lists are valid — the book may be one-sided or fully empty.
    """

    token_id: str
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)
    timestamp_ms: int | None = None
    market_hash: str | None = None

    @property
    def best_bid(self) -> OrderBookLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> OrderBookLevel | None:
        return self.asks[0] if self.asks else None

    @property
    def mid(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None:
            return None
        return (bb.price + ba.price) / 2.0

    @property
    def spread(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None:
            return None
        return ba.price - bb.price


@dataclass
class Market:
    """A Polymarket binary (or multi-outcome) market."""

    market_id: str
    question: str
    condition_id: str
    slug: str
    yes_token_id: str | None
    no_token_id: str | None
    outcomes: list[str]
    volume: float
    liquidity: float
    active: bool
    closed: bool
    end_date: str | None
    category: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def is_binary(self) -> bool:
        return (
            self.yes_token_id is not None
            and self.no_token_id is not None
            and len(self.outcomes) == 2
        )


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _as_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return default


def _decode_maybe_json_list(value: Any) -> list[Any]:
    """Gamma sometimes returns arrays as JSON-encoded strings. Decode defensively."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            if isinstance(decoded, list):
                return decoded
        except json.JSONDecodeError:
            return []
    return []


def parse_market(raw: dict[str, Any]) -> Market | None:
    """Parse a single Gamma market payload. Returns None if the shape is not
    something we can trade on (e.g. missing clob tokens)."""
    try:
        token_ids = _decode_maybe_json_list(raw.get("clobTokenIds"))
        outcomes = _decode_maybe_json_list(raw.get("outcomes"))

        yes_token_id: str | None = None
        no_token_id: str | None = None
        if len(token_ids) == 2:
            yes_token_id = str(token_ids[0])
            no_token_id = str(token_ids[1])

        return Market(
            market_id=str(raw.get("id", "")),
            question=str(raw.get("question", "")),
            condition_id=str(raw.get("conditionId", "")),
            slug=str(raw.get("slug", "")),
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            outcomes=[str(o) for o in outcomes],
            volume=_as_float(raw.get("volumeNum")),
            liquidity=_as_float(raw.get("liquidityNum")),
            active=_as_bool(raw.get("active")),
            closed=_as_bool(raw.get("closed")),
            end_date=raw.get("endDate"),
            category=raw.get("category"),
            raw=raw,
        )
    except Exception as exc:  # defensive: never crash the whole batch on one bad row
        logger.warning("failed to parse market %s: %s", raw.get("id"), exc)
        return None


def parse_orderbook(token_id: str, raw: dict[str, Any]) -> OrderBook:
    """Parse a CLOB /book response into an OrderBook."""

    def _parse_side(items: Any) -> list[OrderBookLevel]:
        if not isinstance(items, list):
            return []
        levels: list[OrderBookLevel] = []
        for item in items:
            try:
                price = float(item["price"])
                size = float(item["size"])
            except (KeyError, TypeError, ValueError):
                continue
            levels.append(OrderBookLevel(price=price, size=size))
        return levels

    bids = _parse_side(raw.get("bids"))
    asks = _parse_side(raw.get("asks"))
    bids.sort(key=lambda lv: lv.price, reverse=True)
    asks.sort(key=lambda lv: lv.price)

    ts_raw = raw.get("timestamp")
    ts: int | None = None
    if ts_raw is not None:
        try:
            ts = int(ts_raw)
        except (TypeError, ValueError):
            ts = None

    return OrderBook(
        token_id=token_id,
        bids=bids,
        asks=asks,
        timestamp_ms=ts,
        market_hash=raw.get("hash"),
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class PolymarketClient:
    """Async read-only client for Polymarket Gamma + CLOB APIs.

    Use as an async context manager so the underlying HTTP pool is closed
    cleanly:

        async with PolymarketClient() as client:
            markets = await client.get_markets(limit=10)
    """

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        user_agent: str = DEFAULT_USER_AGENT,
        gamma_url: str = GAMMA_URL,
        clob_url: str = CLOB_URL,
    ) -> None:
        self._timeout = timeout
        self._max_retries = max_retries
        self._gamma_url = gamma_url.rstrip("/")
        self._clob_url = clob_url.rstrip("/")
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._headers = {
            "User-Agent": user_agent,
            "Accept": "application/json",
        }
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> PolymarketClient:
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            headers=self._headers,
            http2=False,  # gamma served via CF; http2 sometimes flaky
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=DEFAULT_MAX_CONCURRENCY * 2,
                max_keepalive_connections=DEFAULT_MAX_CONCURRENCY,
            ),
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---- internal HTTP helper --------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if self._client is None:
            raise RuntimeError(
                "PolymarketClient must be used inside `async with` context"
            )

        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                async with self._semaphore:
                    resp = await self._client.request(method, url, params=params)
            except (httpx.NetworkError, httpx.TimeoutException) as exc:
                last_exc = exc
                logger.debug("network error on %s (attempt %d): %s", url, attempt, exc)
                if attempt < self._max_retries:
                    await asyncio.sleep(RETRY_BACKOFF_SEC[min(attempt, len(RETRY_BACKOFF_SEC) - 1)])
                    continue
                raise PolymarketNetworkError(f"{url}: {exc}") from exc

            if 500 <= resp.status_code < 600:
                last_exc = PolymarketAPIError(resp.status_code, url, resp.text)
                logger.debug("5xx on %s: %s (attempt %d)", url, resp.status_code, attempt)
                if attempt < self._max_retries:
                    await asyncio.sleep(RETRY_BACKOFF_SEC[min(attempt, len(RETRY_BACKOFF_SEC) - 1)])
                    continue
                raise last_exc

            if resp.status_code >= 400:
                raise PolymarketAPIError(resp.status_code, url, resp.text)

            try:
                return resp.json()
            except ValueError as exc:
                raise PolymarketParseError(f"{url}: invalid JSON: {exc}") from exc

        # unreachable, but satisfies type checker
        assert last_exc is not None
        raise last_exc

    # ---- Gamma: market discovery -----------------------------------------

    async def get_markets(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
        closed: bool = False,
        order: str = "liquidityNum",
        ascending: bool = False,
        min_volume: float = 0.0,
        min_liquidity: float = 0.0,
        price_range: tuple[float, float] | None = None,
    ) -> list[Market]:
        """List active markets from the Gamma API, filtered client-side.

        Args:
            limit: number of markets to return after filtering (not before).
            offset: pagination offset on the raw query.
            active / closed: forwarded to Gamma.
            order: Gamma sort key. We default to liquidityNum to avoid the
                volumeNum dead-tail bias documented in the README.
            ascending: sort direction.
            min_volume / min_liquidity: client-side threshold.
            price_range: if set (lo, hi), drop markets whose implied yes
                probability is outside [lo, hi]. Requires a fetched order
                book to know, so we skip it here — it's applied in Phase 1.2
                after we have book data. Exposed now as a reminder.
        """
        # over-fetch to compensate for client-side filtering
        raw_limit = max(limit * 3, 50)

        params: dict[str, Any] = {
            "limit": str(raw_limit),
            "offset": str(offset),
            "active": "true" if active else "false",
            "closed": "true" if closed else "false",
            "order": order,
            "ascending": "true" if ascending else "false",
        }
        data = await self._request("GET", f"{self._gamma_url}/markets", params=params)

        if not isinstance(data, list):
            raise PolymarketParseError(
                f"expected list from /markets, got {type(data).__name__}"
            )

        parsed: list[Market] = []
        for raw in data:
            if not isinstance(raw, dict):
                continue
            m = parse_market(raw)
            if m is None:
                continue
            if m.volume < min_volume:
                continue
            if m.liquidity < min_liquidity:
                continue
            parsed.append(m)
            if len(parsed) >= limit:
                break
        return parsed

    # ---- CLOB: order book ------------------------------------------------

    async def get_orderbook(self, token_id: str) -> OrderBook:
        """Fetch the CLOB order book for one token id."""
        data = await self._request(
            "GET",
            f"{self._clob_url}/book",
            params={"token_id": token_id},
        )
        if not isinstance(data, dict):
            raise PolymarketParseError(
                f"expected dict from /book, got {type(data).__name__}"
            )
        return parse_orderbook(token_id, data)

    async def get_orderbooks(
        self, token_ids: Iterable[str]
    ) -> dict[str, OrderBook]:
        """Fetch many order books concurrently.

        Returns a dict keyed by token_id. Failed fetches are omitted from the
        result (and logged at WARNING). The caller decides whether a missing
        key is fatal.
        """
        ids = list(token_ids)
        if not ids:
            return {}

        async def _one(tid: str) -> tuple[str, OrderBook | None]:
            try:
                book = await self.get_orderbook(tid)
                return tid, book
            except PolymarketError as exc:
                logger.warning("orderbook fetch failed for %s: %s", tid, exc)
                return tid, None

        results = await asyncio.gather(*(_one(tid) for tid in ids))
        return {tid: book for tid, book in results if book is not None}


# ---------------------------------------------------------------------------
# Helper for scripts: one-shot context without manual `async with`
# ---------------------------------------------------------------------------


@asynccontextmanager
async def open_client(**kwargs: Any):
    """Shorthand for code that just needs a client in an async block."""
    async with PolymarketClient(**kwargs) as client:
        yield client


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------


async def _demo() -> int:
    """Small demo: fetch 5 liquid markets + their order books, print summary.

    Run with:  python polymarket_client.py
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    async with PolymarketClient() as client:
        print("1) get_markets(limit=5, min_liquidity=10_000, order=liquidityNum)")
        markets = await client.get_markets(limit=5, min_liquidity=10_000)
        if not markets:
            print("   no markets returned — try lowering min_liquidity")
            return 1

        for m in markets:
            print(f"   [{m.market_id:>8}] liq=${m.liquidity:>12,.0f}  {m.question[:70]}")

        print("\n2) get_orderbooks(batch) for yes_token_ids")
        yes_ids = [m.yes_token_id for m in markets if m.yes_token_id]
        books = await client.get_orderbooks(yes_ids)
        print(f"   received {len(books)}/{len(yes_ids)} books")

        print("\n3) summary (yes side only)")
        for m in markets:
            if not m.yes_token_id:
                continue
            book = books.get(m.yes_token_id)
            if book is None or book.best_ask is None:
                print(f"   [{m.market_id:>8}] no liquidity")
                continue
            bb = book.best_bid
            ba = book.best_ask
            bb_str = f"{bb.price:.4f} x {bb.size:.0f}" if bb else "-"
            ba_str = f"{ba.price:.4f} x {ba.size:.0f}"
            spread_str = f"{book.spread:.4f}" if book.spread is not None else "-"
            print(
                f"   [{m.market_id:>8}] bid={bb_str:<20} ask={ba_str:<20} spread={spread_str}"
            )

    print("\nOK — PolymarketClient working.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_demo()))
