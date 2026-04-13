"""Async SX Bet client — sports/markets/odds fetcher for cross-platform arb.

SX Bet is a crypto-native sports betting exchange (USDC-based, global access).
This client fetches market data and odds to compare against Polymarket prices.

Read-only. No auth required for data endpoints.

API base: https://api.sx.bet
Key endpoints:
  GET /sports           — list all sports
  GET /leagues/active   — list active leagues
  GET /markets/active   — list active markets (filtered by sportId, leagueId)
  GET /active-orders    — get best odds for markets (by marketHash or leagueId)

Odds are in implied probability format (0.0 to 1.0), same scale as Polymarket.

Example
-------
    async with SXBetClient() as client:
        sports = await client.get_sports()
        markets = await client.get_active_markets(sport_id=5)  # soccer
        odds = await client.get_odds([m.market_hash for m in markets[:10]])
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx

logger = logging.getLogger(__name__)

SX_API_URL = "https://api.sx.bet"
# USDC on SX Mainnet — required for /orders endpoints
SX_USDC_ADDRESS = "0xe2aa35C2039Bd0Ff196A6Ef99523CC0D3972ae3e"

DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_MAX_CONCURRENCY = 8
DEFAULT_USER_AGENT = "polymarket-arb-bot/0.1 (+personal research)"
RETRY_BACKOFF_SEC = (0.5, 1.0, 2.0)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SXBetError(Exception):
    """Base exception for all SX Bet client errors."""


class SXBetNetworkError(SXBetError):
    """HTTP layer failure after all retries."""


class SXBetAPIError(SXBetError):
    """Non-retryable API error."""

    def __init__(self, status_code: int, url: str, body: str):
        self.status_code = status_code
        self.url = url
        self.body = body
        super().__init__(f"{status_code} from {url}: {body[:200]}")


class SXBetParseError(SXBetError):
    """Response body couldn't be parsed."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sport:
    sport_id: int
    label: str


@dataclass(frozen=True)
class League:
    league_id: int
    label: str
    sport_id: int
    home_team_first: bool = True


@dataclass
class SXMarket:
    """An active market on SX Bet."""

    market_hash: str
    status: str
    outcome_one_name: str
    outcome_two_name: str
    outcome_void_name: str
    team_one_name: str
    team_two_name: str
    market_type: int  # 1=moneyline, 2=spread, 3=total, etc.
    game_time: int  # unix timestamp
    sport_id: int
    sport_label: str
    league_id: int
    league_label: str
    home_team_first: bool
    live_enabled: bool
    group1: str  # event grouping key
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


@dataclass(frozen=True)
class SXOrder:
    """A single order on SX Bet orderbook."""

    order_hash: str
    market_hash: str
    maker: str
    total_bet_size: float
    fill_amount: float
    implied_odds: float  # 0.0 to 1.0 (same scale as Polymarket)
    is_maker_betting_outcome_one: bool

    @property
    def remaining_size(self) -> float:
        return max(0.0, self.total_bet_size - self.fill_amount)

    @property
    def side(self) -> str:
        """Which outcome this maker is backing."""
        return "outcome1" if self.is_maker_betting_outcome_one else "outcome2"


@dataclass
class SXOdds:
    """Best odds for a market — aggregated from active orders."""

    market_hash: str
    outcome_one_best: float | None = None  # best implied odds for outcome 1
    outcome_two_best: float | None = None  # best implied odds for outcome 2
    outcome_one_size: float = 0.0
    outcome_two_size: float = 0.0
    orders: list[SXOrder] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _as_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return default


def parse_sport(raw: dict[str, Any]) -> Sport | None:
    try:
        return Sport(
            sport_id=_as_int(raw.get("sportId")),
            label=str(raw.get("label", "")),
        )
    except Exception as exc:
        logger.warning("failed to parse sport: %s", exc)
        return None


def parse_league(raw: dict[str, Any]) -> League | None:
    try:
        return League(
            league_id=_as_int(raw.get("leagueId")),
            label=str(raw.get("label", "")),
            sport_id=_as_int(raw.get("sportId")),
            home_team_first=_as_bool(raw.get("homeTeamFirst"), True),
        )
    except Exception as exc:
        logger.warning("failed to parse league: %s", exc)
        return None


