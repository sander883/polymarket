"""Phase 0: verify Polymarket API access from the local environment.

Refactored in Phase 1.1 to use PolymarketClient — same output, same exit
codes, but now also serves as an integration check on the client module.

Usage
-----
  python verify_polymarket_access.py
  python verify_polymarket_access.py --limit 20 --min-liquidity 50000
  python verify_polymarket_access.py --fee-bps 200 --safety-bps 50
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

from tabulate import tabulate

from polymarket_client import (
    PolymarketAPIError,
    PolymarketClient,
    PolymarketError,
    PolymarketNetworkError,
    PolymarketParseError,
)

DEFAULT_LIMIT = 10
DEFAULT_MIN_LIQUIDITY = 10_000.0  # USD
DEFAULT_FEE_BPS = 0               # Polymarket historically 0% trading fee; verify current
DEFAULT_SAFETY_BPS = 50           # 0.5% buffer for slippage / execution


def log_step(n: int, msg: str) -> None:
    print(f"\n[STEP {n}] {msg}")


def log_ok(msg: str) -> None:
    print(f"  OK    {msg}")


def log_warn(msg: str) -> None:
    print(f"  WARN  {msg}")


def log_err(msg: str) -> None:
    print(f"  ERROR {msg}", file=sys.stderr)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    p.add_argument("--min-liquidity", type=float, default=DEFAULT_MIN_LIQUIDITY)
    p.add_argument("--fee-bps", type=float, default=DEFAULT_FEE_BPS)
    p.add_argument("--safety-bps", type=float, default=DEFAULT_SAFETY_BPS)
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args()


def compute_type1_edge(yes_ask: float, no_ask: float, fee_bps: float, safety_bps: float) -> float:
    cost = yes_ask + no_ask
    fee = cost * (fee_bps / 10_000.0)
    safety = safety_bps / 10_000.0
    return 1.0 - cost - fee - safety


async def run(args: argparse.Namespace) -> int:
    print("=" * 70)
    print("Polymarket access verification (Phase 0)")
    print("=" * 70)
    print(
        f"limit={args.limit}  min_liquidity=${args.min_liquidity:,.0f}  "
        f"fee_bps={args.fee_bps}  safety_bps={args.safety_bps}"
    )

    async with PolymarketClient() as client:
        # ---- STEP 1: Gamma API (market discovery) ----
        log_step(1, "Gamma API (market discovery)")
        t0 = time.perf_counter()
        try:
            markets = await client.get_markets(
                limit=args.limit,
                min_liquidity=args.min_liquidity,
                order="liquidityNum",  # avoids dead-tail novelty sort
            )
        except PolymarketNetworkError as exc:
            log_err(f"Gamma unreachable: {exc}")
            log_err("Likely causes: network, VPN, or geo-block. "
                    "Try from a non-US IP or check firewall.")
            return 2
        except PolymarketAPIError as exc:
            log_err(f"Gamma API error: {exc}")
            return 2
        except PolymarketParseError as exc:
            log_err(f"Gamma returned unexpected shape: {exc}")
            return 3
        elapsed = (time.perf_counter() - t0) * 1000
        log_ok(f"fetched {len(markets)} markets in {elapsed:.0f} ms")
        if not markets:
            log_warn("0 markets above liquidity threshold. Try lowering --min-liquidity.")
            return 3

        binary = [m for m in markets if m.is_binary]
        log_ok(f"{len(binary)}/{len(markets)} are simple binary markets with 2 CLOB tokens")
        if not binary:
            log_err("no parseable binary markets — Gamma schema may have changed.")
            return 3

        if args.verbose:
            print("\n  raw sample (first market):")
            print("  " + json.dumps(binary[0].raw, indent=2)[:1200].replace("\n", "\n  "))

        # ---- STEP 2: CLOB orderbook fetch (batched) ----
        log_step(2, "CLOB API (orderbook per market, batched)")
        t0 = time.perf_counter()
        token_ids: list[str] = []
        for m in binary:
            if m.yes_token_id:
                token_ids.append(m.yes_token_id)
            if m.no_token_id:
                token_ids.append(m.no_token_id)
        try:
            books = await client.get_orderbooks(token_ids)
        except PolymarketError as exc:
            log_err(f"CLOB batch failed: {exc}")
            return 4
        elapsed = (time.perf_counter() - t0) * 1000
        log_ok(f"fetched {len(books)}/{len(token_ids)} books in {elapsed:.0f} ms")
        if not books:
            log_err("All CLOB fetches failed. CLOB unreachable.")
            return 4

        # ---- STEP 3: Scan summary ----
        log_step(3, "Scan summary")
        scan_rows: list[list[object]] = []
        arb_candidates: list[list[object]] = []

        for m in binary:
            if not (m.yes_token_id and m.no_token_id):
                continue
            yes_book = books.get(m.yes_token_id)
            no_book = books.get(m.no_token_id)
            if yes_book is None or no_book is None:
                scan_rows.append([m.market_id, m.question, "book missing", "-", "-", "-"])
                continue

            yes_best = yes_book.best_ask
            no_best = no_book.best_ask
            if yes_best is None or no_best is None:
                scan_rows.append([m.market_id, m.question, "no liquidity", "-", "-", "-"])
                continue

            edge = compute_type1_edge(yes_best.price, no_best.price, args.fee_bps, args.safety_bps)
            fillable = min(yes_best.size, no_best.size)

            scan_rows.append([
                m.market_id,
                m.question,
                f"{yes_best.price:.4f}",
                f"{no_best.price:.4f}",
                f"{(yes_best.price + no_best.price):.4f}",
                f"{edge*100:+.2f}%",
            ])
            if edge > 0:
                arb_candidates.append([
                    m.market_id,
                    m.question,
                    f"{yes_best.price:.4f} x {yes_best.size:.0f}",
                    f"{no_best.price:.4f} x {no_best.size:.0f}",
                    f"{edge*100:+.2f}%",
                    f"${fillable * (yes_best.price + no_best.price):.2f}",
                ])

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
            print(f"\n  {len(arb_candidates)} candidate(s) detected.")
        else:
            print("  no arb detected in this scan (expected — see README Phase 0 findings).")

    print("\n" + "=" * 70)
    print("Phase 0 PASS: Gamma + CLOB reachable, data shape looks healthy.")
    print("=" * 70)
    return 0


def main() -> int:
    args = parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
