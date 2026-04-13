"""Debug: raw API response from SX Bet odds + EPL matching test.

1. Fetches EPL markets from SX Bet (league_id=29)
2. Dumps raw JSON response from /orders/odds/best to debug parsing
3. Fetches Polymarket sports markets and tries matching EPL teams
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys

import httpx

from polymarket_client import PolymarketClient
from sxbet_client import SX_API_URL, SX_USDC_ADDRESS, SXBetClient

EPL_LEAGUE_ID = 29
SERIE_A_LEAGUE_ID = 1113
LA_LIGA_LEAGUE_ID = 1114


async def run() -> None:
    async with SXBetClient() as sx, PolymarketClient() as poly:

        # ── 1. Fetch EPL markets from SX Bet ──
        print("=" * 70)
        print("STEP 1: SX Bet — EPL, Serie A, La Liga markets")
        print("=" * 70)

        for league_name, league_id in [
            ("EPL", EPL_LEAGUE_ID),
            ("Serie A", SERIE_A_LEAGUE_ID),
            ("La Liga", LA_LIGA_LEAGUE_ID),
        ]:
            markets = await sx.get_active_markets(league_id=league_id)
            moneyline = [m for m in markets if m.market_type == 1]
            print(f"\n  {league_name} (id={league_id}): {len(markets)} total, {len(moneyline)} moneyline")
            for m in moneyline[:5]:
                print(f"    {m.team_one_name} vs {m.team_two_name} [{m.outcome_one_name}/{m.outcome_two_name}]")

        # ── 2. Raw odds API response for first few markets ──
        print("\n" + "=" * 70)
        print("STEP 2: Raw /orders/odds/best response (debug)")
        print("=" * 70)

        # get a few markets from any league that has them
        all_markets = await sx.get_active_markets()
        moneyline_all = [m for m in all_markets if m.market_type == 1]
        sample_hashes = [m.market_hash for m in moneyline_all[:3]]

        if sample_hashes:
            # make raw request to see exact response format
            async with httpx.AsyncClient(timeout=15.0) as raw_client:
                params = {
                    "marketHashes": ",".join(sample_hashes),
                    "baseToken": SX_USDC_ADDRESS,
                }
                url = f"{SX_API_URL}/orders/odds/best"
                print(f"\n  GET {url}")
                print(f"  params: {params}")

                resp = await raw_client.get(url, params=params)
                print(f"  status: {resp.status_code}")
                print(f"  response (first 2000 chars):")
                try:
                    body = resp.json()
                    print(json.dumps(body, indent=2)[:2000])
                except Exception:
                    print(resp.text[:2000])

                # also try without baseToken
                print(f"\n  Trying WITHOUT baseToken...")
                params2 = {"marketHashes": ",".join(sample_hashes)}
                resp2 = await raw_client.get(url, params=params2)
                print(f"  status: {resp2.status_code}")
                if resp2.status_code == 200:
                    try:
                        body2 = resp2.json()
                        print(json.dumps(body2, indent=2)[:2000])
                    except Exception:
                        print(resp2.text[:2000])
                else:
                    print(f"  error: {resp2.text[:500]}")

                # try /orders endpoint
                print(f"\n  Trying /orders endpoint...")
                orders_url = f"{SX_API_URL}/orders"
                resp3 = await raw_client.get(orders_url, params={
                    "marketHashes": sample_hashes[0],
                    "baseToken": SX_USDC_ADDRESS,
                })
                print(f"  status: {resp3.status_code}")
                try:
                    body3 = resp3.json()
                    print(json.dumps(body3, indent=2)[:2000])
                except Exception:
                    print(resp3.text[:2000])

        # ── 3. Try /metadata to get correct baseToken ──
        print("\n" + "=" * 70)
        print("STEP 3: /metadata (check baseToken addresses)")
        print("=" * 70)

        async with httpx.AsyncClient(timeout=15.0) as raw_client:
            resp = await raw_client.get(f"{SX_API_URL}/metadata")
            print(f"  status: {resp.status_code}")
            if resp.status_code == 200:
                try:
                    meta = resp.json()
                    print(json.dumps(meta, indent=2)[:3000])
                except Exception:
                    print(resp.text[:3000])

        # ── 4. Polymarket EPL overlap check ──
        print("\n" + "=" * 70)
        print("STEP 4: Polymarket — EPL team overlap check")
        print("=" * 70)

        poly_markets = await poly.get_markets(limit=200, min_liquidity=1_000)
        print(f"  Polymarket markets (liq>$1k): {len(poly_markets)}")

        # extract team names from SX Bet
        sx_teams = set()
        for m in moneyline_all:
            sx_teams.add(m.team_one_name.lower())
            sx_teams.add(m.team_two_name.lower())

        # search Polymarket questions for any SX team name
        matches_found = 0
        for pm in poly_markets:
            q_lower = pm.question.lower()
            for team in sx_teams:
                # try partial match (e.g., "manchester united" in question)
                words = team.split()
                if len(words) >= 2 and words[0] in q_lower and words[1] in q_lower:
                    print(f"  MATCH: SX team '{team}' <-> Poly '{pm.question[:70]}'")
                    matches_found += 1
                    break

        if matches_found == 0:
            # also try just the first significant word
            print("\n  No exact matches. Trying fuzzy single-word match:")
            for pm in poly_markets:
                q_lower = pm.question.lower()
                for team in sx_teams:
                    # use the most distinctive word (usually city/club name)
                    for word in team.split():
                        if len(word) > 4 and word in q_lower:
                            print(f"  FUZZY: '{word}' (from SX '{team}') in Poly '{pm.question[:70]}'")
                            matches_found += 1
                            break

        print(f"\n  Total matches found: {matches_found}")


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(run())
        return 0
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