def parse_market(raw: dict[str, Any]) -> SXMarket | None:
    try:
        return SXMarket(
            market_hash=str(raw.get("marketHash", "")),
            status=str(raw.get("status", "")),
            outcome_one_name=str(raw.get("outcomeOneName", "")),
            outcome_two_name=str(raw.get("outcomeTwoName", "")),
            outcome_void_name=str(raw.get("outcomeVoidName", "")),
            team_one_name=str(raw.get("teamOneName", "")),
            team_two_name=str(raw.get("teamTwoName", "")),
            market_type=_as_int(raw.get("type")),
            game_time=_as_int(raw.get("gameTime")),
            sport_id=_as_int(raw.get("sportId")),
            sport_label=str(raw.get("sportLabel", "")),
            league_id=_as_int(raw.get("leagueId")),
            league_label=str(raw.get("leagueLabel", "")),
            home_team_first=_as_bool(raw.get("homeTeamFirst")),
            live_enabled=_as_bool(raw.get("liveEnabled")),
            group1=str(raw.get("group1", "")),
            raw=raw,
        )
    except Exception as exc:
        logger.warning("failed to parse market %s: %s", raw.get("marketHash"), exc)
        return None


def parse_order(raw: dict[str, Any]) -> SXOrder | None:
    try:
        return SXOrder(
            order_hash=str(raw.get("orderHash", "")),
            market_hash=str(raw.get("marketHash", "")),
            maker=str(raw.get("maker", "")),
            total_bet_size=_as_float(raw.get("totalBetSize")),
            fill_amount=_as_float(raw.get("fillAmount")),
            implied_odds=_as_float(raw.get("percentageOdds")),
            is_maker_betting_outcome_one=_as_bool(raw.get("isMakerBettingOutcomeOne")),
        )
    except Exception as exc:
        logger.warning("failed to parse order: %s", exc)
        return None


