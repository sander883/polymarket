"""Validate Type-2 arb findings — check for data quality issues.

The initial analyze_type2.py found 2198 arb instances with edge up to +41.5%
and sum as low as 0.58. These numbers are suspiciously high — real arbs of
that magnitude would be arbitraged instantly. This script digs deeper:

  1. Shows the exact question text matched in each triple (catch mis-matches)
  2. For each arb instance, shows individual leg prices (catch zero/stale legs)
  3. Checks YES ask size (liquidity) on arb legs
  4. Filters to "real" arbs where all 3 legs have price >= 0.10 and size > 0
  5. Compares: how many arbs survive the quality filter?

Usage:
  python validate_type2.py
  python validate_type2.py --data-dir /path/to/snapshots
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import duckdb
import pandas as pd

DEFAULT_DATA_DIR = Path("data/snapshots")

DRAW_PATTERN = re.compile(
    r"^Will\s+(.+?)\s+vs\.?\s+(.+?)\s+end in a draw",
    re.IGNORECASE,
)

WIN_PATTERN = re.compile(
    r"^Will\s+(.+?)\s+win on\s+(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)


def load_snapshots(data_dir: Path) -> pd.DataFrame:
    glob_path = str(data_dir / "**" / "*.parquet")
    db = duckdb.connect()
    df = db.sql(
        f"SELECT timestamp_ms, market_id, question, "
        f"yes_best_ask_price, yes_best_ask_size, "
        f"yes_best_bid_price, "
        f"no_best_ask_price, "
        f"yes_depth_5_ask, liquidity "
        f"FROM read_parquet('{glob_path}')"
    ).fetchdf()
    db.close()
    return df


def find_match_groups(questions: list[str]) -> dict[str, tuple[str, str, str]]:
    draw_markets: dict[str, tuple[str, str]] = {}
    for q in questions:
        m = DRAW_PATTERN.match(q)
        if m:
            draw_markets[q] = (m.group(1).strip(), m.group(2).strip())

    win_by_team: dict[str, dict[str, str]] = {}
    for q in questions:
        m = WIN_PATTERN.match(q)
        if m:
            team = m.group(1).strip()
            date = m.group(2)
            win_by_team.setdefault(team, {})[date] = q

    groups: dict[str, tuple[str, str, str]] = {}
    for draw_q, (team_a, team_b) in draw_markets.items():
        a_dates = win_by_team.get(team_a, {})
        b_dates = win_by_team.get(team_b, {})
        common_dates = set(a_dates.keys()) & set(b_dates.keys())
        if common_dates:
            date = sorted(common_dates)[0]
            key = f"{team_a} vs {team_b} ({date})"
            groups[key] = (draw_q, a_dates[date], b_dates[date])

    return groups


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data-dir", type=str, default=str(DEFAULT_DATA_DIR))
    p.add_argument("--min-leg-price", type=float, default=0.10,
                   help="minimum YES ask price per leg to count as 'real' (default 0.10)")
    p.add_argument("--min-leg-size", type=float, default=1.0,
                   help="minimum YES ask size per leg to count as 'real' (default 1.0)")
    args = p.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        print(f"ERROR: {data_dir} does not exist", file=sys.stderr)
        return 1

    print("=" * 70)
    print("Type-2 arb VALIDATION — checking data quality")
    print("=" * 70)
    print(f"min_leg_price={args.min_leg_price}  min_leg_size={args.min_leg_size}")
    print()

    # ── load ──
    print("Loading snapshots...")
    df = load_snapshots(data_dir)
    print(f"  {len(df):,} rows loaded")
    print()

    # ── match groups ──
    unique_questions = df["question"].unique().tolist()
    groups = find_match_groups(unique_questions)
    if not groups:
        print("No match triples found.")
        return 1

    # ── CHECK 1: show matched questions ──
    print("-" * 70)
    print("CHECK 1: Matched question triples (verify correctness)")
    print("-" * 70)
    for key in sorted(groups.keys()):
        draw_q, a_q, b_q = groups[key]
        print(f"\n  Match: {key}")
        print(f"    A wins: {a_q[:80]}")
        print(f"    Draw:   {draw_q[:80]}")
        print(f"    B wins: {b_q[:80]}")

    # ── CHECK 2: per-match price distributions ──
    print("\n" + "-" * 70)
    print("CHECK 2: Individual leg price distributions")
    print("-" * 70)

    total_raw_arb = 0
    total_real_arb = 0
    match_details = []

    for key in sorted(groups.keys()):
        draw_q, a_q, b_q = groups[key]

        draw = df[df["question"] == draw_q][
            ["timestamp_ms", "yes_best_ask_price", "yes_best_ask_size"]
        ].rename(columns={"yes_best_ask_price": "draw_ask", "yes_best_ask_size": "draw_size"})

        a_win = df[df["question"] == a_q][
            ["timestamp_ms", "yes_best_ask_price", "yes_best_ask_size"]
        ].rename(columns={"yes_best_ask_price": "a_ask", "yes_best_ask_size": "a_size"})

        b_win = df[df["question"] == b_q][
            ["timestamp_ms", "yes_best_ask_price", "yes_best_ask_size"]
        ].rename(columns={"yes_best_ask_price": "b_ask", "yes_best_ask_size": "b_size"})

        merged = draw.merge(a_win, on="timestamp_ms").merge(b_win, on="timestamp_ms")
        if merged.empty:
            continue

        merged["three_sum"] = merged["draw_ask"] + merged["a_ask"] + merged["b_ask"]

        # raw arb (sum < 1.0)
        raw_arb = merged[merged["three_sum"] < 1.0]
        raw_count = len(raw_arb)

        # "real" arb: all 3 legs have price >= threshold AND size >= threshold
        if raw_count > 0:
            real_mask = (
                (raw_arb["draw_ask"] >= args.min_leg_price) &
                (raw_arb["a_ask"] >= args.min_leg_price) &
                (raw_arb["b_ask"] >= args.min_leg_price) &
                (raw_arb["draw_size"] >= args.min_leg_size) &
                (raw_arb["a_size"] >= args.min_leg_size) &
                (raw_arb["b_size"] >= args.min_leg_size)
            )
            real_arb = raw_arb[real_mask]
            real_count = len(real_arb)
        else:
            real_arb = pd.DataFrame()
            real_count = 0

        total_raw_arb += raw_count
        total_real_arb += real_count

        print(f"\n  {key} ({len(merged):,} snapshots)")
        print(f"    Leg prices (min/mean/max):")
        print(f"      A win:  {merged['a_ask'].min():.3f} / {merged['a_ask'].mean():.3f} / {merged['a_ask'].max():.3f}")
        print(f"      Draw:   {merged['draw_ask'].min():.3f} / {merged['draw_ask'].mean():.3f} / {merged['draw_ask'].max():.3f}")
        print(f"      B win:  {merged['b_ask'].min():.3f} / {merged['b_ask'].mean():.3f} / {merged['b_ask'].max():.3f}")
        print(f"    Leg sizes (min/mean/max):")
        print(f"      A win:  {merged['a_size'].min():.1f} / {merged['a_size'].mean():.1f} / {merged['a_size'].max():.1f}")
        print(f"      Draw:   {merged['draw_size'].min():.1f} / {merged['draw_size'].mean():.1f} / {merged['draw_size'].max():.1f}")
        print(f"      B win:  {merged['b_size'].min():.1f} / {merged['b_size'].mean():.1f} / {merged['b_size'].max():.1f}")
        print(f"    3-way sum: {merged['three_sum'].min():.4f} .. {merged['three_sum'].max():.4f}")
        print(f"    Raw arb (sum<1.0):  {raw_count}")
        print(f"    Real arb (filtered): {real_count}")

        # show worst offenders — arbs with suspiciously low legs
        if raw_count > 0 and real_count < raw_count:
            fake_arb = raw_arb[~raw_arb.index.isin(real_arb.index)]
            sample = fake_arb.nsmallest(3, "three_sum")
            print(f"    Fake arb examples (low sum caused by bad data):")
            for _, row in sample.iterrows():
                print(f"      sum={row['three_sum']:.3f}  "
                      f"a={row['a_ask']:.3f}(sz={row['a_size']:.0f})  "
                      f"draw={row['draw_ask']:.3f}(sz={row['draw_size']:.0f})  "
                      f"b={row['b_ask']:.3f}(sz={row['b_size']:.0f})")

        # show best real arbs
        if real_count > 0:
            best_real = real_arb.nsmallest(5, "three_sum")
            print(f"    Best REAL arb examples:")
            for _, row in best_real.iterrows():
                edge = 1.0 - row["three_sum"]
                print(f"      sum={row['three_sum']:.4f} edge={edge*100:+.2f}%  "
                      f"a={row['a_ask']:.3f}(sz={row['a_size']:.0f})  "
                      f"draw={row['draw_ask']:.3f}(sz={row['draw_size']:.0f})  "
                      f"b={row['b_ask']:.3f}(sz={row['b_size']:.0f})")

        match_details.append({
            "match": key,
            "snapshots": len(merged),
            "raw_arb": raw_count,
            "real_arb": real_count,
            "min_sum": merged["three_sum"].min(),
        })

    # ── CHECK 3: overall price distribution for arb rows ──
    print("\n" + "-" * 70)
    print("CHECK 3: Price distribution of ALL arb legs")
    print("-" * 70)

    # collect all arb rows across matches
    all_arb_legs = []
    for key in sorted(groups.keys()):
        draw_q, a_q, b_q = groups[key]
        draw = df[df["question"] == draw_q][
            ["timestamp_ms", "yes_best_ask_price"]
        ].rename(columns={"yes_best_ask_price": "draw_ask"})
        a_win = df[df["question"] == a_q][
            ["timestamp_ms", "yes_best_ask_price"]
        ].rename(columns={"yes_best_ask_price": "a_ask"})
        b_win = df[df["question"] == b_q][
            ["timestamp_ms", "yes_best_ask_price"]
        ].rename(columns={"yes_best_ask_price": "b_ask"})
        merged = draw.merge(a_win, on="timestamp_ms").merge(b_win, on="timestamp_ms")
        if not merged.empty:
            merged["three_sum"] = merged["draw_ask"] + merged["a_ask"] + merged["b_ask"]
            arb_rows = merged[merged["three_sum"] < 1.0]
            if not arb_rows.empty:
                all_arb_legs.append(arb_rows)

    if all_arb_legs:
        all_arb = pd.concat(all_arb_legs, ignore_index=True)
        min_per_leg = all_arb[["a_ask", "draw_ask", "b_ask"]].min(axis=1)
        print(f"\n  Total raw arb rows: {len(all_arb)}")
        print(f"  Minimum leg price across all arb rows:")
        for bucket_max in [0.01, 0.02, 0.05, 0.10, 0.20]:
            count = (min_per_leg < bucket_max).sum()
            pct = count / len(all_arb) * 100
            print(f"    min_leg < {bucket_max:.2f}: {count:,} ({pct:.1f}%)")

        has_zero = ((all_arb["a_ask"] == 0) | (all_arb["draw_ask"] == 0) | (all_arb["b_ask"] == 0)).sum()
        print(f"  Rows with at least one leg = 0.000: {has_zero}")

    # ── SUMMARY ──
    print("\n" + "=" * 70)
    print("VALIDATION SUMMARY")
    print("=" * 70)
    print(f"  Total raw arb instances (sum < 1.0):     {total_raw_arb}")
    print(f"  Survived quality filter:                  {total_real_arb}")
    print(f"  Filtered out (bad data):                  {total_raw_arb - total_real_arb}")
    if total_raw_arb > 0:
        survival_pct = total_real_arb / total_raw_arb * 100
        print(f"  Survival rate:                            {survival_pct:.1f}%")
    print()

    if total_real_arb == 0:
        print("  VERDICT: All arbs are PHANTOM — caused by empty/stale orderbooks.")
        print("  The 'arb' disappears when you require real prices on all 3 legs.")
        print("  Type-2 arb is likely not viable with current data.")
    elif total_real_arb < 10:
        print(f"  VERDICT: Only {total_real_arb} real arbs survived. Marginal signal.")
        print("  Need more data or tighter market coverage to confirm viability.")
    else:
        print(f"  VERDICT: {total_real_arb} real arbs survived quality filter!")
        print("  Type-2 arb shows genuine signal. Worth building execution layer.")
        # show the best real arbs across all matches
        best_matches = sorted(match_details, key=lambda x: x["real_arb"], reverse=True)[:5]
        print("\n  Top matches by real arb count:")
        for md in best_matches:
            if md["real_arb"] > 0:
                print(f"    {md['match']}: {md['real_arb']} real arbs (min_sum={md['min_sum']:.4f})")

    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
