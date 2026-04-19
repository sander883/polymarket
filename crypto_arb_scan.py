"""Crypto latency arb scanner: Polymarket BTC markets vs Binance spot price.

Strategy: Polymarket crypto markets resolve using Binance BTC/USDT 1-min
candle data. When BTC moves sharply on Binance, Polymarket odds lag by
30-90 seconds. This script:

  1. Fetches current BTC/USDT price from Binance (via ccxt REST)
  2. Fetches active Polymarket crypto markets (BTC price targets)
  3. Parses strike prices from question text
  4. Compares: is Polymarket still pricing as if BTC hasn't moved?
  5. Flags mispriced contracts where edge > threshold

Market question format examples:
  "Will BTC be above $85,000 at 10:00 AM ET?"       (hourly)
  "Will BTC be above $84,500 on April 13?"           (daily)
  "What price will Bitcoin hit in April?"             (monthly, multi-outcome)

Usage:
  python crypto_arb_scan.py                # one-shot scan
  python crypto_arb_scan.py --loop 5       # continuous, every 5 seconds
  python crypto_arb_scan.py -v             # verbose
"""

from __future__ import annotations

import asyncio
import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import ccxt

from polymarket_client import (
    Market,
    OrderBook,
    PolymarketClient,
    PolymarketError,
)

logger = logging.getLogger(__name__)

# Regex patterns for extracting strike prices from Polymarket questions
# "Will BTC be above $85,000 at ..."
# "Will Bitcoin reach $150,000 in April?"
# "Bitcoin above $84k?"
STRIKE_PATTERN = re.compile(
    r"(?:BTC|Bitcoin).*?(?:above|below|over|under|reach|hit|exceed)\s*\$?([\d,]+(?:\.\d+)?(?:k|K)?)",
    re.IGNORECASE,
)

# Direction: above/over/reach/hit = bullish, below/under = bearish
DIRECTION_PATTERN = re.compile(r"\b(above|over|below|under|reach|hit|exceed)\b", re.IGNORECASE)

# Questions that match STRIKE_PATTERN but aren't BTC *price* markets
# e.g. "Bitcoin realized volatility index hit 70", "Bitcoin dominance above 60%"
NON_PRICE_KEYWORDS = re.compile(
    r"\b(volatility|dominance|market\s*cap|hash\s*rate|difficulty|"
    r"fear\s*(?:and|&)\s*greed|index|sentiment|etf\s*flow)\b",
    re.IGNORECASE,
)

# "hit $60k or $80k first?" / "$X or $Y" — multi-outcome "which first" markets
# These are NOT standard above/below markets, must be excluded
WHICH_FIRST_PATTERN = re.compile(
    r"\$[\d,]+k?\s+or\s+\$[\d,]+k?\s+first",
    re.IGNORECASE,
)

# Minimum plausible BTC strike price — anything below this is not a price market
MIN_BTC_STRIKE = 10_000

# Signals are tiered into two logs:
#   signals.log   — "actionable": edge >= ACTIONABLE_EDGE_PCT AND size >= ACTIONABLE_SIZE
#   near_miss.log — everything else with a positive edge (diagnostic data)
# Hard floor below: signals with size < MIN_SIGNAL_SIZE are dropped entirely
# (they're almost certainly already-filled tails, not real edges).
MIN_SIGNAL_SIZE = 10
ACTIONABLE_EDGE_PCT = 3.0
ACTIONABLE_SIZE = 30

# ---------------------------------------------------------------------------
# Expiry / time-to-resolution parsing
# ---------------------------------------------------------------------------

# Patterns for extracting resolution time from question text
# "... at 10:00 AM ET?"  or  "... on April 13, 7PM ET?"  or  "... April 13, 10PM ET?"
# Matches time with optional "at" — real questions use both formats
TIME_PATTERN = re.compile(
    r"(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\s*ET",
    re.IGNORECASE,
)

# "... on April 13?"  or  "... on 2026-04-13?"
DATE_PATTERN = re.compile(
    r"on\s+(?:(\w+)\s+(\d{1,2})(?:,?\s*(\d{4}))?|(\d{4})-(\d{2})-(\d{2}))",
    re.IGNORECASE,
)

# "... by April 30?"  "... by December 31, 2026?"
BY_DATE_PATTERN = re.compile(
    r"by\s+(?:(\w+)\s+(\d{1,2})(?:,?\s*(\d{4}))?|(\d{4})-(\d{2})-(\d{2}))",
    re.IGNORECASE,
)

# "... April 13-19?"  (date range = multi-day market)
DATE_RANGE_PATTERN = re.compile(
    r"(\w+)\s+(\d{1,2})\s*-\s*(\d{1,2})(?:,?\s*(\d{4}))?",
    re.IGNORECASE,
)

# "... in April?"  "... in April 2026?"
_MONTH_NAMES = "january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec"
IN_PERIOD_PATTERN = re.compile(
    rf"in\s+({_MONTH_NAMES})(?:\s+(\d{{4}}))?",
    re.IGNORECASE,
)

MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_expiry(question: str) -> datetime | None:
    """Try to extract the resolution/expiry datetime from a question.

    Returns a UTC datetime, or None if unparseable.

    Checks date ranges first (e.g. "April 13-19" = end of day April 19).
    ET (Eastern Time) is assumed UTC-4 (EDT) for simplicity.
    """
    now = datetime.now(timezone.utc)

    # Check date range first: "April 13-19" → end of day April 19
    range_m = DATE_RANGE_PATTERN.search(question)
    if range_m:
        month_name = range_m.group(1).lower()
        month = MONTH_MAP.get(month_name)
        if month is not None:
            end_day = int(range_m.group(3))
            year = int(range_m.group(4)) if range_m.group(4) else now.year
            try:
                return datetime(year, month, end_day, 23, 59, tzinfo=timezone.utc) + timedelta(hours=4)
            except ValueError:
                pass

    # Try "at HH:MM AM/PM ET" (intraday markets — the latency arb targets)
    time_m = TIME_PATTERN.search(question)
    date_m = DATE_PATTERN.search(question)

    if time_m:
        hour = int(time_m.group(1))
        minute = int(time_m.group(2) or "0")
        ampm = time_m.group(3).upper()
        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0
        # ET -> UTC (EDT = UTC-4)
        utc_hour = hour + 4

        # figure out the date
        if date_m:
            if date_m.group(4):  # ISO format
                year = int(date_m.group(4))
                month = int(date_m.group(5))
                day = int(date_m.group(6))
            else:
                month_name = date_m.group(1).lower()
                month = MONTH_MAP.get(month_name, now.month)
                day = int(date_m.group(2))
                year = int(date_m.group(3)) if date_m.group(3) else now.year
        else:
            # time but no date — assume today
            year, month, day = now.year, now.month, now.day

        try:
            expiry = datetime(year, month, day, tzinfo=timezone.utc) + timedelta(hours=utc_hour, minutes=minute)
            return expiry
        except ValueError:
            pass

    # "on April 13" (daily market, no specific time — assume end of day ET = 23:59 ET = 03:59 UTC next day)
    if date_m and not time_m:
        if date_m.group(4):  # ISO
            year = int(date_m.group(4))
            month = int(date_m.group(5))
            day = int(date_m.group(6))
        else:
            month_name = date_m.group(1).lower()
            month = MONTH_MAP.get(month_name, now.month)
            day = int(date_m.group(2))
            year = int(date_m.group(3)) if date_m.group(3) else now.year
        try:
            # end of day ET ≈ 04:00 UTC next day
            return datetime(year, month, day, 23, 59, tzinfo=timezone.utc) + timedelta(hours=4)
        except ValueError:
            pass

    # "by December 31, 2026" — long-dated
    by_m = BY_DATE_PATTERN.search(question)
    if by_m:
        if by_m.group(4):
            year = int(by_m.group(4))
            month = int(by_m.group(5))
            day = int(by_m.group(6))
        else:
            month_name = by_m.group(1).lower()
            month = MONTH_MAP.get(month_name, now.month)
            day = int(by_m.group(2))
            year = int(by_m.group(3)) if by_m.group(3) else now.year
        try:
            return datetime(year, month, day, 23, 59, tzinfo=timezone.utc) + timedelta(hours=4)
        except ValueError:
            pass

    # "in April" — end of month
    in_m = IN_PERIOD_PATTERN.search(question)
    if in_m:
        month_name = in_m.group(1).lower()
        month = MONTH_MAP.get(month_name)
        if month:
            year = int(in_m.group(2)) if in_m.group(2) else now.year
            # last day of month
            if month == 12:
                expiry = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
            else:
                expiry = datetime(year, month + 1, 1, tzinfo=timezone.utc)
            return expiry

    return None


def hours_to_expiry(question: str) -> float | None:
    """Return hours until market resolution, or None if unparseable."""
    expiry = parse_expiry(question)
    if expiry is None:
        return None
    now = datetime.now(timezone.utc)
    delta = (expiry - now).total_seconds() / 3600
    return delta


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class CryptoMarket:
    """A Polymarket crypto price market with parsed metadata."""

    market: Market
    strike_price: float
    direction: str  # "above" or "below"
    yes_ask: float  # current YES ask price on Polymarket
    no_ask: float   # current NO ask price on Polymarket
    yes_bid: float
    no_bid: float
    yes_ask_size: float
    no_ask_size: float
    hours_to_expiry: float | None = None  # None = couldn't parse


@dataclass
class ArbSignal:
    """A detected mispricing between Binance spot and Polymarket."""

    crypto_market: CryptoMarket
    binance_price: float
    fair_value: str  # "YES should be high" or "YES should be low"
    poly_yes_ask: float
    poly_no_ask: float
    edge_description: str
    edge_pct: float


# ---------------------------------------------------------------------------
# Binance price feed
# ---------------------------------------------------------------------------


def get_binance_btc_price() -> float:
    """Fetch current BTC/USDT price from Binance via ccxt (REST)."""
    exchange = ccxt.binance({"enableRateLimit": True})
    ticker = exchange.fetch_ticker("BTC/USDT")
    return float(ticker["last"])


