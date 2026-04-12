"""Phase 1.4: Poll loop — periodic orderbook snapshot service.

Runs continuously, capturing orderbook snapshots at a configurable interval.
Two cadences:

  - **Book refresh** (fast, default 5s): fetch CLOB order books for the
    current market list, write a parquet snapshot.
  - **Market re-discovery** (slow, default 5min): re-run the Gamma-based
    discovery pipeline to pick up new markets and drop expired ones.

This avoids hammering the Gamma API every 5 seconds while keeping orderbook
data fresh. The market list is stable over minutes; the book data is not.

Usage
-----
  # default: 5s book interval, 5min re-discovery, 30 markets
  python poll_loop.py

  # faster polling, more markets
  python poll_loop.py --interval 2 --rediscover 120 --target 50

  # custom output dir
  python poll_loop.py --data-dir /mnt/data/snapshots

  # run overnight then Ctrl-C — stats printed on shutdown
  nohup python poll_loop.py > poll.log 2>&1 &

Graceful shutdown
-----------------
  Ctrl-C (SIGINT) or SIGTERM → finish current cycle, print summary stats,
  exit cleanly. No data corruption risk (each parquet file is written
  atomically by pyarrow).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from market_discovery import DiscoveredMarket, _days_until, _parse_end_date, discover
from polymarket_client import (
    Market,
    OrderBook,
    PolymarketClient,
    PolymarketError,
)
from snapshot import (
    DEFAULT_DATA_DIR,
    SnapshotRow,
    row_from_books,
    write_snapshot,
)

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SEC = 5.0
DEFAULT_REDISCOVER_SEC = 300.0  # 5 minutes
DEFAULT_TARGET = 30
DEFAULT_MIN_LIQUIDITY = 50_000.0
DEFAULT_PRICE_LO = 0.05
DEFAULT_PRICE_HI = 0.95
DEFAULT_FEE_BPS = 0.0
DEFAULT_SAFETY_BPS = 50.0


# ---------------------------------------------------------------------------
# Stats tracking
# ---------------------------------------------------------------------------


class PollStats:
    def __init__(self) -> None:
        self.started_at: float = time.time()
        self.cycles: int = 0
        self.rows_written: int = 0
        self.files_written: int = 0
        self.errors: int = 0
        self.discoveries: int = 0
        self.min_edge: float = float("inf")
        self.max_edge: float = float("-inf")
        self.arb_detections: int = 0

    def record_cycle(self, rows: int, path: Path | None, edge_values: list[float]) -> None:
        self.cycles += 1
        self.rows_written += rows
        if path:
            self.files_written += 1
        for e in edge_values:
            self.min_edge = min(self.min_edge, e)
            self.max_edge = max(self.max_edge, e)
            if e > 0:
                self.arb_detections += 1

    def record_error(self) -> None:
        self.errors += 1

    def record_discovery(self) -> None:
        self.discoveries += 1

    @property
    def uptime_sec(self) -> float:
        return time.time() - self.started_at

    def summary(self) -> str:
        uptime = self.uptime_sec
        h, rem = divmod(int(uptime), 3600)
        m, s = divmod(rem, 60)
        lines = [
            "",
            "=" * 60,
            "Poll loop summary",
            "=" * 60,
            f"  Uptime:             {h}h {m}m {s}s",
            f"  Cycles completed:   {self.cycles}",
            f"  Files written:      {self.files_written}",
            f"  Total rows:         {self.rows_written:,}",
            f"  Market discoveries: {self.discoveries}",
            f"  Errors:             {self.errors}",
        ]
        if self.min_edge != float("inf"):
            lines.append(f"  Edge range:         [{self.min_edge*100:+.3f}%, {self.max_edge*100:+.3f}%]")
        if self.arb_detections:
            lines.append(f"  ARB DETECTIONS:     {self.arb_detections} row(s) with edge > 0!")
        else:
            lines.append(f"  Arb detections:     0 (sum never dipped below threshold)")
        lines.append("=" * 60)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------


async def poll_loop(
    *,
    interval_sec: float = DEFAULT_INTERVAL_SEC,
    rediscover_sec: float = DEFAULT_REDISCOVER_SEC,
    target: int = DEFAULT_TARGET,
    min_liquidity: float = DEFAULT_MIN_LIQUIDITY,
    price_lo: float = DEFAULT_PRICE_LO,
    price_hi: float = DEFAULT_PRICE_HI,
    fee_bps: float = DEFAULT_FEE_BPS,
    safety_bps: float = DEFAULT_SAFETY_BPS,
    data_dir: Path = DEFAULT_DATA_DIR,
    stop_event: asyncio.Event | None = None,
) -> PollStats:
    """Run the poll loop until stop_event is set or KeyboardInterrupt."""

    if stop_event is None:
        stop_event = asyncio.Event()

    stats = PollStats()

    # install signal handlers
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    async with PolymarketClient() as client:
        # --- initial discovery ---
        print(f"[{_ts()}] Initial market discovery (target={target}, liq>=${min_liquidity:,.0f})...")
        market_list = await _discover_safe(
            client, target, min_liquidity, price_lo, price_hi, fee_bps, safety_bps
        )
        stats.record_discovery()

        if not market_list:
            print(f"[{_ts()}] No markets found. Exiting.")
            return stats

        print(f"[{_ts()}] Monitoring {len(market_list)} markets. "
              f"Interval={interval_sec}s, re-discover every {rediscover_sec}s.")
        print(f"[{_ts()}] Press Ctrl-C to stop.\n")

        last_discover = time.monotonic()

        while not stop_event.is_set():
            cycle_start = time.monotonic()

            # --- re-discovery if stale ---
            if (cycle_start - last_discover) >= rediscover_sec:
                logger.info("re-discovering markets...")
                new_list = await _discover_safe(
                    client, target, min_liquidity, price_lo, price_hi, fee_bps, safety_bps
                )
                stats.record_discovery()
                if new_list:
                    added = len(set(m.market.market_id for m in new_list) -
                                set(m.market.market_id for m in market_list))
                    dropped = len(set(m.market.market_id for m in market_list) -
                                  set(m.market.market_id for m in new_list))
                    market_list = new_list
                    if added or dropped:
                        print(f"[{_ts()}] Re-discovered: {len(market_list)} markets "
                              f"(+{added} new, -{dropped} dropped)")
                last_discover = cycle_start

            # --- fast path: book fetch only ---
            ts_ms = int(time.time() * 1000)
            token_ids: list[str] = []
            for d in market_list:
                if d.market.yes_token_id:
                    token_ids.append(d.market.yes_token_id)
                if d.market.no_token_id:
                    token_ids.append(d.market.no_token_id)

            try:
                books = await client.get_orderbooks(token_ids)
            except PolymarketError as exc:
                logger.warning("book fetch failed: %s", exc)
                stats.record_error()
                await _wait_or_stop(stop_event, interval_sec)
                continue

            # build rows
            rows: list[SnapshotRow] = []
            edge_values: list[float] = []
            for d in market_list:
                m = d.market
                if not (m.yes_token_id and m.no_token_id):
                    continue
                yes_book = books.get(m.yes_token_id)
                no_book = books.get(m.no_token_id)
                if yes_book is None or no_book is None:
                    continue

                end_dt = _parse_end_date(m.end_date)
                days = _days_until(end_dt) if end_dt else d.days_remaining

                row = row_from_books(
                    m, yes_book, no_book, ts_ms,
                    fee_bps=fee_bps, safety_bps=safety_bps,
                    days_remaining=days,
                )
                if row is not None:
                    rows.append(row)
                    edge_values.append(row.edge)

            # write
            path: Path | None = None
            if rows:
                try:
                    path = write_snapshot(rows, data_dir)
                except Exception as exc:
                    logger.warning("parquet write failed: %s", exc)
                    stats.record_error()

            stats.record_cycle(len(rows), path, edge_values)

            # status line
            best_edge = max(edge_values) if edge_values else 0.0
            flag = " *** ARB ***" if best_edge > 0 else ""
            sys.stdout.write(
                f"\r[{_ts()}] cycle={stats.cycles:<6} rows={len(rows):<4} "
                f"best_edge={best_edge*100:+.3f}% "
                f"files={stats.files_written}{flag}    "
            )
            sys.stdout.flush()

            # sleep remainder
            elapsed = time.monotonic() - cycle_start
            remaining = max(0.0, interval_sec - elapsed)
            await _wait_or_stop(stop_event, remaining)

    print(stats.summary())
    return stats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


async def _wait_or_stop(event: asyncio.Event, seconds: float) -> None:
    """Sleep for `seconds` but wake up early if `event` is set."""
    try:
        await asyncio.wait_for(event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def _discover_safe(
    client: PolymarketClient,
    target: int,
    min_liquidity: float,
    price_lo: float,
    price_hi: float,
    fee_bps: float,
    safety_bps: float,
) -> list[DiscoveredMarket]:
    try:
        return await discover(
            client,
            target=target,
            min_liquidity=min_liquidity,
            price_lo=price_lo,
            price_hi=price_hi,
            fee_bps=fee_bps,
            safety_bps=safety_bps,
        )
    except PolymarketError as exc:
        logger.warning("discovery failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SEC,
                   help=f"seconds between book snapshots (default {DEFAULT_INTERVAL_SEC})")
    p.add_argument("--rediscover", type=float, default=DEFAULT_REDISCOVER_SEC,
                   help=f"seconds between market re-discoveries (default {DEFAULT_REDISCOVER_SEC})")
    p.add_argument("--target", type=int, default=DEFAULT_TARGET)
    p.add_argument("--min-liquidity", type=float, default=DEFAULT_MIN_LIQUIDITY)
    p.add_argument("--price-lo", type=float, default=DEFAULT_PRICE_LO)
    p.add_argument("--price-hi", type=float, default=DEFAULT_PRICE_HI)
    p.add_argument("--fee-bps", type=float, default=DEFAULT_FEE_BPS)
    p.add_argument("--safety-bps", type=float, default=DEFAULT_SAFETY_BPS)
    p.add_argument("--data-dir", type=str, default=str(DEFAULT_DATA_DIR))
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    log_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(level=log_level, format="%(levelname)s %(name)s: %(message)s")

    stats = asyncio.run(poll_loop(
        interval_sec=args.interval,
        rediscover_sec=args.rediscover,
        target=args.target,
        min_liquidity=args.min_liquidity,
        price_lo=args.price_lo,
        price_hi=args.price_hi,
        fee_bps=args.fee_bps,
        safety_bps=args.safety_bps,
        data_dir=Path(args.data_dir),
    ))

    return 0 if stats.errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
