"""Analyze overnight data for Type-2 (three-way sum) arbitrage.

Type-2 arb: for a sports match with 3 outcomes (A wins, draw, B wins),
the sum of all three YES_ask prices should equal $1.00. If sum < $1.00,
buying all three YES tokens locks in a guaranteed profit.

This script:
  1. Reads all parquet snapshots from data/snapshots/
  2. Identifies "draw" markets (question contains "end in a draw")
  3. Parses team names → groups markets into match triples
  4. For each triple at each timestamp, computes the three-way sum
  5. Reports distribution, arb instances, and match-level stats

Usage:
  python analyze_type2.py
  python analyze_type2.py --data-dir /path/to/snapshots
  python analyze_type2.py --fee-bps 200   # with 2% fee assumption
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import duckdb
import pandas as pd

DEFAULT_DATA_DIR = Path("data/snapshots")
DEFAULT_FEE_BPS = 0.0
DEFAULT_SAFETY_BPS = 50.0

DRAW_PATTERN = re.compile(
    r"^Will\s+(.+?)\s+vs\.?\s+(.+?)\s+end in a draw",
    re.IGNORECASE,
)


def load_snapshots(data_dir: Path) -> pd.DataFrame:
    """Load all parquet snapshots via DuckDB (fast glob read)."""
    glob_path = str(data_dir / "**" / "*.parquet")
    db = duckdb.connect()
    df = db.sql(
        f"SELECT timestamp_ms, market_id, question, yes_best_ask_price, "
        f"yes_best_bid_price, no_best_ask_price "
        f"FROM read_parquet('{glob_path}')"
    ).fetchdf()
    db.close()
    return df


def find_match_groups(
    questions: list[str],
) -> dict[str, tuple[str, str, str]]:
    """Identify match triples from question text.

    Returns {match_key: (draw_question, team_a_win_question, team_b_win_question)}.
    Only complete triples (all 3 found) are returned.
    """
    # step 1: find draw markets, extract team names
    draw_markets: dict[str, tuple[str, str]] = {}  # draw_question -> (team_a, team_b)
    for q in questions:
        m = DRAW_PATTERN.match(q)
        if m:
            draw_markets[q] = (m.group(1).strip(), m.group(2).strip())

    # step 2: for each draw, find matching win markets
    groups: dict[str, tuple[str, str, str]] = {}
    incomplete = 0

    for draw_q, (team_a, team_b) in draw_markets.items():
        # require BOTH team names to avoid cross-match contamination
        # (e.g., "Will Leeds win vs Wolverhampton" not "Will Leeds win vs Chelsea")
        a_wins = [q for q in questions if q.startswith(f"Will {team_a} win") and team_b in q]
        b_wins = [q for q in questions if q.startswith(f"Will {team_b} win") and team_a in q]

        if a_wins and b_wins:
            key = f"{team_a} vs {team_b}"
            groups[key] = (draw_q, a_wins[0], b_wins[0])
        else:
            incomplete += 1

    return groups


def analyze_match(
    df: pd.DataFrame,
    match_key: str,
    draw_q: str,
    a_win_q: str,
    b_win_q: str,
    fee_bps: float,
    safety_bps: float,
) -> dict:
    """Compute three-way sum time series for one match."""
    draw = df[df["question"] == draw_q][["timestamp_ms", "yes_best_ask_price"]].rename(
        columns={"yes_best_ask_price": "draw_ask"}
    )
    a_win = df[df["question"] == a_win_q][["timestamp_ms", "yes_best_ask_price"]].rename(
        columns={"yes_best_ask_price": "a_win_ask"}
    )
    b_win = df[df["question"] == b_win_q][["timestamp_ms", "yes_best_ask_price"]].rename(
        columns={"yes_best_ask_price": "b_win_ask"}
    )

    merged = draw.merge(a_win, on="timestamp_ms").merge(b_win, on="timestamp_ms")
    if merged.empty:
        return {"match": match_key, "snapshots": 0}

    merged["three_way_sum"] = merged["draw_ask"] + merged["a_win_ask"] + merged["b_win_ask"]

    # edge: buy all 3 YES for sum, receive $1 at resolution
    fee_mult = 1.0 + fee_bps / 10_000.0
    safety = safety_bps / 10_000.0
    merged["edge"] = 1.0 - (merged["three_way_sum"] * fee_mult) - safety

    arb_mask = merged["edge"] > 0
    arb_count = arb_mask.sum()

    result = {
        "match": match_key,
        "snapshots": len(merged),
        "sum_min": merged["three_way_sum"].min(),
        "sum_max": merged["three_way_sum"].max(),
        "sum_mean": merged["three_way_sum"].mean(),
        "sum_std": merged["three_way_sum"].std(),
        "edge_max": merged["edge"].max(),
        "arb_count": int(arb_count),
    }

    if arb_count > 0:
        arb_rows = merged[arb_mask].sort_values("edge", ascending=False)
        best = arb_rows.iloc[0]
        result["best_arb_edge"] = best["edge"]
        result["best_arb_sum"] = best["three_way_sum"]
        result["best_arb_ts"] = int(best["timestamp_ms"])
        result["best_arb_prices"] = {
            "a_win": best["a_win_ask"],
            "draw": best["draw_ask"],
            "b_win": best["b_win_ask"],
        }
        # estimate how long the window lasted
        arb_timestamps = sorted(arb_rows["timestamp_ms"].tolist())
        if len(arb_timestamps) > 1:
            duration_sec = (arb_timestamps[-1] - arb_timestamps[0]) / 1000.0
            result["arb_window_sec"] = duration_sec
        else:
            result["arb_window_sec"] = 0.0

    return result


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", type=str, default=str(DEFAULT_DATA_DIR))
    p.add_argument("--fee-bps", type=float, default=DEFAULT_FEE_BPS)
    p.add_argument("--safety-bps", type=float, default=DEFAULT_SAFETY_BPS)
    args = p.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        print(f"ERROR: {data_dir} does not exist", file=sys.stderr)
        return 1

    print("=" * 65)
    print("Type-2 analysis: three-way sum across match outcomes")
    print("=" * 65)
    print(f"fee_bps={args.fee_bps}  safety_bps={args.safety_bps}")
    print()

    # load data
    print("Loading snapshots...")
    df = load_snapshots(data_dir)
    print(f"  {len(df):,} rows loaded, {df['market_id'].nunique()} unique markets")
    print(f"  {df['timestamp_ms'].nunique():,} unique timestamps")
    print()

    # find match groups
    unique_questions = df["question"].unique().tolist()
    groups = find_match_groups(unique_questions)
    print(f"Match groups found: {len(groups)} complete triples")
    if not groups:
        print("\nNo complete match triples found in the data.")
        print("This can happen if the data is from non-sports markets or")
        print("if the question format doesn't match the expected pattern.")
        return 1

    for key in sorted(groups.keys()):
        draw_q, a_q, b_q = groups[key]
        print(f"  {key}")

    # analyze each match
    print("\n" + "-" * 65)
    print("Per-match analysis")
    print("-" * 65)

    all_results = []
    total_arb = 0

    for key in sorted(groups.keys()):
        draw_q, a_q, b_q = groups[key]
        result = analyze_match(df, key, draw_q, a_q, b_q, args.fee_bps, args.safety_bps)
        all_results.append(result)

        if result["snapshots"] == 0:
            print(f"\n{key}: no overlapping timestamps (skipped)")
            continue

        arb_flag = " *** ARB FOUND ***" if result["arb_count"] > 0 else ""
        print(f"\n{key} ({result['snapshots']:,} snapshots){arb_flag}")
        print(f"  3-way sum: min={result['sum_min']:.4f}  max={result['sum_max']:.4f}  "
              f"avg={result['sum_mean']:.4f}  std={result['sum_std']:.4f}")
        print(f"  Best edge: {result['edge_max']*100:+.3f}%")
        print(f"  Arb snapshots (edge > 0): {result['arb_count']}")

        if result["arb_count"] > 0:
            total_arb += result["arb_count"]
            prices = result["best_arb_prices"]
            print(f"  >>> Best arb: sum={result['best_arb_sum']:.4f}, "
                  f"edge={result['best_arb_edge']*100:+.3f}%")
            print(f"      Prices: a_win={prices['a_win']:.3f}, "
                  f"draw={prices['draw']:.3f}, b_win={prices['b_win']:.3f}")
            if result.get("arb_window_sec", 0) > 0:
                print(f"      Window duration: {result['arb_window_sec']:.0f}s")

    # overall summary
    print("\n" + "=" * 65)
    print("OVERALL SUMMARY")
    print("=" * 65)

    valid = [r for r in all_results if r["snapshots"] > 0]
    if not valid:
        print("No valid match data found.")
        return 1

    all_sums_min = min(r["sum_min"] for r in valid)
    all_sums_max = max(r["sum_max"] for r in valid)
    all_edge_max = max(r["edge_max"] for r in valid)
    total_snapshots = sum(r["snapshots"] for r in valid)

    print(f"  Matches analyzed:     {len(valid)}")
    print(f"  Total match-snapshots:{total_snapshots:,}")
    print(f"  3-way sum range:      [{all_sums_min:.4f}, {all_sums_max:.4f}]")
    print(f"  Best edge observed:   {all_edge_max*100:+.3f}%")
    print(f"  Total arb snapshots:  {total_arb}")

    if total_arb > 0:
        print(f"\n  TYPE-2 ARB EXISTS! {total_arb} instances found across {len(valid)} matches.")
        print(f"  Next step: build execution layer (Phase 3) for three-way trades.")
    else:
        print(f"\n  No Type-2 arb found in this dataset.")
        print(f"  Closest to arb: sum={all_sums_min:.4f} (edge={all_edge_max*100:+.3f}%)")
        if all_sums_min < 1.02:
            print(f"  Sum dipped to {all_sums_min:.4f} — closer than Type-1 (always 1.01).")
            print(f"  Consider: longer polling, more matches, or lower safety buffer.")
        else:
            print(f"  Sum never went below 1.02 — similar structural floor as Type-1.")

    print("=" * 65)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