def get_binance_btc_price_at(ts_ms: int) -> float | None:
    """Fetch the BTC/USDT price at a specific timestamp using 1-min klines.

    Returns the open price of the 1-min candle that contains the timestamp.
    This is the reference price for Up/Down market resolution.
    """
    try:
        exchange = ccxt.binance({"enableRateLimit": True})
        # fetch 1 candle starting at the given timestamp
        ohlcv = exchange.fetch_ohlcv("BTC/USDT", "1m", since=ts_ms, limit=1)
        if ohlcv and len(ohlcv) > 0:
            # [timestamp, open, high, low, close, volume]
            return float(ohlcv[0][1])  # open price
    except Exception as exc:
        logger.warning("failed to fetch historical kline at %s: %s", ts_ms, exc)
    return None


# ---------------------------------------------------------------------------
# Polymarket crypto market parser
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Up/Down market parser
# ---------------------------------------------------------------------------

# "BTC Up or Down - April 13, 4:00 AM ET"
# "Bitcoin 5 Minute Up or Down - 10:30 AM ET"
# "Bitcoin Up or Down on April 14?"
UP_DOWN_PATTERN = re.compile(
    r"(?:BTC|Bitcoin)\s+(?:(\d+)\s*(?:Minute|Min)\s+)?Up\s+(?:or|and)\s+Down",
    re.IGNORECASE,
)

# Range time format: "8:00PM-12:00AM ET" or "8:45PM-9:00PM ET"
UP_DOWN_RANGE_PATTERN = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\s*-\s*(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\s*ET",
    re.IGNORECASE,
)

# Single time format: "9PM ET" or "4:00 AM ET"
UP_DOWN_TIME_PATTERN = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\s*ET",
    re.IGNORECASE,
)

# Extract date from Up/Down question: "April 13" or "on April 14"
UP_DOWN_DATE_PATTERN = re.compile(
    r"(?:(\w+)\s+(\d{1,2})(?:,?\s*(\d{4}))?)",
    re.IGNORECASE,
)


def _et_to_utc_hour_min(hour: int, minute: int, ampm: str) -> tuple[int, int]:
    """Convert ET hour/minute/AMPM to UTC hour/minute."""
    if ampm == "PM" and hour != 12:
        hour += 12
    elif ampm == "AM" and hour == 12:
        hour = 0
    # EDT = UTC-4
    utc_hour = hour + 4
    return utc_hour, minute


@dataclass
class UpDownInfo:
    """Parsed info for an Up/Down market."""
    window_minutes: int          # 5, 15, 60, 240, etc.
    window_start_utc: datetime   # start of the resolution window
    window_end_utc: datetime     # end of the resolution window
    open_price: float | None     # BTC price at window start (fetched from Binance)
    minutes_elapsed: float       # how many minutes into the window we are


def parse_up_down(question: str) -> UpDownInfo | None:
    """Parse an Up/Down market question into window timing.

    Handles multiple formats:
      "Bitcoin Up or Down - April 13, 9PM ET"           → hourly (9PM-10PM)
      "Bitcoin Up or Down - April 13, 8:00PM-12:00AM ET" → range (4 hours)
      "Bitcoin Up or Down - April 13, 8:45PM-9:00PM ET"  → range (15 min)
      "BTC 5 Minute Up or Down - April 13, 4:00 AM ET"   → explicit 5 min
      "Bitcoin Up or Down on April 14?"                   → daily (skip)

    Returns UpDownInfo or None if not parseable.
    """
    ud_match = UP_DOWN_PATTERN.search(question)
    if not ud_match:
        return None

    # Explicit duration in question text: "5 Minute", "15 Minute"
    explicit_duration = ud_match.group(1)

    now = datetime.now(timezone.utc)

    # Parse the date — find a match with a valid month name
    year, month, day = now.year, now.month, now.day
    for date_m in UP_DOWN_DATE_PATTERN.finditer(question):
        month_name = date_m.group(1).lower()
        parsed_month = MONTH_MAP.get(month_name)
        if parsed_month is not None:
            month = parsed_month
            day = int(date_m.group(2))
            year = int(date_m.group(3)) if date_m.group(3) else now.year
            break

    # Try range format first: "8:00PM-12:00AM ET"
    range_m = UP_DOWN_RANGE_PATTERN.search(question)
    if range_m:
        start_h, start_min = _et_to_utc_hour_min(
            int(range_m.group(1)), int(range_m.group(2) or "0"), range_m.group(3).upper()
        )
        end_h, end_min = _et_to_utc_hour_min(
            int(range_m.group(4)), int(range_m.group(5) or "0"), range_m.group(6).upper()
        )
        try:
            window_start = datetime(year, month, day, tzinfo=timezone.utc) + timedelta(hours=start_h, minutes=start_min)
            window_end = datetime(year, month, day, tzinfo=timezone.utc) + timedelta(hours=end_h, minutes=end_min)
            # handle midnight crossover (e.g. 8PM-12AM)
            if window_end <= window_start:
                window_end += timedelta(days=1)
            window_minutes = int((window_end - window_start).total_seconds() / 60)
        except ValueError:
            return None

        minutes_elapsed = (now - window_start).total_seconds() / 60
        return UpDownInfo(
            window_minutes=window_minutes,
            window_start_utc=window_start,
            window_end_utc=window_end,
            open_price=None,
            minutes_elapsed=minutes_elapsed,
        )

    # Try single time format: "9PM ET" or "4:00 AM ET"
    time_m = UP_DOWN_TIME_PATTERN.search(question)
    if time_m:
        utc_hour, utc_min = _et_to_utc_hour_min(
            int(time_m.group(1)), int(time_m.group(2) or "0"), time_m.group(3).upper()
        )

        if explicit_duration:
            window_minutes = int(explicit_duration)
        else:
            # No explicit duration + single time = hourly window
            window_minutes = 60

        try:
            window_start = datetime(year, month, day, tzinfo=timezone.utc) + timedelta(hours=utc_hour, minutes=utc_min)
            window_end = window_start + timedelta(minutes=window_minutes)
        except ValueError:
            return None

        minutes_elapsed = (now - window_start).total_seconds() / 60
        return UpDownInfo(
            window_minutes=window_minutes,
            window_start_utc=window_start,
            window_end_utc=window_end,
            open_price=None,
            minutes_elapsed=minutes_elapsed,
        )

    # "Bitcoin Up or Down on April 14?" — daily, no time specified
    # These are too long for latency arb, return None to skip
    return None


