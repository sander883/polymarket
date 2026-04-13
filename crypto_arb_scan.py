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
from datetime import datetime, timezone

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
# "Will Bitcoin be above $84,500 on ..."
STRIKE_PATTERN = re.compile(
    r"(?:BTC|Bitcoin).*?(?:above|below|over|under)\s*\$?([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)

# Direction: above/over = bullish, below/under = bearish
DIRECTION_PATTERN = re.compile(r"\b(above|over|below|under)\b", re.IGNORECASE)


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


# ---------------------------------------------------------------------------
# Polymarket crypto market parser
# ---------------------------------------------------------------------------


def parse_strike(question: str) -> tuple[float, str] | None:
    """Extract strike price and direction from a Polymarket question.

    Returns (strike_price, direction) or None if not parseable.
    """
    strike_match = STRIKE_PATTERN.search(question)
    if not strike_match:
        return None

    try:
        strike = float(strike_match.group(1).replace(",", ""))
    except ValueError:
        return None

    dir_match = DIRECTION_PATTERN.search(question)
    if dir_match:
        word = dir_match.group(1).lower()
        direction = "above" if word in ("above", "over") else "below"
    else:
        direction = "above"  # default assumption

    return strike, direction


async def fetch_crypto_markets(
    client: PolymarketClient,
) -> list[tuple[Market, float, str]]:
    """Fetch Polymarket markets related to BTC price targets.

    Returns list of (Market, strike_price, direction).
    """
    # fetch markets mentioning BTC/Bitcoin with decent liquidity
    markets = await client.get_markets(limit=200, min_liquidity=500)

    crypto = []
    for m in markets:
        q = m.question.lower()
        if "btc" not in q and "bitcoin" not in q:
            continue
        parsed = parse_strike(m.question)
        if parsed:
            crypto.append((m, parsed[0], parsed[1]))

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
        raw_markets = await fetch_crypto_markets(client)
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
            ))

    print(f"  Markets with orderbook: {len(crypto_markets)}")

    # 5. Detect mispricings
    signals: list[ArbSignal] = []

    for cm in sorted(crypto_markets, key=lambda c: c.strike_price):
        strike = cm.strike_price
        direction = cm.direction

        # determine fair direction
        if direction == "above":
            btc_above = btc_price > strike
            btc_below = btc_price < strike
            distance_pct = (btc_price - strike) / strike * 100
        else:  # "below"
            btc_above = btc_price < strike
            btc_below = btc_price > strike
            distance_pct = (strike - btc_price) / strike * 100

        if verbose:
            tag = ">>>" if btc_above else "   "
            print(f"  {tag} ${strike:>10,.0f} {direction:>5}  "
                  f"YES={cm.yes_ask:.3f} NO={cm.no_ask:.3f}  "
                  f"BTC dist={distance_pct:+.2f}%  "
                  f"'{cm.market.question[:50]}'")

        # Check for mispricings:
        # If BTC is clearly above strike and direction="above":
        #   YES should be high (~0.90+), NO should be low (~0.10-)
        #   If YES_ask is still low → buy YES cheap
        # If BTC is clearly below strike and direction="above":
        #   YES should be low, NO should be high
        #   If NO_ask is still low → buy NO cheap

        if direction == "above":
            if btc_price > strike * 1.005:  # BTC > strike+0.5% → YES should be high
                if cm.yes_ask < 0.85 and cm.yes_ask > 0:
                    edge = 0.95 - cm.yes_ask  # fair ~0.95 for well-above
                    signals.append(ArbSignal(
                        crypto_market=cm,
                        binance_price=btc_price,
                        fair_value="YES should be HIGH (BTC already above strike)",
                        poly_yes_ask=cm.yes_ask,
                        poly_no_ask=cm.no_ask,
                        edge_description=f"buy YES @ {cm.yes_ask:.3f}, BTC ${btc_price:,.0f} > strike ${strike:,.0f}",
                        edge_pct=edge * 100,
                    ))
            elif btc_price < strike * 0.995:  # BTC < strike-0.5% → NO should be high
                if cm.no_ask < 0.85 and cm.no_ask > 0:
                    edge = 0.95 - cm.no_ask
                    signals.append(ArbSignal(
                        crypto_market=cm,
                        binance_price=btc_price,
                        fair_value="NO should be HIGH (BTC already below strike)",
                        poly_yes_ask=cm.yes_ask,
                        poly_no_ask=cm.no_ask,
                        edge_description=f"buy NO @ {cm.no_ask:.3f}, BTC ${btc_price:,.0f} < strike ${strike:,.0f}",
                        edge_pct=edge * 100,
                    ))

    return signals


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def print_report(signals: list[ArbSignal], btc_price: float) -> None:
    """Print scan results."""
    print(f"\n{'=' * 70}")
    print(f"CRYPTO ARB SCAN RESULTS — BTC=${btc_price:,.2f}")
    print(f"{'=' * 70}")

    if not signals:
        print("\n  No mispricings detected.")
        print("  Polymarket prices are in line with current Binance BTC price.")
        print("  This is expected when markets are not moving fast.")
        return

    print(f"\n  {len(signals)} potential mispricings found!\n")

    for s in sorted(signals, key=lambda x: -x.edge_pct):
        print(f"  *** EDGE: {s.edge_pct:+.1f}% ***")
        print(f"  Market: {s.crypto_market.market.question[:70]}")
        print(f"  Strike: ${s.crypto_market.strike_price:,.0f}  "
              f"Direction: {s.crypto_market.direction}")
        print(f"  Binance: ${s.binance_price:,.2f}")
        print(f"  Poly:    YES_ask={s.poly_yes_ask:.3f} (sz={s.crypto_market.yes_ask_size:.0f})  "
              f"NO_ask={s.poly_no_ask:.3f} (sz={s.crypto_market.no_ask_size:.0f})")
        print(f"  Signal:  {s.fair_value}")
        print(f"  Action:  {s.edge_description}")
        print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def run_loop(interval: float, verbose: bool) -> None:
    """Continuous scanning loop."""
    print(f"Starting crypto arb scanner (interval={interval}s)")
    print(f"Press Ctrl-C to stop.\n")

    cycle = 0
    total_signals = 0

    try:
        while True:
            cycle += 1
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"[{ts}] Cycle {cycle}")

            try:
                signals = await scan_once(verbose=verbose)
                btc_price = get_binance_btc_price()
                if signals:
                    total_signals += len(signals)
                    print_report(signals, btc_price)
                else:
                    print(f"  No edge detected.\n")
            except Exception as exc:
                logger.warning("scan error: %s", exc)
                print(f"  Error: {exc}\n")

            await asyncio.sleep(interval)

    except KeyboardInterrupt:
        print(f"\n\nStopped. {cycle} cycles, {total_signals} total signals.")


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

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.loop > 0:
        return asyncio.run(run_loop(args.loop, args.verbose))
    else:
        return asyncio.run(run_once(args.verbose))


if __name__ == "__main__":
    raise SystemExit(main())
