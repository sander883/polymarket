"""Fetch BTC/USDT OHLCV and live ticker from Binance via ccxt.

This is the first building block for the Polymarket trading project.
It only uses Binance public endpoints, so no API key is required.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import ccxt
from tabulate import tabulate

SYMBOL = "BTC/USDT"
TIMEFRAME = "1m"
LIMIT = 100


def format_timestamp(ms: int) -> str:
    """Convert a millisecond UTC timestamp to a human readable string."""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def fetch_ohlcv(exchange: ccxt.Exchange) -> list[list]:
    """Fetch the most recent OHLCV candles for SYMBOL."""
    if not exchange.has.get("fetchOHLCV"):
        raise RuntimeError(f"{exchange.id} does not support fetchOHLCV")
    return exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=LIMIT)


def fetch_price(exchange: ccxt.Exchange) -> dict:
    """Fetch the current ticker for SYMBOL."""
    return exchange.fetch_ticker(SYMBOL)


def render_ohlcv_table(candles: list[list]) -> str:
    """Return a pretty printed OHLCV table."""
    rows = [
        [
            format_timestamp(ts),
            f"{open_:.2f}",
            f"{high:.2f}",
            f"{low:.2f}",
            f"{close:.2f}",
            f"{volume:.4f}",
        ]
        for ts, open_, high, low, close, volume in candles
    ]
    headers = ["Time (UTC)", "Open", "High", "Low", "Close", "Volume"]
    return tabulate(rows, headers=headers, tablefmt="github")


def main() -> int:
    # Binance public API — no API key needed.
    exchange = ccxt.binance(
        {
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
    )

    try:
        exchange.load_markets()
    except ccxt.BaseError as exc:
        print(f"[ERROR] Failed to load markets: {exc}", file=sys.stderr)
        return 1

    print(f"Fetching last {LIMIT} {TIMEFRAME} candles for {SYMBOL} from Binance...\n")
    try:
        candles = fetch_ohlcv(exchange)
    except ccxt.BaseError as exc:
        print(f"[ERROR] Failed to fetch OHLCV: {exc}", file=sys.stderr)
        return 1

    print(render_ohlcv_table(candles))

    print("\nFetching live ticker...")
    try:
        ticker = fetch_price(exchange)
    except ccxt.BaseError as exc:
        print(f"[ERROR] Failed to fetch ticker: {exc}", file=sys.stderr)
        return 1

    last = ticker.get("last")
    bid = ticker.get("bid")
    ask = ticker.get("ask")
    ts = ticker.get("timestamp")
    ts_str = format_timestamp(ts) if ts else "n/a"

    summary = [
        ["Symbol", SYMBOL],
        ["Last", f"{last:.2f}" if last is not None else "n/a"],
        ["Bid", f"{bid:.2f}" if bid is not None else "n/a"],
        ["Ask", f"{ask:.2f}" if ask is not None else "n/a"],
        ["Timestamp (UTC)", ts_str],
    ]
    print(tabulate(summary, tablefmt="github"))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