# ---------------------------------------------------------------------------
# Strike price parser
# ---------------------------------------------------------------------------


def parse_strike(question: str) -> tuple[float, str] | None:
    """Extract strike price and direction from a Polymarket question.

    Returns (strike_price, direction) or None if not parseable.
    Filters out non-price markets (volatility index, dominance, etc.).
    """
    # Reject non-price markets before even trying to parse
    if NON_PRICE_KEYWORDS.search(question):
        return None

    # Reject "which first" markets: "hit $60k or $80k first?"
    if WHICH_FIRST_PATTERN.search(question):
        return None

    strike_match = STRIKE_PATTERN.search(question)
    if not strike_match:
        return None

    try:
        raw_strike = strike_match.group(1).replace(",", "")
        if raw_strike.lower().endswith("k"):
            strike = float(raw_strike[:-1]) * 1000
        else:
            strike = float(raw_strike)
    except ValueError:
        return None

    # Reject implausibly low strikes (volatility=70, dominance=60%, etc.)
    if strike < MIN_BTC_STRIKE:
        return None

    dir_match = DIRECTION_PATTERN.search(question)
    if dir_match:
        word = dir_match.group(1).lower()
        direction = "above" if word in ("above", "over", "reach", "hit", "exceed") else "below"
    else:
        direction = "above"  # default assumption

    return strike, direction


async def fetch_crypto_markets(
    client: PolymarketClient,
    *,
    verbose: bool = False,
) -> list[tuple[Market, float, str]]:
    """Fetch Polymarket markets related to BTC price targets.

    Uses the Gamma /events?tag_id=21 endpoint to discover crypto markets
    directly, instead of searching the top-200 by liquidity (which is
    dominated by politics/sports and misses most crypto markets).

    Returns list of (Market, strike_price, direction).
    """
    # Primary: use tag_id=21 (Crypto) via /events endpoint
    # This returns event groups, each containing multiple BTC price markets
    markets = await client.get_events_markets(tag_id=21, limit=200, min_liquidity=0)

    # Fallback: also check /markets with tag_id filter
    if not markets:
        markets = await client.get_markets(limit=500, min_liquidity=0, tag_id=21)

    # If tag_id filtering didn't work, fall back to keyword search
    if not markets:
        if verbose:
            print("  tag_id=21 returned 0 markets, falling back to keyword search")
        markets = await client.get_markets(limit=200, min_liquidity=100)
        crypto_patterns = [
            re.compile(r"\bbtc\b", re.IGNORECASE),
            re.compile(r"\bbitcoin\b", re.IGNORECASE),
            re.compile(r"\bcrypto\b", re.IGNORECASE),
            re.compile(r"\beth\b", re.IGNORECASE),
            re.compile(r"\bethereum\b", re.IGNORECASE),
            re.compile(r"\bsol\b", re.IGNORECASE),
            re.compile(r"\bsolana\b", re.IGNORECASE),
        ]
        markets = [m for m in markets if any(p.search(m.question) for p in crypto_patterns)]

    if verbose:
        print(f"  Crypto markets from Gamma: {len(markets)}")
        # Show sample of BTC-related questions
        btc_markets = [m for m in markets if "btc" in m.question.lower() or "bitcoin" in m.question.lower()]
        print(f"  BTC-specific: {len(btc_markets)}")
        for m in btc_markets[:20]:
            parsed = parse_strike(m.question)
            tag = f" -> strike=${parsed[0]:,.0f} {parsed[1]}" if parsed else " -> NO PARSE"
            print(f"    {m.question[:70]}{tag}")
        if not btc_markets and markets:
            print(f"  No BTC markets. Sample of {len(markets)} crypto markets:")
            for m in markets[:15]:
                print(f"    [{m.category}] {m.question[:70]}")

    # Parse strike prices (BTC only for now)
    crypto: list[tuple[Market, float, str]] = []
    for m in markets:
        q = m.question.lower()
        if "btc" not in q and "bitcoin" not in q:
            continue
        parsed = parse_strike(m.question)
        if parsed:
            crypto.append((m, parsed[0], parsed[1]))

    # "Up or Down" markets (no strike, direction-only)
    up_down_pattern = re.compile(r"(?:BTC|Bitcoin)\s+Up or Down", re.IGNORECASE)
    seen_ids = {c[0].market_id for c in crypto}
    for m in markets:
        if up_down_pattern.search(m.question) and m.market_id not in seen_ids:
            crypto.append((m, 0.0, "up_or_down"))

    return crypto


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


