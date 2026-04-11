"""Phase 0: verify Polymarket API access from the local environment.

Purpose
-------
Before investing time in building the arbitrage bot, confirm that the machine
this script runs on can actually talk to Polymarket's public endpoints and
that the data shape matches our assumptions.

What it does (read-only, no wallet, no orders)
----------------------------------------------
  1. GET Gamma API        → list a few active liquid markets
  2. For each market      → fetch the CLOB order book for YES and NO tokens
  3. Compute YES_ask + NO_ask and detect any Type-1 arbitrage candidates
     (pure detection; no trades are placed)
  4. Print a human-readable report and exit 0 on success

Exit codes
----------
  0  all checks passed
  2  Gamma API unreachable / geo-blocked / network error
  3  Gamma returned data but in an unexpected shape
  4  CLOB API unreachable or returned an error
  5  CLOB returned data but in an unexpected shape

Usage
-----
  python verify_polymarket_access.py
  python verify_polymarket_access.py --limit 20 --min-volume 50000
  python verify_polymarket_access.py --fee-bps 200 --safety-bps 50
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx
from tabulate import tabulate

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"

DEFAULT_LIMIT = 10
DEFAULT_MIN_VOLUME = 10_000.0  # USD
DEFAULT_FEE_BPS = 0            # Polymarket historically 0% trading fee; verify current
DEFAULT_SAFETY_BPS = 50        # 0.5% buffer for slippage / execution slippage
REQUEST_TIMEOUT = 15.0


@dataclass
class Market:
    market_id: str
    question: str
    yes_token_id: str | None
    no_token_id: str | None
    volume: float
    liquidity: float
    end_date: str | None


def log_step(n: int, msg: str) -> None:
    print(f"\n[STEP {n}] {msg}")


def log_ok(msg: str) -> None:
    print(f"  OK    {msg}")


def log_warn(msg: str) -> None:
    print(f"  WARN  {msg}")


def log_err(msg: str) -> None:
    print(f"  ERROR {msg}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                   help=f"how many markets to probe (default {DEFAULT_LIMIT})")
    p.add_argument("--min-volume", type=float, default=DEFAULT_MIN_VOLUME,
                   help=f"skip markets with volume < this (USD, default {DEFAULT_MIN_VOLUME})")
    p.add_argument("--fee-bps", type=float, default=DEFAULT_FEE_BPS,
                   help=f"assumed round-trip fee in basis points (default {DEFAULT_FEE_BPS})")
    p.add_argument("--safety-bps", type=float, default=DEFAULT_SAFETY_BPS,
                   help=f"extra safety buffer in basis points (default {DEFAULT_SAFETY_BPS})")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="print raw API responses for debugging")
    return p.parse_args()


def fetch_gamma_markets(client: httpx.Client, limit: int, min_volume: float) -> list[dict[str, Any]]:
    """Fetch active, liquid, binary markets from Gamma API."""
    params = {
        "closed": "false",
        "active": "true",
        "limit": str(limit * 3),  # fetch more to filter
        "order": "volumeNum",
        "ascending": "false",
    }
    t0 = time.perf_counter()
    resp = client.get(GAMMA_URL, params=params)
    elapsed = (time.perf_counter() - t0) * 1000
    log_ok(f"GET {GAMMA_URL} -> {resp.status_code} ({elapsed:.0f} ms)")
    resp.raise_for_status()
    data = resp.json()

    if not isinstance(data, list):
        raise ValueError(f"expected list from Gamma, got {type(data).__name__}")

    filtered = [m for m in data if float(m.get("volumeNum") or 0) >= min_volume]
    return filtered[:limit]


def parse_market(raw: dict[str, Any]) -> Market | None:
    """Extract the fields we care about. Return None if the market is not a
    simple binary with two clob token ids."""
    # clobTokenIds is returned as a JSON-encoded string by Gamma — defensively handle both shapes.
    token_ids_field = raw.get("clobTokenIds")
    if isinstance(token_ids_field, str):
        try:
            token_ids = json.loads(token_ids_field)
        except json.JSONDecodeError:
            return None
    else:
        token_ids = token_ids_field

    if not isinstance(token_ids, list) or len(token_ids) != 2:
        return None

    return Market(
        market_id=str(raw.get("id", "")),
        question=str(raw.get("question", ""))[:80],
        yes_token_id=str(token_ids[0]),
        no_token_id=str(token_ids[1]),
        volume=float(raw.get("volumeNum") or 0),
        liquidity=float(raw.get("liquidityNum") or 0),
        end_date=raw.get("endDate"),
    )


def fetch_orderbook(client: httpx.Client, token_id: str) -> dict[str, Any]:
    """Fetch the CLOB order book for a single token id."""
    resp = client.get(CLOB_BOOK_URL, params={"token_id": token_id})
    resp.raise_for_status()
    return resp.json()


def best_ask(book: dict[str, Any]) -> tuple[float, float] | None:
    """Return (price, size) for the best (lowest) ask in the book, or None.

    CLOB book shape: {"asks": [{"price": "0.52", "size": "100"}, ...], "bids": [...]}
    NOTE: on Polymarket CLOB the asks list is sorted ASC by price already, but
    we don't rely on that — we scan to be safe.
    """
    asks = book.get("asks") or []
    if not asks:
        return None
    try:
        parsed = [(float(a["price"]), float(a["size"])) for a in asks]
    except (KeyError, TypeError, ValueError):
        return None
    parsed.sort(key=lambda x: x[0])
    return parsed[0]


def compute_type1_edge(yes_ask_px: float, no_ask_px: float, fee_bps: float, safety_bps: float) -> float:
    """Edge = 1 - (yes_ask + no_ask) - fees - safety, in decimal form.

    Positive edge means a YES+NO basket can be bought for less than $1, locking
    in profit at resolution regardless of outcome.
    """
    cost = yes_ask_px + no_ask_px
    fee = cost * (fee_bps / 10_000.0)
    safety = safety_bps / 10_000.0
    return 1.0 - cost - fee - safety


def main() -> int:
    args = parse_args()

    print("=" * 70)
    print("Polymarket access verification (Phase 0)")
    print("=" * 70)
    print(f"limit={args.limit}  min_volume=${args.min_volume:,.0f}  "
          f"fee_bps={args.fee_bps}  safety_bps={args.safety_bps}")

    headers = {"User-Agent": "polymarket-arb-bot-verify/0.1 (+personal research)"}
    with httpx.Client(timeout=REQUEST_TIMEOUT, headers=headers, follow_redirects=True) as client:

        # ---- STEP 1: Gamma API ----
        log_step(1, "Gamma API (market discovery)")
        try:
            raw_markets = fetch_gamma_markets(client, args.limit, args.min_volume)
        except httpx.HTTPError as exc:
            log_err(f"Gamma API unreachable: {exc}")
            log_err("Likely causes: network, VPN, or geo-block. "
                    "Try from a non-US IP or check firewall.")
            return 2
        except ValueError as exc:
            log_err(f"Gamma returned unexpected shape: {exc}")
            return 3

        if not raw_markets:
            log_warn("Gamma returned 0 markets above the volume threshold. "
                     "Try lowering --min-volume.")
            return 3
        log_ok(f"fetched {len(raw_markets)} markets above ${args.min_volume:,.0f} volume")

        if args.verbose:
            print("\n  raw sample (first market):")
            print("  " + json.dumps(raw_markets[0], indent=2)[:1200].replace("\n", "\n  "))

        parsed = [m for m in (parse_market(r) for r in raw_markets) if m is not None]
        log_ok(f"parsed {len(parsed)} simple binary markets with 2 CLOB tokens")
        if not parsed:
            log_err("no parseable binary markets — Gamma schema may have changed.")
            return 3

        # ---- STEP 2: CLOB orderbook fetch ----
        log_step(2, "CLOB API (orderbook per market)")
        scan_rows: list[list[Any]] = []
        arb_candidates: list[list[Any]] = []
        clob_errors = 0

        for m in parsed:
            try:
                yes_book = fetch_orderbook(client, m.yes_token_id)
                no_book = fetch_orderbook(client, m.no_token_id)
            except httpx.HTTPError as exc:
                log_warn(f"{m.market_id} CLOB fetch failed: {exc}")
                clob_errors += 1
                continue

            yes_best = best_ask(yes_book)
            no_best = best_ask(no_book)
            if yes_best is None or no_best is None:
                scan_rows.append([m.market_id, m.question, "no liquidity", "-", "-", "-"])
                continue

            yes_px, yes_sz = yes_best
            no_px, no_sz = no_best
            edge = compute_type1_edge(yes_px, no_px, args.fee_bps, args.safety_bps)
            fillable = min(yes_sz, no_sz)

            scan_rows.append([
                m.market_id,
                m.question,
                f"{yes_px:.4f}",
                f"{no_px:.4f}",
                f"{(yes_px + no_px):.4f}",
                f"{edge*100:+.2f}%",
            ])
            if edge > 0:
                arb_candidates.append([
                    m.market_id,
                    m.question,
                    f"{yes_px:.4f} x {yes_sz:.0f}",
                    f"{no_px:.4f} x {no_sz:.0f}",
                    f"{edge*100:+.2f}%",
                    f"${fillable * (yes_px + no_px):.2f}",
                ])

        if clob_errors:
            log_warn(f"{clob_errors} CLOB fetches failed")
        if clob_errors == len(parsed):
            log_err("All CLOB fetches failed. CLOB unreachable.")
            return 4
        log_ok(f"CLOB reachable; scanned {len(parsed) - clob_errors} markets")

        # ---- STEP 3: report ----
        log_step(3, "Scan summary")
        print()
        print(tabulate(
            scan_rows,
            headers=["market_id", "question", "yes_ask", "no_ask", "sum", "edge*"],
            tablefmt="github",
            maxcolwidths=[12, 50, 10, 10, 10, 10],
        ))
        print(f"\n  * edge = 1 - (yes_ask + no_ask) - fee({args.fee_bps}bps) - safety({args.safety_bps}bps)")

        # ---- STEP 4: Type-1 arb candidates ----
        log_step(4, "Type-1 arb candidates (edge > 0)")
        if arb_candidates:
            print()
            print(tabulate(
                arb_candidates,
                headers=["market_id", "question", "yes_ask x size", "no_ask x size", "edge", "max_basket_cost"],
                tablefmt="github",
                maxcolwidths=[12, 40, 18, 18, 10, 16],
            ))
            print(f"\n  {len(arb_candidates)} candidate(s) detected — would trade in live mode.")
        else:
            print("  no arb detected in this scan (expected — arbs are rare and fleeting).")
            print("  this is fine: the goal of Phase 0 is to confirm connectivity, not find money.")

    print("\n" + "=" * 70)
    print("Phase 0 PASS: Gamma + CLOB reachable, data shape looks healthy.")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
