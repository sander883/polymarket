"""Diagnose Type-2 matching — are we pairing questions from different matches?

The key suspicion: `startswith("Will Leeds United FC win")` might match
a question from a DIFFERENT match (e.g., Leeds vs Chelsea) instead of the
intended Leeds vs Wolverhampton. This would make the three-way sum
meaningless because the prices come from independent events.

This script:
  1. For each "draw" market, shows ALL questions containing each team name
  2. Highlights when multiple matches exist per team (the bug)
  3. Shows which specific questions the current logic picks

Usage:
  python diagnose_matching.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import duckdb

DEFAULT_DATA_DIR = Path("data/snapshots")

DRAW_PATTERN = re.compile(
    r"^Will\s+(.+?)\s+vs\.?\s+(.+?)\s+end in a draw",
    re.IGNORECASE,
)


def main() -> int:
    data_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DATA_DIR
    if not data_dir.exists():
        print(f"ERROR: {data_dir} does not exist", file=sys.stderr)
        return 1

    glob_path = str(data_dir / "**" / "*.parquet")
    db = duckdb.connect()
    df = db.sql(
        f"SELECT DISTINCT question FROM read_parquet('{glob_path}')"
    ).fetchdf()
    db.close()

    questions = sorted(df["question"].tolist())
    print(f"Total unique questions: {len(questions)}\n")

    # find all draw markets
    draw_markets = {}
    for q in questions:
        m = DRAW_PATTERN.match(q)
        if m:
            draw_markets[q] = (m.group(1).strip(), m.group(2).strip())

    print(f"Draw markets found: {len(draw_markets)}\n")

    for draw_q, (team_a, team_b) in sorted(draw_markets.items(), key=lambda x: x[0]):
        print("=" * 70)
        print(f"DRAW: {draw_q}")
        print(f"  Parsed team_a: '{team_a}'")
        print(f"  Parsed team_b: '{team_b}'")

        # find ALL questions mentioning each team
        a_mentions = [q for q in questions if team_a in q and q != draw_q]
        b_mentions = [q for q in questions if team_b in q and q != draw_q]

        print(f"\n  ALL questions containing '{team_a}' (excl. draw):")
        for q in a_mentions:
            marker = " <<<< PICKED" if q.startswith(f"Will {team_a} win") else ""
            print(f"    - {q}{marker}")

        print(f"\n  ALL questions containing '{team_b}' (excl. draw):")
        for q in b_mentions:
            marker = " <<<< PICKED" if q.startswith(f"Will {team_b} win") else ""
            print(f"    - {q}{marker}")

        # check: does startswith pick the RIGHT one?
        a_wins = [q for q in questions if q.startswith(f"Will {team_a} win")]
        b_wins = [q for q in questions if q.startswith(f"Will {team_b} win")]

        if len(a_wins) > 1:
            print(f"\n  *** BUG: {len(a_wins)} questions match 'Will {team_a} win...' ***")
            for q in a_wins:
                print(f"    - {q}")
        if len(b_wins) > 1:
            print(f"\n  *** BUG: {len(b_wins)} questions match 'Will {team_b} win...' ***")
            for q in b_wins:
                print(f"    - {q}")

        print()

    # summary: how many teams appear in multiple matches?
    print("\n" + "=" * 70)
    print("MULTI-MATCH TEAMS (teams appearing in >1 draw market)")
    print("=" * 70)
    team_counts: dict[str, list[str]] = {}
    for draw_q, (team_a, team_b) in draw_markets.items():
        team_counts.setdefault(team_a, []).append(draw_q)
        team_counts.setdefault(team_b, []).append(draw_q)

    multi = {t: qs for t, qs in team_counts.items() if len(qs) > 1}
    if multi:
        for team, qs in sorted(multi.items()):
            print(f"\n  {team} appears in {len(qs)} matches:")
            for q in qs:
                print(f"    - {q}")
        print(f"\n  >>> These teams cause CROSS-MATCH CONTAMINATION in the current matching logic!")
    else:
        print("  No teams appear in multiple draw markets.")
        print("  If matching is correct, the arb signal may be genuine.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