async def scan_once(*, verbose: bool = False) -> list[ArbSignal]:
    """Run one scan cycle: fetch Binance price + Polymarket odds, compare."""

    # 1. Binance price
    t0 = time.time()
    btc_price = get_binance_btc_price()
    binance_ms = (time.time() - t0) * 1000
    print(f"  Binance BTC/USDT: ${btc_price:,.2f} ({binance_ms:.0f}ms)")

    # 2. Polymarket crypto markets
    async with PolymarketClient() as client:
        t0 = time.time()
        raw_markets = await fetch_crypto_markets(client, verbose=verbose)
        poly_ms = (time.time() - t0) * 1000
        print(f"  Polymarket BTC markets: {len(raw_markets)} found ({poly_ms:.0f}ms)")

        if not raw_markets:
            print("  No BTC price-target markets found on Polymarket.")
            return []

        # 3. Fetch orderbooks
        token_ids = []
        for m, _, _ in raw_markets:
            if m.yes_token_id:
                token_ids.append(m.yes_token_id)
            if m.no_token_id:
                token_ids.append(m.no_token_id)

        t0 = time.time()
        books = await client.get_orderbooks(token_ids)
        book_ms = (time.time() - t0) * 1000
        print(f"  Orderbooks fetched: {len(books)} ({book_ms:.0f}ms)")

    # 4. Build CryptoMarket objects
    crypto_markets: list[CryptoMarket] = []
    for m, strike, direction in raw_markets:
        yes_book = books.get(m.yes_token_id) if m.yes_token_id else None
        no_book = books.get(m.no_token_id) if m.no_token_id else None

        yes_ask = yes_book.best_ask.price if yes_book and yes_book.best_ask else 0.0
        no_ask = no_book.best_ask.price if no_book and no_book.best_ask else 0.0
        yes_bid = yes_book.best_bid.price if yes_book and yes_book.best_bid else 0.0
        no_bid = no_book.best_bid.price if no_book and no_book.best_bid else 0.0
        yes_ask_size = yes_book.best_ask.size if yes_book and yes_book.best_ask else 0.0
        no_ask_size = no_book.best_ask.size if no_book and no_book.best_ask else 0.0

        hte = hours_to_expiry(m.question)

        if yes_ask > 0 or no_ask > 0:
            crypto_markets.append(CryptoMarket(
                market=m,
                strike_price=strike,
                direction=direction,
                yes_ask=yes_ask,
                no_ask=no_ask,
                yes_bid=yes_bid,
                no_bid=no_bid,
                yes_ask_size=yes_ask_size,
                no_ask_size=no_ask_size,
                hours_to_expiry=hte,
            ))

    print(f"  Markets with orderbook: {len(crypto_markets)}")

    # Classify by expiry
    near_expiry = [cm for cm in crypto_markets if cm.hours_to_expiry is not None and cm.hours_to_expiry <= 6]
    today_expiry = [cm for cm in crypto_markets if cm.hours_to_expiry is not None and 6 < cm.hours_to_expiry <= 24]
    far_expiry = [cm for cm in crypto_markets if cm.hours_to_expiry is None or cm.hours_to_expiry > 24]
    print(f"  Near-expiry (≤6h): {len(near_expiry)}  "
          f"Today (6-24h): {len(today_expiry)}  "
          f"Far (>24h): {len(far_expiry)}")

    # 5. Detect mispricings — TIME-AWARE
    #
    # Latency arb thesis: Polymarket prices lag Binance by 30-90 seconds.
    # This is only exploitable on NEAR-EXPIRY markets where BTC is clearly
    # past the strike and the market should be pricing near 0 or 1.
    #
    # For multi-day/monthly markets, odds reflect genuine uncertainty about
    # future price — NOT a lagging signal. Those are excluded.
    #
    # Thresholds scale with time-to-expiry:
    #   ≤1h:   BTC past strike by 0.3% → fair value ~0.95, very strong signal
    #   1-6h:  BTC past strike by 1.0% → fair value ~0.90, moderate signal
    #   6-24h: BTC past strike by 2.0% → fair value ~0.80, weak signal (show but caveat)
    #   >24h:  SKIP — not latency arb, it's genuine uncertainty

    signals: list[ArbSignal] = []

    for cm in sorted(crypto_markets, key=lambda c: c.strike_price):
        strike = cm.strike_price
        direction = cm.direction
        hte = cm.hours_to_expiry

        if direction == "up_or_down":
            # ── Up/Down market edge detection ──
            # YES = BTC goes UP from window open price
            # NO  = BTC goes DOWN from window open price
            #
            # Edge: if BTC already moved significantly from window open,
            # but Polymarket still pricing near 0.50, that's latency lag.

            ud_info = parse_up_down(cm.market.question)
            if ud_info is None:
                if verbose:
                    print(f"  UD  SKIP (can't parse)  '{cm.market.question[:55]}'")
                continue

            # Only process windows that are currently active
            if ud_info.minutes_elapsed < 0:
                if verbose:
                    print(f"  UD  SKIP (not started, {-ud_info.minutes_elapsed:.0f}m away)  "
                          f"'{cm.market.question[:50]}'")
                continue
            if ud_info.minutes_elapsed > ud_info.window_minutes:
                if verbose:
                    print(f"  UD  SKIP (expired)  '{cm.market.question[:50]}'")
                continue

            # Fetch the BTC open price for this window
            window_start_ms = int(ud_info.window_start_utc.timestamp() * 1000)
            open_price = get_binance_btc_price_at(window_start_ms)
            ud_info.open_price = open_price

            if open_price is None or open_price <= 0:
                if verbose:
                    print(f"  UD  SKIP (no open price)  '{cm.market.question[:50]}'")
                continue

            # Calculate BTC movement from open
            btc_move_pct = (btc_price - open_price) / open_price * 100
            minutes_left = ud_info.window_minutes - ud_info.minutes_elapsed

            if verbose:
                print(f"  UD  {ud_info.window_minutes}min  "
                      f"open=${open_price:,.0f}  now=${btc_price:,.0f}  "
                      f"move={btc_move_pct:+.3f}%  "
                      f"{ud_info.minutes_elapsed:.1f}m in / {minutes_left:.1f}m left  "
                      f"YES={cm.yes_ask:.3f} NO={cm.no_ask:.3f}  "
                      f"'{cm.market.question[:40]}'")

            # Edge detection for Up/Down:
            # Thresholds scale with fraction of window remaining.
            # A move with 10% of window left is much more meaningful
            # than the same move with 80% of window left.
            #
            # fraction_left = minutes_left / window_minutes
            #   <5%  left:  0.03% move enough (nearly resolved)
            #   <15% left:  0.05% move (very strong)
            #   <30% left:  0.10% move (strong)
            #   <50% left:  0.15% move (moderate)
            #   ≥50% left:  0.25% move needed (lots of time for reversal)

            fraction_left = minutes_left / ud_info.window_minutes if ud_info.window_minutes > 0 else 1.0

            if fraction_left <= 0.05:
                move_threshold = 0.03
                fair_winner = 0.95
                confidence = "HIGH"
            elif fraction_left <= 0.15:
                move_threshold = 0.05
                fair_winner = 0.90
                confidence = "HIGH"
            elif fraction_left <= 0.30:
                move_threshold = 0.10
                fair_winner = 0.80
                confidence = "MEDIUM"
            elif fraction_left <= 0.50:
                move_threshold = 0.15
                fair_winner = 0.70
                confidence = "MEDIUM"
            else:
                move_threshold = 0.25
                fair_winner = 0.60
                confidence = "LOW"

            if abs(btc_move_pct) >= move_threshold:
                # BTC has moved enough — determine which side to buy
                window_label = f"{ud_info.window_minutes}min"
                if btc_move_pct > 0:
                    # BTC up → YES should be high
                    if (cm.yes_ask < fair_winner and cm.yes_ask > 0
                            and cm.yes_ask_size >= MIN_SIGNAL_SIZE):
                        edge = fair_winner - cm.yes_ask
                        signals.append(ArbSignal(
                            crypto_market=cm,
                            binance_price=btc_price,
                            fair_value=(f"YES ~{fair_winner:.0%} [{confidence}, {window_label}, "
                                        f"BTC +{btc_move_pct:.3f}% from open, "
                                        f"{minutes_left:.0f}m left]"),
                            poly_yes_ask=cm.yes_ask,
                            poly_no_ask=cm.no_ask,
                            edge_description=(f"buy YES @ {cm.yes_ask:.3f}, "
                                              f"BTC ${btc_price:,.0f} up from open ${open_price:,.0f}"),
                            edge_pct=edge * 100,
                        ))
                else:
                    # BTC down → NO should be high (NO = "down")
                    if (cm.no_ask < fair_winner and cm.no_ask > 0
                            and cm.no_ask_size >= MIN_SIGNAL_SIZE):
                        edge = fair_winner - cm.no_ask
                        signals.append(ArbSignal(
                            crypto_market=cm,
                            binance_price=btc_price,
                            fair_value=(f"NO ~{fair_winner:.0%} [{confidence}, {window_label}, "
                                        f"BTC {btc_move_pct:.3f}% from open, "
                                        f"{minutes_left:.0f}m left]"),
                            poly_yes_ask=cm.yes_ask,
                            poly_no_ask=cm.no_ask,
                            edge_description=(f"buy NO @ {cm.no_ask:.3f}, "
                                              f"BTC ${btc_price:,.0f} down from open ${open_price:,.0f}"),
                            edge_pct=edge * 100,
                        ))

            continue

        if strike <= 0:
            continue

        # Skip markets we can't time — if expiry is unknown, it's likely
        # a special format (weekly, "which first", multi-outcome) that
        # isn't suitable for latency arb
        if hte is None:
            if verbose:
                print(f"  SKIP ${strike:>10,.0f} {direction:>5}  "
                      f"exp=?? (can't parse expiry)  "
                      f"'{cm.market.question[:45]}'")
            continue

        # Skip already-resolved markets
        if hte <= 0:
            if verbose:
                print(f"  SKIP ${strike:>10,.0f} {direction:>5}  "
                      f"exp={hte:.1f}h (already resolved)  "
                      f"'{cm.market.question[:45]}'")
            continue

        # Skip far-out markets — not latency arb candidates
        if hte > 24:
            if verbose:
                print(f"  SKIP ${strike:>10,.0f} {direction:>5}  "
                      f"exp={hte:.0f}h (too far out)  "
                      f"'{cm.market.question[:45]}'")
            continue

        # Determine thresholds based on time-to-expiry
        if hte <= 1:
            # Near-expiry: BTC 0.3% past strike is strong signal
            distance_threshold = 0.003
            fair_value_est = 0.95
            confidence = "HIGH"
        elif hte <= 6:
            distance_threshold = 0.01
            fair_value_est = 0.90
            confidence = "MEDIUM"
        elif hte <= 24:
            distance_threshold = 0.02
            fair_value_est = 0.80
            confidence = "LOW"

        if direction == "above":
            distance_pct = (btc_price - strike) / strike * 100
        else:
            distance_pct = (strike - btc_price) / strike * 100

        hte_str = f"{hte:.1f}h" if hte is not None else "??h"

        if verbose:
            above_strike = btc_price > strike if direction == "above" else btc_price < strike
            tag = ">>>" if above_strike else "   "
            print(f"  {tag} ${strike:>10,.0f} {direction:>5}  "
                  f"YES={cm.yes_ask:.3f} NO={cm.no_ask:.3f}  "
                  f"BTC dist={distance_pct:+.2f}%  exp={hte_str}  "
                  f"'{cm.market.question[:45]}'")

        # Check for mispricings
        if direction == "above":
            if btc_price > strike * (1 + distance_threshold):
                # BTC above strike → YES should be high
                if (cm.yes_ask < fair_value_est and cm.yes_ask > 0
                        and cm.yes_ask_size >= MIN_SIGNAL_SIZE):
                    edge = fair_value_est - cm.yes_ask
                    signals.append(ArbSignal(
                        crypto_market=cm,
                        binance_price=btc_price,
                        fair_value=f"YES ~{fair_value_est:.0%} [{confidence}, exp={hte_str}]",
                        poly_yes_ask=cm.yes_ask,
                        poly_no_ask=cm.no_ask,
                        edge_description=f"buy YES @ {cm.yes_ask:.3f}, BTC ${btc_price:,.0f} > strike ${strike:,.0f}",
                        edge_pct=edge * 100,
                    ))
            elif btc_price < strike * (1 - distance_threshold):
                # BTC below strike → NO should be high
                if (cm.no_ask < fair_value_est and cm.no_ask > 0
                        and cm.no_ask_size >= MIN_SIGNAL_SIZE):
                    edge = fair_value_est - cm.no_ask
                    signals.append(ArbSignal(
                        crypto_market=cm,
                        binance_price=btc_price,
                        fair_value=f"NO ~{fair_value_est:.0%} [{confidence}, exp={hte_str}]",
                        poly_yes_ask=cm.yes_ask,
                        poly_no_ask=cm.no_ask,
                        edge_description=f"buy NO @ {cm.no_ask:.3f}, BTC ${btc_price:,.0f} < strike ${strike:,.0f}",
                        edge_pct=edge * 100,
                    ))

    return signals


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