def aggregate_odds(market_hash: str, orders: list[SXOrder]) -> SXOdds:
    """Aggregate orders into best odds for each outcome."""
    best_one: float | None = None
    best_two: float | None = None
    size_one = 0.0
    size_two = 0.0

    for o in orders:
        if o.remaining_size <= 0:
            continue
        if o.is_maker_betting_outcome_one:
            # maker is betting outcome 1 → taker gets outcome 2
            # taker pays (1 - implied_odds) for outcome 2
            taker_price = 1.0 - o.implied_odds
            if best_two is None or taker_price < best_two:
                best_two = taker_price
            size_two += o.remaining_size
        else:
            # maker is betting outcome 2 → taker gets outcome 1
            taker_price = 1.0 - o.implied_odds
            if best_one is None or taker_price < best_one:
                best_one = taker_price
            size_one += o.remaining_size

    return SXOdds(
        market_hash=market_hash,
        outcome_one_best=best_one,
        outcome_two_best=best_two,
        outcome_one_size=size_one,
        outcome_two_size=size_two,
        orders=orders,
    )


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class SXBetClient:
    """Async read-only client for SX Bet API.

    Use as an async context manager:
        async with SXBetClient() as client:
            sports = await client.get_sports()
    """

    def __init__(
        self,
        *,
        base_url: str = SX_API_URL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._sem = asyncio.Semaphore(max_concurrency)
        self._user_agent = user_agent
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> SXBetClient:
        self._client = httpx.AsyncClient(
            http2=True,
            timeout=self._timeout,
            headers={"User-Agent": self._user_agent},
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _ensure_open(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("SXBetClient must be used inside async with context")
        return self._client

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET with retry + semaphore."""
        client = self._ensure_open()
        url = f"{self._base_url}{path}"

        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            async with self._sem:
                try:
                    resp = await client.get(url, params=params)
                except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout,
                        httpx.PoolTimeout, httpx.ConnectTimeout) as exc:
                    last_exc = exc
                    if attempt < self._max_retries - 1:
                        wait = RETRY_BACKOFF_SEC[min(attempt, len(RETRY_BACKOFF_SEC) - 1)]
                        logger.debug("retry %d for %s: %s (wait %.1fs)", attempt + 1, url, exc, wait)
                        await asyncio.sleep(wait)
                        continue
                    raise SXBetNetworkError(f"failed after {self._max_retries} retries: {exc}") from exc

                if resp.status_code >= 500:
                    last_exc = SXBetAPIError(resp.status_code, url, resp.text)
                    if attempt < self._max_retries - 1:
                        wait = RETRY_BACKOFF_SEC[min(attempt, len(RETRY_BACKOFF_SEC) - 1)]
                        logger.debug("retry %d for %s: HTTP %d (wait %.1fs)", attempt + 1, url, resp.status_code, wait)
                        await asyncio.sleep(wait)
                        continue
                    raise last_exc

                if resp.status_code >= 400:
                    raise SXBetAPIError(resp.status_code, url, resp.text)

                try:
                    return resp.json()
                except Exception as exc:
                    raise SXBetParseError(f"invalid JSON from {url}: {exc}") from exc

        raise SXBetNetworkError(f"exhausted retries for {url}")

    # ── Public API ──────────────────────────────────────────────────────────

    async def get_sports(self) -> list[Sport]:
        """Fetch all sports."""
        data = await self._get("/sports")
        raw_list = data.get("data", []) if isinstance(data, dict) else []
        return [s for raw in raw_list if (s := parse_sport(raw)) is not None]

    async def get_leagues(self, *, active_only: bool = True) -> list[League]:
        """Fetch leagues (active by default)."""
        path = "/leagues/active" if active_only else "/leagues"
        data = await self._get(path)
        raw_list = data.get("data", []) if isinstance(data, dict) else []
        return [lg for raw in raw_list if (lg := parse_league(raw)) is not None]

    async def get_active_markets(
        self,
        *,
        sport_id: int | None = None,
        league_id: int | None = None,
    ) -> list[SXMarket]:
        """Fetch active markets, optionally filtered by sport or league."""
        params: dict[str, Any] = {}
        if sport_id is not None:
            params["sportId"] = sport_id
        if league_id is not None:
            params["leagueId"] = league_id

        data = await self._get("/markets/active", params=params or None)

        # response: {"status": "success", "data": {"markets": [...]}}
        inner = data.get("data", {}) if isinstance(data, dict) else {}
        raw_list = inner.get("markets", []) if isinstance(inner, dict) else []
        return [m for raw in raw_list if (m := parse_market(raw)) is not None]

    async def get_odds(
        self,
        market_hashes: list[str],
        *,
        base_token: str = SX_USDC_ADDRESS,
    ) -> dict[str, SXOdds]:
        """Fetch best odds for given markets.

        Tries /orders/odds/best first (pre-aggregated), falls back to /orders.
        Returns {market_hash: SXOdds}.
        """
        if not market_hashes:
            return {}

        chunk_size = 20
        all_odds: dict[str, SXOdds] = {}

        for i in range(0, len(market_hashes), chunk_size):
            chunk = market_hashes[i:i + chunk_size]
            params: dict[str, Any] = {
                "marketHashes": ",".join(chunk),
                "baseToken": base_token,
            }

            # try /orders/odds/best first (returns pre-aggregated best odds)
            try:
                data = await self._get("/orders/odds/best", params=params)
                all_odds.update(self._parse_best_odds(chunk, data))
                continue
            except SXBetAPIError as exc:
                if exc.status_code == 404:
                    logger.debug("/orders/odds/best returned 404, trying /orders")
                else:
                    raise

            # fallback: /orders with raw order list
            try:
                data = await self._get("/orders", params=params)
                all_odds.update(self._parse_raw_orders(chunk, data))
            except SXBetAPIError as exc:
                if exc.status_code == 404:
                    logger.warning("both /orders/odds/best and /orders returned 404")
                    for mh in chunk:
                        all_odds[mh] = SXOdds(market_hash=mh)
                else:
                    raise

        return all_odds

    async def get_odds_by_league(
        self,
        league_id: int,
        *,
        base_token: str = SX_USDC_ADDRESS,
    ) -> dict[str, SXOdds]:
        """Fetch best odds for all markets in a league."""
        params: dict[str, Any] = {
            "leagueId": league_id,
            "baseToken": base_token,
        }

        # try /orders/odds/best first
        try:
            data = await self._get("/orders/odds/best", params=params)
            return self._parse_best_odds([], data)
        except SXBetAPIError as exc:
            if exc.status_code != 404:
                raise

        # fallback: /orders
        try:
            data = await self._get("/orders", params=params)
            return self._parse_raw_orders([], data)
        except SXBetAPIError as exc:
            if exc.status_code == 404:
                logger.warning("no orders endpoint available for league %d", league_id)
                return {}
            raise

    def _parse_best_odds(self, expected_hashes: list[str], data: Any) -> dict[str, SXOdds]:
        """Parse response from /orders/odds/best endpoint."""
        result: dict[str, SXOdds] = {}
        inner = data.get("data", {}) if isinstance(data, dict) else {}

        # response may be a dict keyed by marketHash, or a list
        if isinstance(inner, dict):
            for mh, odds_data in inner.items():
                if isinstance(odds_data, dict):
                    result[mh] = SXOdds(
                        market_hash=mh,
                        outcome_one_best=_as_float(odds_data.get("outcomeOne")) or None,
                        outcome_two_best=_as_float(odds_data.get("outcomeTwo")) or None,
                    )
        elif isinstance(inner, list):
            for item in inner:
                if isinstance(item, dict):
                    mh = str(item.get("marketHash", ""))
                    if mh:
                        result[mh] = SXOdds(
                            market_hash=mh,
                            outcome_one_best=_as_float(item.get("outcomeOne")) or None,
                            outcome_two_best=_as_float(item.get("outcomeTwo")) or None,
                        )

        # ensure all expected hashes have entries
        for mh in expected_hashes:
            if mh not in result:
                result[mh] = SXOdds(market_hash=mh)

        return result

    def _parse_raw_orders(self, expected_hashes: list[str], data: Any) -> dict[str, SXOdds]:
        """Parse response from /orders endpoint (raw order list)."""
        inner = data.get("data", {}) if isinstance(data, dict) else {}
        raw_orders = inner if isinstance(inner, list) else inner.get("orders", [])
        if not isinstance(raw_orders, list):
            raw_orders = []

        orders_by_market: dict[str, list[SXOrder]] = {}
        for raw in raw_orders:
            order = parse_order(raw)
            if order and order.remaining_size > 0:
                orders_by_market.setdefault(order.market_hash, []).append(order)

        result = {
            mh: aggregate_odds(mh, orders)
            for mh, orders in orders_by_market.items()
        }

        for mh in expected_hashes:
            if mh not in result:
                result[mh] = SXOdds(market_hash=mh)

        return result


# ---------------------------------------------------------------------------
# CLI demo
# ---------------------------------------------------------------------------


async def _demo() -> None:
    """Quick connectivity + data shape test."""
    async with SXBetClient() as client:
        # 1. Sports
        print("=== SX Bet Connectivity Test ===\n")
        sports = await client.get_sports()
        print(f"Sports ({len(sports)}):")
        for s in sports:
            print(f"  [{s.sport_id}] {s.label}")

        # 2. Active leagues
        leagues = await client.get_leagues()
        print(f"\nActive leagues ({len(leagues)}):")
        soccer_leagues = [lg for lg in leagues if lg.sport_id == 5]  # soccer
        for lg in soccer_leagues[:10]:
            print(f"  [{lg.league_id}] {lg.label}")
        if len(soccer_leagues) > 10:
            print(f"  ... and {len(soccer_leagues) - 10} more soccer leagues")

        # 3. Active markets (soccer)
        if soccer_leagues:
            league = soccer_leagues[0]
            markets = await client.get_active_markets(league_id=league.league_id)
            # filter to moneyline (type=1) only
            moneyline = [m for m in markets if m.market_type == 1]
            print(f"\nMoneyline markets in {league.label}: {len(moneyline)}")

            if moneyline:
                # 4. Get odds for first few
                sample = moneyline[:5]
                hashes = [m.market_hash for m in sample]
                odds_map = await client.get_odds(hashes)

                print(f"\nOdds sample:")
                for m in sample:
                    odds = odds_map.get(m.market_hash)
                    o1 = f"{odds.outcome_one_best:.3f}" if odds and odds.outcome_one_best else "n/a"
                    o2 = f"{odds.outcome_two_best:.3f}" if odds and odds.outcome_two_best else "n/a"
                    print(f"  {m.team_one_name} vs {m.team_two_name}")
                    print(f"    {m.outcome_one_name}={o1}  {m.outcome_two_name}={o2}")

        print("\n=== SX Bet connectivity OK ===")


def main() -> int:
    import sys
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(_demo())
        return 0
    except SXBetError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
