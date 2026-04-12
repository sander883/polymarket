"""Phase 1.3: Orderbook snapshot capture + parquet storage.

Captures a snapshot of all discovered markets' order books and writes them
as parquet files — one file per snapshot batch. This is the raw material for:
  - Phase 1.4 poll loop (calls `take_snapshot` every N seconds)
  - Phase 2 DuckDB analysis ("how often does sum dip below X?")
  - Phase 5 backtesting (replay historical snapshots)

Storage layout
--------------
    data/snapshots/
      2026-04-12/
        snap_143022.parquet       # 14:30:22 UTC — one batch
        snap_143527.parquet       # 14:35:27 UTC — next batch
        ...
      2026-04-13/
        snap_000502.parquet
        ...

Each parquet file contains N rows (one per market per snapshot). DuckDB
can read the entire directory tree with:

    SELECT * FROM read_parquet('data/snapshots/**/*.parquet')

Schema
------
Each row captures top-of-book for both YES and NO sides, plus depth
summaries. See `PARQUET_SCHEMA` constant for the full pyarrow schema.

Standalone usage
----------------
    # one-shot: discover markets + snapshot + write parquet
    python snapshot.py

    # custom filters forwarded to market_discovery
    python snapshot.py --min-liquidity 100000 --target 50

    # custom output dir
    python snapshot.py --data-dir /tmp/poly-snapshots
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from market_discovery import DiscoveredMarket, discover
from polymarket_client import (
    OrderBook,
    PolymarketClient,
    PolymarketError,
)

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = Path("data/snapshots")

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

PARQUET_SCHEMA = pa.schema([
    ("timestamp_ms", pa.int64()),
    ("market_id", pa.string()),
    ("question", pa.string()),
    ("condition_id", pa.string()),
    ("slug", pa.string()),
    ("yes_token_id", pa.string()),
    ("no_token_id", pa.string()),
    ("category", pa.string()),
    ("end_date", pa.string()),
    ("liquidity", pa.float64()),
    ("volume", pa.float64()),
    # YES side
    ("yes_best_bid_price", pa.float64()),
    ("yes_best_bid_size", pa.float64()),
    ("yes_best_ask_price", pa.float64()),
    ("yes_best_ask_size", pa.float64()),
    ("yes_depth_5_ask", pa.float64()),   # total size of top 5 ask levels
    ("yes_depth_5_bid", pa.float64()),
    # NO side
    ("no_best_bid_price", pa.float64()),
    ("no_best_bid_size", pa.float64()),
    ("no_best_ask_price", pa.float64()),
    ("no_best_ask_size", pa.float64()),
    ("no_depth_5_ask", pa.float64()),
    ("no_depth_5_bid", pa.float64()),
    # computed
    ("book_sum", pa.float64()),         # yes_ask + no_ask
    ("edge", pa.float64()),             # 1 - sum - fee - safety
    ("fillable_size", pa.float64()),    # min(yes_ask_size, no_ask_size)
    ("days_remaining", pa.float64()),
])


# ---------------------------------------------------------------------------
# Snapshot data
# ---------------------------------------------------------------------------


@dataclass
class SnapshotRow:
    """One market at one point in time — flat, parquet-ready."""

    timestamp_ms: int
    market_id: str
    question: str
    condition_id: str
    slug: str
    yes_token_id: str
    no_token_id: str
    category: str
    end_date: str
    liquidity: float
    volume: float
    yes_best_bid_price: float | None
    yes_best_bid_size: float | None
    yes_best_ask_price: float | None
    yes_best_ask_size: float | None
    yes_depth_5_ask: float
    yes_depth_5_bid: float
    no_best_bid_price: float | None
    no_best_bid_size: float | None
    no_best_ask_price: float | None
    no_best_ask_size: float | None
    no_depth_5_ask: float
    no_depth_5_bid: float
    book_sum: float
    edge: float
    fillable_size: float
    days_remaining: float


def _depth_n(book: OrderBook, side: str, n: int = 5) -> float:
    """Sum the size of the top N levels on a side."""
    levels = book.asks[:n] if side == "ask" else book.bids[:n]
    return sum(lv.size for lv in levels)


def row_from_discovered(d: DiscoveredMarket, ts_ms: int) -> SnapshotRow:
    """Convert a DiscoveredMarket (which already has book data) to a flat row."""
    return SnapshotRow(
        timestamp_ms=ts_ms,
        market_id=d.market.market_id,
        question=d.market.question,
        condition_id=d.market.condition_id,
        slug=d.market.slug,
        yes_token_id=d.market.yes_token_id or "",
        no_token_id=d.market.no_token_id or "",
        category=d.market.category or "",
        end_date=d.market.end_date or "",
        liquidity=d.market.liquidity,
        volume=d.market.volume,
        yes_best_bid_price=d.yes_bid,
        yes_best_bid_size=None,  # DiscoveredMarket doesn't carry bid size — filled from books
        yes_best_ask_price=d.yes_ask,
        yes_best_ask_size=d.yes_ask_size,
        yes_depth_5_ask=0.0,  # filled when we have raw books
        yes_depth_5_bid=0.0,
        no_best_bid_price=d.no_bid,
        no_best_bid_size=None,
        no_best_ask_price=d.no_ask,
        no_best_ask_size=d.no_ask_size,
        no_depth_5_ask=0.0,
        no_depth_5_bid=0.0,
        book_sum=d.book_sum,
        edge=d.edge,
        fillable_size=d.fillable_size,
        days_remaining=d.days_remaining,
    )


def row_from_books(
    market: Any,
    yes_book: OrderBook,
    no_book: OrderBook,
    ts_ms: int,
    fee_bps: float = 0.0,
    safety_bps: float = 50.0,
    days_remaining: float = 0.0,
) -> SnapshotRow | None:
    """Build a snapshot row from raw order books (used by poll loop where we
    already have the books and don't need to re-discover)."""
    yes_ba = yes_book.best_ask
    no_ba = no_book.best_ask
    if yes_ba is None or no_ba is None:
        return None

    yes_bb = yes_book.best_bid
    no_bb = no_book.best_bid

    book_sum = yes_ba.price + no_ba.price
    cost = book_sum
    fee = cost * (fee_bps / 10_000.0)
    safety = safety_bps / 10_000.0
    edge = 1.0 - cost - fee - safety
    fillable = min(yes_ba.size, no_ba.size)

    return SnapshotRow(
        timestamp_ms=ts_ms,
        market_id=getattr(market, "market_id", ""),
        question=getattr(market, "question", ""),
        condition_id=getattr(market, "condition_id", ""),
        slug=getattr(market, "slug", ""),
        yes_token_id=getattr(market, "yes_token_id", "") or "",
        no_token_id=getattr(market, "no_token_id", "") or "",
        category=getattr(market, "category", "") or "",
        end_date=getattr(market, "end_date", "") or "",
        liquidity=getattr(market, "liquidity", 0.0),
        volume=getattr(market, "volume", 0.0),
        yes_best_bid_price=yes_bb.price if yes_bb else None,
        yes_best_bid_size=yes_bb.size if yes_bb else None,
        yes_best_ask_price=yes_ba.price,
        yes_best_ask_size=yes_ba.size,
        yes_depth_5_ask=_depth_n(yes_book, "ask"),
        yes_depth_5_bid=_depth_n(yes_book, "bid"),
        no_best_bid_price=no_bb.price if no_bb else None,
        no_best_bid_size=no_bb.size if no_bb else None,
        no_best_ask_price=no_ba.price,
        no_best_ask_size=no_ba.size,
        no_depth_5_ask=_depth_n(no_book, "ask"),
        no_depth_5_bid=_depth_n(no_book, "bid"),
        book_sum=book_sum,
        edge=edge,
        fillable_size=fillable,
        days_remaining=days_remaining,
    )


# ---------------------------------------------------------------------------
# Parquet I/O
# ---------------------------------------------------------------------------


def _rows_to_table(rows: list[SnapshotRow]) -> pa.Table:
    """Convert snapshot rows to a pyarrow Table with our schema."""
    columns: dict[str, list[Any]] = {f.name: [] for f in PARQUET_SCHEMA}

    for r in rows:
        columns["timestamp_ms"].append(r.timestamp_ms)
        columns["market_id"].append(r.market_id)
        columns["question"].append(r.question)
        columns["condition_id"].append(r.condition_id)
        columns["slug"].append(r.slug)
        columns["yes_token_id"].append(r.yes_token_id)
        columns["no_token_id"].append(r.no_token_id)
        columns["category"].append(r.category)
        columns["end_date"].append(r.end_date)
        columns["liquidity"].append(r.liquidity)
        columns["volume"].append(r.volume)
        columns["yes_best_bid_price"].append(r.yes_best_bid_price)
        columns["yes_best_bid_size"].append(r.yes_best_bid_size)
        columns["yes_best_ask_price"].append(r.yes_best_ask_price)
        columns["yes_best_ask_size"].append(r.yes_best_ask_size)
        columns["yes_depth_5_ask"].append(r.yes_depth_5_ask)
        columns["yes_depth_5_bid"].append(r.yes_depth_5_bid)
        columns["no_best_bid_price"].append(r.no_best_bid_price)
        columns["no_best_bid_size"].append(r.no_best_bid_size)
        columns["no_best_ask_price"].append(r.no_best_ask_price)
        columns["no_best_ask_size"].append(r.no_best_ask_size)
        columns["no_depth_5_ask"].append(r.no_depth_5_ask)
        columns["no_depth_5_bid"].append(r.no_depth_5_bid)
        columns["book_sum"].append(r.book_sum)
        columns["edge"].append(r.edge)
        columns["fillable_size"].append(r.fillable_size)
        columns["days_remaining"].append(r.days_remaining)

    arrays = [pa.array(columns[f.name], type=f.type) for f in PARQUET_SCHEMA]
    return pa.table(arrays, schema=PARQUET_SCHEMA)


def write_snapshot(rows: list[SnapshotRow], base_dir: Path = DEFAULT_DATA_DIR) -> Path:
    """Write a batch of snapshot rows to a parquet file.

    Returns the path of the written file. Creates directories as needed.
    """
    if not rows:
        raise ValueError("no rows to write")

    now = datetime.now(timezone.utc)
    day_dir = base_dir / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    filename = f"snap_{now.strftime('%H%M%S')}.parquet"
    path = day_dir / filename

    table = _rows_to_table(rows)
    pq.write_table(table, path, compression="snappy")

    logger.info("wrote %d rows to %s (%.1f KB)", len(rows), path, path.stat().st_size / 1024)
    return path


# ---------------------------------------------------------------------------
# High-level: take_snapshot (used by poll loop)
# ---------------------------------------------------------------------------


async def take_snapshot(
    client: PolymarketClient,
    *,
    target: int = 30,
    min_liquidity: float = 50_000.0,
    end_date_max_days: float = 90.0,
    price_lo: float = 0.05,
    price_hi: float = 0.95,
    fee_bps: float = 0.0,
    safety_bps: float = 50.0,
    base_dir: Path = DEFAULT_DATA_DIR,
) -> tuple[Path | None, int]:
    """Discover markets + fetch books + write parquet. One-shot.

    Returns (path_written, num_rows). path is None if no markets found.
    Used by poll_loop.py to take periodic snapshots.
    """
    ts_ms = int(time.time() * 1000)

    discovered = await discover(
        client,
        target=target,
        min_liquidity=min_liquidity,
        end_date_max_days=end_date_max_days,
        price_lo=price_lo,
        price_hi=price_hi,
        fee_bps=fee_bps,
        safety_bps=safety_bps,
    )

    if not discovered:
        logger.warning("no markets discovered — nothing to snapshot")
        return None, 0

    rows = [row_from_discovered(d, ts_ms) for d in discovered]

    path = write_snapshot(rows, base_dir)
    return path, len(rows)


# ---------------------------------------------------------------------------
# CLI: one-shot snapshot
# ---------------------------------------------------------------------------


async def _run(args) -> int:
    import argparse

    print(f"Taking one-shot snapshot (target={args.target}, liq>=${args.min_liquidity:,.0f})")

    async with PolymarketClient() as client:
        path, n = await take_snapshot(
            client,
            target=args.target,
            min_liquidity=args.min_liquidity,
            price_lo=args.price_lo,
            price_hi=args.price_hi,
            base_dir=Path(args.data_dir),
        )

    if path is None:
        print("No markets found — no file written.")
        return 1

    print(f"\nWrote {n} rows to {path}")
    print(f"File size: {path.stat().st_size / 1024:.1f} KB")

    # quick preview
    table = pq.read_table(path)
    df = table.to_pandas()
    print(f"\nPreview (first 5 rows):")
    preview_cols = ["market_id", "question", "yes_best_ask_price", "no_best_ask_price", "book_sum", "edge"]
    available = [c for c in preview_cols if c in df.columns]
    print(df[available].head().to_string(index=False))

    print(f"\nQuery with DuckDB:")
    print(f"  duckdb -c \"SELECT * FROM read_parquet('{args.data_dir}/**/*.parquet')\"")

    return 0


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", type=int, default=30)
    p.add_argument("--min-liquidity", type=float, default=50_000.0)
    p.add_argument("--price-lo", type=float, default=0.05)
    p.add_argument("--price-hi", type=float, default=0.95)
    p.add_argument("--data-dir", type=str, default=str(DEFAULT_DATA_DIR))
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(level=log_level, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