SIGNAL_LOG = "signals.log"
NEAR_MISS_LOG = "near_miss.log"

# WIB = UTC+7
WIB = timezone(timedelta(hours=7))


def _wib_now() -> str:
    """Current time in WIB (UTC+7) as HH:MM:SS."""
    return datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S WIB")


def _actionable_size(signal: ArbSignal) -> float:
    """Size available on the leg the signal says to buy."""
    cm = signal.crypto_market
    # edge_description always starts with "buy YES" or "buy NO"
    return cm.yes_ask_size if signal.edge_description.startswith("buy YES") else cm.no_ask_size


def log_signal(signal: ArbSignal) -> None:
    """Append a signal to the appropriate log.

    Actionable (edge >= ACTIONABLE_EDGE_PCT AND size >= ACTIONABLE_SIZE)
    lands in signals.log. Everything else goes to near_miss.log for
    diagnostic review — these are real edges but too thin/small to trade.
    """
    ts = _wib_now()
    cm = signal.crypto_market
    if cm.direction == "up_or_down":
        ud_info = parse_up_down(cm.market.question)
        if ud_info is not None:
            minutes_left = ud_info.window_minutes - ud_info.minutes_elapsed
            exp_str = f"UD{ud_info.window_minutes}m {minutes_left:.1f}m-left"
        else:
            exp_str = "UD-??"
    else:
        hte = cm.hours_to_expiry
        exp_str = f"{hte:.1f}h" if hte is not None else "??"

    act_size = _actionable_size(signal)
    is_actionable = (
        signal.edge_pct >= ACTIONABLE_EDGE_PCT and act_size >= ACTIONABLE_SIZE
    )
    tag = "ACT" if is_actionable else "NM "
    target = SIGNAL_LOG if is_actionable else NEAR_MISS_LOG

    line = (
        f"[{ts}] {tag} EDGE={signal.edge_pct:+.1f}% | "
        f"exp={exp_str} | "
        f"BTC=${signal.binance_price:,.0f} | "
        f"{signal.edge_description} | "
        f"sz_yes={cm.yes_ask_size:.0f} sz_no={cm.no_ask_size:.0f} | "
        f"{cm.market.question[:60]}\n"
    )
    try:
        with open(target, "a") as f:
            f.write(line)
    except OSError:
        pass  # don't crash the scanner if file write fails


