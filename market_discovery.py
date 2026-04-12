"""Phase 1.2: Market discovery CLI.

Finds Polymarket binary markets worth monitoring for Type-1 arbitrage by
applying a cascade of filters that weed out dead-tail, illiquid, and already-
resolved markets — the noise that dominated Phase 0 scans.

Two-stage pipeline:
  1. Paginate through Gamma API, apply cheap metadata filters (binary, active,
     liquidity, end-date window).
  2. Batch-fetch CLOB order books for survivors, apply price-range + edge
     filters that require live book data.

Usage
-----
  # sane defaults: binary, active, liq>$50k, end in 1–90 days, yes∈[0.05,0.95]
  python market_discovery.py

  # tighter filters
  python market_discovery.py --min-liquidity 200000 --price-lo 0.10 --price-hi 0.90

  # category filter (if Gamma tags markets — YMMV)
  python market_discovery.py --category sports

  # JSON output for piping to Phase 1.3
  python market_discovery.py --json

  # scan deeper (more Gamma pages)
  python market_discovery.py --max-pages 10 --target 50
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from tabulate import tabulate

from polymarket_client import (
    Market,
    OrderBook,
    PolymarketClient,
    PolymarketError,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_TARGET = 30            # how many markets we want to end up with
DEFAULT_MAX_PAGES = 5          # max Gamma pages (100 each) to scan
DEFAULT_MIN_LIQUIDITY = 50_000.0
DEFAULT_END_DATE_MIN_HOURS = 2     # ignore markets that resolve within this
DEFAULT_END_DATE_MAX_DAYS = 90     # ignore markets that resolve after this
DEFAULT_PRICE_LO = 0.05
DEFAULT_PRICE_HI = 0.95
DEFAULT_FEE_BPS = 0.0
DEFAULT_SAFETY_BPS = 50.0
GAMMA_PAGE_SIZE = 100


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class DiscoveredMarket:
    """A market that survived all filters — ready for monitoring / snapshot."""

    market: Market
    yes_ask: float
    yes_ask_size: float
    no_ask: float
    no_ask_size: float
    yes_bid: float | None
    no_bid: float | None
    book_sum: float          # yes_ask + no_ask
    edge: float              # 1 - sum - fee - safety (negative = no arb)
    fillable_size: float     # min(yes_ask_size, no_ask_size) in shares
    days_remaining: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_id": self.market.market_id,
            "question": self.market.question,
            "category": self.market.category,
            "slug": self.market.slug,
            "yes_token_id": self.market.yes_token_id,
            "no_token_id": self.market.no_token_id,
            "liquidity": self.market.liquidity,
            "volume": self.market.volume,
            "end_date": self.market.end_date,
            "days_remaining": round(self.days_remaining, 1),
            "yes_ask": self.yes_ask,
            "yes_ask_size": self.yes_ask_size,
            "no_ask": self.no_ask,
            "no_ask_size": self.no_ask_size,
            "book_sum": round(self.book_sum, 4),
            "edge": round(self.edge, 6),
            "fillable_size": self.fillable_size,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_end_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _days_until(dt: datetime) -> float:
    now = datetime.now(timezone.utc)
    return (dt - now).total_seconds() / 86400.0


def _compute_edge(yes_ask: float, no_ask: float, fee_bps: float, safety_bps: float) -> float:
    cost = yes_ask + no_ask
    fee = cost * (fee_bps / 10_000.0)
    safety = safety_bps / 10_000.0
    return 1.0 - cost - fee - safety


# ---------------------------------------------------------------------------
# Discovery pipeline
# ---------------------------------------------------------------------------


async def discover(
    client: PolymarketClient,
    *,
    target: int = DEFAULT_TARGET,
    max_pages: int = DEFAULT_MAX_PAGES,
    min_liquidity: float = DEFAULT_MIN_LIQUIDITY,
    end_date_min_hours: float = DEFAULT_END_DATE_MIN_HOURS,
    end_date_max_days: float = DEFAULT_END_DATE_MAX_DAYS,
    price_lo: float = DEFAULT_PRICE_LO,
    price_hi: float = DEFAULT_PRICE_HI,
    category: str | None = None,
    fee_bps: float = DEFAULT_FEE_BPS,
    safety_bps: float = DEFAULT_SAFETY_BPS,
) -> list[DiscoveredMarket]:
    """Run the full two-stage discovery pipeline. Returns up to `target` markets."""

    # --- stage 1: paginate Gamma, metadata filter -------------------------
    candidates: list[Market] = []
    total_raw = 0

    for page in range(max_pages):
        offset = page * GAMMA_PAGE_SIZE
        try:
            batch = await client.get_markets(
                limit=GAMMA_PAGE_SIZE,
                offset=offset,
                min_liquidity=min_liquidity,
                order="liquidityNum",
            )
        except PolymarketError as exc:
            logger.warning("Gamma page %d failed: %s", page, exc)
            break

        if not batch:
            break  # no more markets
        total_raw += len(batch)

        for m in batch:
            if not m.is_binary:
                continue
            if not m.active or m.closed:
                continue

            # category filter (case-insensitive substring)
            if category and (not m.category or category.lower() not in m.category.lower()):
                continue

            # end-date window
            end_dt = _parse_end_date(m.end_date)
            if end_dt is None:
                continue  # no end date = can't compute days remaining
            days = _days_until(end_dt)
            if days < (end_date_min_hours / 24.0):
                continue  # too soon — imminent resolution
            if days > end_date_max_days:
                continue  # too far out

            candidates.append(m)

        # stop early if we have way more than we need (orderbook fetch is the bottleneck)
        if len(candidates) >= target * 3:
            break

    logger.info(
        "stage 1: scanned %d raw markets across %d page(s), %d passed metadata filter",
        total_raw,
        min(max_pages, (total_raw // GAMMA_PAGE_SIZE) + 1),
        len(candidates),
    )

    if not candidates:
        return []

    # --- stage 2: batch orderbook, price-range + edge filter --------------
    token_ids: list[str] = []
    for m in candidates:
        if m.yes_token_id:
            token_ids.append(m.yes_token_id)
        if m.no_token_id:
            token_ids.append(m.no_token_id)

    books: dict[str, OrderBook] = {}
    try:
        books = await client.get_orderbooks(token_ids)
    except PolymarketError as exc:
        logger.error("batch orderbook fetch failed: %s", exc)
        return []

    logger.info("stage 2: fetched %d/%d order books", len(books), len(token_ids))

    results: list[DiscoveredMarket] = []

    for m in candidates:
        if not (m.yes_token_id and m.no_token_id):
            continue
        yes_book = books.get(m.yes_token_id)
        no_book = books.get(m.no_token_id)
        if yes_book is None or no_book is None:
            continue

        yes_ba = yes_book.best_ask
        no_ba = no_book.best_ask
        if yes_ba is None or no_ba is None:
            continue  # one-sided book = no liquidity

        # price-range filter: yes_ask is the implied probability
        if yes_ba.price < price_lo or yes_ba.price > price_hi:
            continue

        end_dt = _parse_end_date(m.end_date)
        days = _days_until(end_dt) if end_dt else 0.0

        book_sum = yes_ba.price + no_ba.price
        edge = _compute_edge(yes_ba.price, no_ba.price, fee_bps, safety_bps)
        fillable = min(yes_ba.size, no_ba.size)

        yes_bb = yes_book.best_bid
        no_bb = no_book.best_bid

        results.append(DiscoveredMarket(
            market=m,
            yes_ask=yes_ba.price,
            yes_ask_size=yes_ba.size,
            no_ask=no_ba.price,
            no_ask_size=no_ba.size,
            yes_bid=yes_bb.price if yes_bb else None,
            no_bid=no_bb.price if no_bb else None,
            book_sum=book_sum,
            edge=edge,
            fillable_size=fillable,
            days_remaining=days,
        ))

        if len(results) >= target:
            break

    # sort by edge descending (least negative = closest to arb)
    results.sort(key=lambda d: d.edge, reverse=True)

    logger.info("stage 2: %d markets passed all filters", len(results))
    return results


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def _format_days(d: float) -> str:
    if d < 1:
        return f"{d * 24:.0f}h"
    return f"{d:.0f}d"


def print_table(discovered: list[DiscoveredMarket]) -> None:
    rows = []
    for d in discovered:
        rows.append([
            d.market.market_id,
            d.market.question[:55],
            d.market.category or "-",
            _format_days(d.days_remaining),
            f"${d.market.liquidity:,.0f}",
            f"{d.yes_ask:.3f}",
            f"{d.no_ask:.3f}",
            f"{d.book_sum:.4f}",
            f"{d.edge * 100:+.2f}%",
            f"{d.fillable_size:,.0f}",
        ])
    headers = [
        "id", "question", "cat", "ends", "liquidity",
        "y_ask", "n_ask", "sum", "edge", "fill_sz",
    ]
    print(tabulate(rows, headers=headers, tablefmt="github",
                   maxcolwidths=[10, 55, 12, 6, 12, 7, 7, 8, 8, 10]))


def print_json(discovered: list[DiscoveredMarket]) -> None:
    print(json.dumps([d.to_dict() for d in discovered], indent=2))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--target", type=int, default=DEFAULT_TARGET,
                   help=f"max markets to return (default {DEFAULT_TARGET})")
    p.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES,
                   help=f"max Gamma pages to scan (default {DEFAULT_MAX_PAGES})")
    p.add_argument("--min-liquidity", type=float, default=DEFAULT_MIN_LIQUIDITY,
                   help=f"minimum liquidity in USD (default {DEFAULT_MIN_LIQUIDITY:,.0f})")
    p.add_argument("--end-min-hours", type=float, default=DEFAULT_END_DATE_MIN_HOURS,
                   help=f"ignore markets resolving sooner than this (default {DEFAULT_END_DATE_MIN_HOURS}h)")
    p.add_argument("--end-max-days", type=float, default=DEFAULT_END_DATE_MAX_DAYS,
                   help=f"ignore markets resolving later than this (default {DEFAULT_END_DATE_MAX_DAYS}d)")
    p.add_argument("--price-lo", type=float, default=DEFAULT_PRICE_LO,
                   help=f"min yes_ask price (default {DEFAULT_PRICE_LO})")
    p.add_argument("--price-hi", type=float, default=DEFAULT_PRICE_HI,
                   help=f"max yes_ask price (default {DEFAULT_PRICE_HI})")
    p.add_argument("--category", type=str, default=None,
                   help="filter by category substring (e.g. sports, politics, crypto)")
    p.add_argument("--fee-bps", type=float, default=DEFAULT_FEE_BPS)
    p.add_argument("--safety-bps", type=float, default=DEFAULT_SAFETY_BPS)
    p.add_argument("--json", action="store_true", help="output as JSON (for piping)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


async def run(args: argparse.Namespace) -> int:
    if not args.json:
        print(f"Discovering markets (target={args.target}, liq>=${args.min_liquidity:,.0f}, "
              f"end={args.end_min_hours}h..{args.end_max_days}d, "
              f"price=[{args.price_lo},{args.price_hi}])")
        print()

    async with PolymarketClient() as client:
        discovered = await discover(
            client,
            target=args.target,
            max_pages=args.max_pages,
            min_liquidity=args.min_liquidity,
            end_date_min_hours=args.end_min_hours,
            end_date_max_days=args.end_max_days,
            price_lo=args.price_lo,
            price_hi=args.price_hi,
            category=args.category,
            fee_bps=args.fee_bps,
            safety_bps=args.safety_bps,
        )

    if not discovered:
        if not args.json:
            print("No markets matched all filters. Try relaxing parameters:")
            print("  --min-liquidity 10000  --price-lo 0.03  --end-max-days 180")
        else:
            print("[]")
        return 1

    if args.json:
        print_json(discovered)
    else:
        print_table(discovered)
        print(f"\n{len(discovered)} market(s) found.")
        arb = [d for d in discovered if d.edge > 0]
        if arb:
            print(f"  {len(arb)} with positive edge (Type-1 arb candidate)!")
        else:
            print("  0 with positive edge — all sum > $1 (expected, see README).")

    return 0


def main() -> int:
    args = parse_args()
    log_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(level=log_level, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
