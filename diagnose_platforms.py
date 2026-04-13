"""Diagnose: what does each platform actually return?

Shows side-by-side market data from Polymarket and SX Bet
so we can understand why matching fails and fix it.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from polymarket_client import PolymarketClient
from sxbet_client import SXBetClient


async def run() -> None:
    async with PolymarketClient() as poly, SXBetClient() as sx:

        # ── SX Bet side ──
        print("=" * 70)
        print("SX BET: Active sports & markets")
        print("=" * 70)

        sports = await sx.get_sports()
        print(f"\nSports ({len(sports)}):")
        for s in sports:
            print(f"  [{s.sport_id}] {s.label}")

        leagues = await sx.get_leagues()
        print(f"\nActive leagues ({len(leagues)}):")
        for lg in leagues[:20]:
            print(f"  [{lg.league_id}] {lg.label} (sport={lg.sport_id})")
        if len(leagues) > 20:
            print(f"  ... +{len(leagues) - 20} more")

        # get markets for first few leagues
        sx_all_markets = await sx.get_active_markets()
        print(f"\nTotal active markets: {len(sx_all_markets)}")

        # show moneyline markets with team names
        moneyline = [m for m in sx_all_markets if m.market_type == 1]
        print(f"Moneyline markets: {len(moneyline)}")
        print("\nSample SX Bet moneyline markets (first 20):")
        for m in moneyline[:20]:
            print(f"  {m.team_one_name} vs {m.team_two_name}")
            print(f"    league={m.league_label}  sport={m.sport_label}")
            print(f"    outcomes: {m.outcome_one_name} / {m.outcome_two_name}")

        # ── Polymarket side ──
        print("\n" + "=" * 70)
        print("POLYMARKET: Sports-related markets")
        print("=" * 70)

        poly_markets = await poly.get_markets(limit=100, min_liquidity=10_000)
        print(f"\nTotal markets (liq>$10k): {len(poly_markets)}")

        # show all categories
        categories = {}
        for m in poly_markets:
            cat = m.category or "unknown"
            categories.setdefault(cat, []).append(m)
        print(f"\nCategories:")
        for cat, ms in sorted(categories.items(), key=lambda x: -len(x[1])):
            print(f"  {cat}: {len(ms)} markets")

        # show sports-related markets
        sports_kw = ["win", "draw", "match", "vs", "defeat", "beat",
                     "champion", "cup", "league", "premier", "serie",
                     "la liga", "bundesliga", "nba", "nfl", "mlb",
                     "soccer", "football", "tennis", "ufc", "boxing"]
        poly_sports = [
            m for m in poly_markets
            if any(kw in m.question.lower() for kw in sports_kw)
        ]
        print(f"\nSports-keyword markets: {len(poly_sports)}")
        print("\nSample Polymarket sports markets (first 30):")
        for m in poly_sports[:30]:
            print(f"  [{m.category}] {m.question[:80]}")

        # ── Compare team names ──
        if moneyline and poly_sports:
            print("\n" + "=" * 70)
            print("TEAM NAME COMPARISON")
            print("=" * 70)

            sx_teams = set()
            for m in moneyline:
                sx_teams.add(m.team_one_name)
                sx_teams.add(m.team_two_name)

            print(f"\nUnique SX Bet teams: {len(sx_teams)}")
            print("Sample:", sorted(list(sx_teams))[:15])

            print(f"\nPolymarket questions containing SX team names:")
            found = 0
            for team in sorted(sx_teams):
                team_lower = team.lower()
                for pm in poly_sports:
                    if team_lower in pm.question.lower():
                        print(f"  SX team '{team}' found in: {pm.question[:70]}")
                        found += 1
            if found == 0:
                print("  NONE — no overlap in team names between platforms")
                print("\n  This means either:")
                print("  1. The platforms cover different sports/leagues")
                print("  2. Team name formatting is too different")
                print("  3. Markets have different time horizons")


def main() -> int:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run())
        return 0
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