def print_report(signals: list[ArbSignal], btc_price: float) -> None:
    """Print scan results."""
    ts = _wib_now()
    print(f"\n{'=' * 70}")
    print(f"CRYPTO ARB SCAN — BTC=${btc_price:,.2f} — {ts}")
    print(f"{'=' * 70}")

    if not signals:
        print("  No mispricings detected. Markets in line with Binance.")
        return

    print(f"\n  {len(signals)} potential mispricings found!\n")

    for s in sorted(signals, key=lambda x: -x.edge_pct):
        hte = s.crypto_market.hours_to_expiry
        hte_str = f"{hte:.1f}h" if hte is not None else "unknown"
        print(f"  *** EDGE: {s.edge_pct:+.1f}%  (expiry: {hte_str}) ***")
        print(f"  Market: {s.crypto_market.market.question[:70]}")
        print(f"  Strike: ${s.crypto_market.strike_price:,.0f}  "
              f"Direction: {s.crypto_market.direction}")
        print(f"  Binance: ${s.binance_price:,.2f}")
        print(f"  Poly:    YES_ask={s.poly_yes_ask:.3f} (sz={s.crypto_market.yes_ask_size:.0f})  "
              f"NO_ask={s.poly_no_ask:.3f} (sz={s.crypto_market.no_ask_size:.0f})")
        print(f"  Signal:  {s.fair_value}")
        print(f"  Action:  {s.edge_description}")
        print()

        # Auto-log every signal to file
        log_signal(s)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def run_loop(interval: float, verbose: bool) -> None:
    """Continuous scanning loop."""
    ts = _wib_now()
    print(f"Starting crypto arb scanner (interval={interval}s)")
    print(f"Time: {ts}")
    print(f"Signals auto-logged to: {SIGNAL_LOG}")
    print(f"Press Ctrl-C to stop.\n")

    cycle = 0
    total_signals = 0

    try:
        while True:
            cycle += 1
            ts_wib = datetime.now(WIB).strftime("%H:%M:%S")
            ts_utc = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"[{ts_wib} WIB / {ts_utc} UTC] Cycle {cycle}")

            try:
                signals = await scan_once(verbose=verbose)
                if signals:
                    total_signals += len(signals)
                    btc_price = signals[0].binance_price
                    print_report(signals, btc_price)
                else:
                    if verbose:
                        print(f"  No edge detected.\n")
                    else:
                        print(f"  OK — no edge.\n")
            except Exception as exc:
                logger.warning("scan error: %s", exc)
                print(f"  Error: {exc}\n")

            await asyncio.sleep(interval)

    except KeyboardInterrupt:
        print(f"\n\nStopped at {_wib_now()}. {cycle} cycles, {total_signals} total signals.")
        if total_signals > 0:
            print(f"Review signals: cat {SIGNAL_LOG}")


async def run_once(verbose: bool) -> int:
    """Single scan."""
    print("Crypto arb scan: Polymarket BTC vs Binance\n")

    try:
        signals = await scan_once(verbose=verbose)
        btc_price = get_binance_btc_price()
        print_report(signals, btc_price)
        return 0
    except (PolymarketError, ccxt.BaseError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--loop", type=float, default=0,
                   help="continuous mode: seconds between scans (0=one-shot)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="show all markets, not just mispricings")
    args = p.parse_args()

    # suppress noisy library loggers
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if args.verbose:
        logging.getLogger("crypto_arb_scan").setLevel(logging.DEBUG)
    # keep libraries quiet regardless of -v
    for noisy in ("ccxt", "httpx", "httpcore", "urllib3", "hpack"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.loop > 0:
        return asyncio.run(run_loop(args.loop, args.verbose))
    else:
        return asyncio.run(run_once(args.verbose))


if __name__ == "__main__":
    raise SystemExit(main())
