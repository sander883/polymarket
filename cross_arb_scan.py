"""Cross-platform arb scanner: Polymarket vs SX Bet.

Fetches soccer (football) markets from both platforms, matches events
by team name similarity, and compares odds to find price discrepancies.

If Polymarket says "Team A win" = $0.45 (YES ask) and SX Bet says the
same outcome is available at $0.40 implied — the gap is exploitable.

Usage:
  python cross_arb_scan.py
  python cross_arb_scan.py --sport soccer
  python cross_arb_scan.py -v            # verbose logging
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher

from polymarket_client import PolymarketClient, PolymarketError
from sxbet_client import SXBetClient, SXBetError, SXMarket, SXOdds

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Event matching
# ---------------------------------------------------------------------------


def _normalize(name: str) -> str:
    """Normalize team name for fuzzy matching."""
    name = name.lower().strip()
    # remove common suffixes
    for suffix in (" fc", " cf", " afc", " sc", " ssc", " calcio"):
        if name.endswith(suffix):
            name = name[: -len(suffix)].strip()
    return name


def _similarity(a: str, b: str) -> float:
    """String similarity ratio (0..1)."""
    return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


@dataclass
class MatchedEvent:
    """A matched event across both platforms."""

    # Polymarket side
    poly_question: str
    poly_yes_ask: float  # price to buy YES on Polymarket
    poly_no_ask: float   # price to buy NO on Polymarket
    poly_market_id: str

    # SX Bet side
    sx_team_one: str
    sx_team_two: str
    sx_outcome_one_price: float | None  # implied odds for outcome 1
    sx_outcome_two_price: float | None  # implied odds for outcome 2
    sx_market_hash: str
    sx_league: str

    # matching quality
    team_similarity: float

    def edges(self) -> list[dict]:
        """Calculate cross-platform edges.

        Edge = what you'd pay on the cheaper platform vs what you'd receive
        on the other. Positive edge = profit opportunity.
        """
        results = []

        # Check: buy YES on Polymarket, bet opposite on SX Bet
        # If Polymarket YES ask < SX Bet implied price for same outcome → edge
        if self.sx_outcome_one_price is not None and self.poly_yes_ask > 0:
            # Polymarket YES ≈ SX outcome 1 (team 1 wins)
            # Buy YES on cheaper platform
            edge_poly_yes = self.sx_outcome_one_price - self.poly_yes_ask
            if abs(edge_poly_yes) > 0.005:
                results.append({
                    "type": "outcome1",
                    "poly_price": self.poly_yes_ask,
                    "sx_price": self.sx_outcome_one_price,
                    "edge": edge_poly_yes,
                    "action": "buy Poly YES" if edge_poly_yes > 0 else "buy SX outcome1",
                })

        if self.sx_outcome_two_price is not None and self.poly_no_ask > 0:
            # Polymarket NO ≈ SX outcome 2 (team 2 wins / other outcome)
            edge_poly_no = self.sx_outcome_two_price - self.poly_no_ask
            if abs(edge_poly_no) > 0.005:
                results.append({
                    "type": "outcome2",
                    "poly_price": self.poly_no_ask,
                    "sx_price": self.sx_outcome_two_price,
                    "edge": edge_poly_no,
                    "action": "buy Poly NO" if edge_poly_no > 0 else "buy SX outcome2",
                })

        return results


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


async def scan_soccer(
    *,
    min_similarity: float = 0.60,
    verbose: bool = False,
) -> list[MatchedEvent]:
    """Scan for cross-platform arb in soccer markets."""

    async with PolymarketClient() as poly_client, SXBetClient() as sx_client:
        # ── Fetch Polymarket soccer markets ──
        print("Fetching Polymarket markets...")
        poly_markets = await poly_client.get_markets(
            limit=100,
            min_liquidity=10_000,
        )
        # filter to questions that look like sports/win/draw
        sports_keywords = ["win", "draw", "match", "vs", "defeat"]
        poly_sports = [
            m for m in poly_markets
            if any(kw in m.question.lower() for kw in sports_keywords)
        ]
        print(f"  Polymarket: {len(poly_markets)} total, {len(poly_sports)} sports-related")

        # ── Fetch SX Bet soccer markets ──
        print("Fetching SX Bet markets...")
        sx_sports = await sx_client.get_sports()
        soccer_sport = next((s for s in sx_sports if s.label.lower() in ("soccer", "football")), None)

        if soccer_sport is None:
            print("  SX Bet: no soccer sport found. Available sports:")
            for s in sx_sports:
                print(f"    [{s.sport_id}] {s.label}")
            # try all sports
            sx_markets = await sx_client.get_active_markets()
        else:
            print(f"  SX Bet soccer sport_id: {soccer_sport.sport_id}")
            sx_markets = await sx_client.get_active_markets(sport_id=soccer_sport.sport_id)

        # filter to moneyline markets (type=1: straight win/lose)
        sx_moneyline = [m for m in sx_markets if m.market_type == 1]
        print(f"  SX Bet: {len(sx_markets)} total, {len(sx_moneyline)} moneyline")

        if not poly_sports:
            print("\nNo sports markets found on Polymarket. Showing all available:")
            for m in poly_markets[:20]:
                print(f"  {m.question[:70]}  (liq={m.liquidity:,.0f})")
            return []

        if not sx_moneyline:
            print("\nNo moneyline markets found on SX Bet.")
            return []

        # ── Fetch odds for SX Bet markets ──
        print("\nFetching SX Bet odds...")
        sx_hashes = [m.market_hash for m in sx_moneyline]
        sx_odds_map = await sx_client.get_odds(sx_hashes)

        # ── Fetch Polymarket orderbooks ──
        print("Fetching Polymarket orderbooks...")
        poly_token_ids = []
        for m in poly_sports:
            if m.yes_token_id:
                poly_token_ids.append(m.yes_token_id)
            if m.no_token_id:
                poly_token_ids.append(m.no_token_id)
        poly_books = await poly_client.get_orderbooks(poly_token_ids)

        # ── Match events by team name similarity ──
        print("\nMatching events across platforms...")
        matched: list[MatchedEvent] = []

        for pm in poly_sports:
            q_lower = pm.question.lower()
            best_match: tuple[float, SXMarket | None] = (0.0, None)

            for sx in sx_moneyline:
                # try matching team names against Polymarket question
                sim1 = _similarity(sx.team_one_name, pm.question)
                sim2 = _similarity(sx.team_two_name, pm.question)
                combined = max(sim1, sim2)

                # also try matching both team names
                if _normalize(sx.team_one_name) in _normalize(pm.question):
                    combined = max(combined, 0.7)
                if _normalize(sx.team_two_name) in _normalize(pm.question):
                    combined = max(combined, 0.7)

                if combined > best_match[0]:
                    best_match = (combined, sx)

            sim_score, sx_match = best_match
            if sx_match and sim_score >= min_similarity:
                odds = sx_odds_map.get(sx_match.market_hash)
                yes_book = poly_books.get(pm.yes_token_id) if pm.yes_token_id else None
                no_book = poly_books.get(pm.no_token_id) if pm.no_token_id else None

                poly_yes_ask = yes_book.best_ask.price if yes_book and yes_book.best_ask else 0.0
                poly_no_ask = no_book.best_ask.price if no_book and no_book.best_ask else 0.0

                event = MatchedEvent(
                    poly_question=pm.question,
                    poly_yes_ask=poly_yes_ask,
                    poly_no_ask=poly_no_ask,
                    poly_market_id=pm.market_id,
                    sx_team_one=sx_match.team_one_name,
                    sx_team_two=sx_match.team_two_name,
                    sx_outcome_one_price=odds.outcome_one_best if odds else None,
                    sx_outcome_two_price=odds.outcome_two_best if odds else None,
                    sx_market_hash=sx_match.market_hash,
                    sx_league=sx_match.league_label,
                    team_similarity=sim_score,
                )
                matched.append(event)

                if verbose:
                    print(f"  MATCH (sim={sim_score:.2f}): "
                          f"Poly='{pm.question[:50]}' <-> "
                          f"SX='{sx_match.team_one_name} vs {sx_match.team_two_name}'")

        print(f"\nMatched events: {len(matched)}")
        return matched


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def print_report(matched: list[MatchedEvent]) -> int:
    """Print arb scan results. Returns count of arb opportunities."""
    print("\n" + "=" * 70)
    print("CROSS-PLATFORM ARB SCAN: Polymarket vs SX Bet")
    print("=" * 70)

    if not matched:
        print("\nNo matched events found.")
        print("This can happen if:")
        print("  - Polymarket and SX Bet don't share overlapping events right now")
        print("  - Team name formats differ too much for matching")
        print("  - One platform has no active sports markets")
        return 0

    arb_count = 0

    for event in sorted(matched, key=lambda e: e.team_similarity, reverse=True):
        edges = event.edges()

        sx_o1 = f"{event.sx_outcome_one_price:.3f}" if event.sx_outcome_one_price else "n/a"
        sx_o2 = f"{event.sx_outcome_two_price:.3f}" if event.sx_outcome_two_price else "n/a"

        arb_flag = ""
        if any(e["edge"] > 0.01 for e in edges):
            arb_flag = " *** POTENTIAL ARB ***"
            arb_count += 1

        print(f"\n  {event.sx_team_one} vs {event.sx_team_two} [{event.sx_league}]"
              f" (match={event.team_similarity:.0%}){arb_flag}")
        print(f"    Poly: '{event.poly_question[:60]}'")
        print(f"    Poly prices:  YES_ask={event.poly_yes_ask:.3f}  NO_ask={event.poly_no_ask:.3f}")
        print(f"    SX prices:    out1={sx_o1}  out2={sx_o2}")

        if edges:
            for e in edges:
                direction = "+" if e["edge"] > 0 else ""
                print(f"    Edge ({e['type']}): poly={e['poly_price']:.3f} vs "
                      f"sx={e['sx_price']:.3f} → {direction}{e['edge']*100:.2f}% "
                      f"[{e['action']}]")
        else:
            print(f"    No significant price gap detected")

    # summary
    print("\n" + "=" * 70)
    print(f"SUMMARY: {len(matched)} matched events, {arb_count} with edge > 1%")
    if arb_count > 0:
        print("Next: validate match accuracy, check liquidity depth, then execute.")
    else:
        print("No actionable arb found in this scan.")
        print("Consider: broader sport coverage, lower similarity threshold, or re-scan later.")
    print("=" * 70)

    return arb_count


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


async def _run(verbose: bool) -> int:
    try:
        matched = await scan_soccer(verbose=verbose)
        arb_count = print_report(matched)
        return 0
    except (PolymarketError, SXBetError) as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1


def main() -> int:
    import argparse
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    return asyncio.run(_run(args.verbose))


if __name__ == "__main__":
    raise SystemExit(main())
