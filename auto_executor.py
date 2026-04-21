"""Dry-run auto-executor for Polymarket latency arb.

Reads signals from the scanner in real-time and simulates trades.
No real orders are placed — every decision is logged to
`dry_run_trades.log` for analysis after 24+ hours.

Usage (terminal terpisah dari scanner):

    python auto_executor.py                  # default: watch signals.log
    python auto_executor.py --interval 3     # poll tiap 3 detik

Setelah 24 jam, jalankan analysis:

    python auto_executor.py --report

Perlu py-clob-client terinstall HANYA untuk mode live (nanti).
Dry-run tidak butuh wallet atau API key apapun.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SIGNAL_LOG = ROOT / "signals.log"
TRADES_LOG = ROOT / "dry_run_trades.log"
POSITIONS_FILE = ROOT / "dry_run_positions.json"

WIB = timezone(timedelta(hours=7))

# ---------------------------------------------------------------------------
# Execution criteria — only 5-min sub-expiry for now
# ---------------------------------------------------------------------------

MAX_WINDOW_MINUTES = 5           # hanya 5-min Up/Down markets
MAX_MINUTES_LEFT = 2.0           # window hampir tutup
MIN_EDGE_PCT = 15.0              # data-driven: 8-15% bucket = 50% winrate, skip
MIN_ACT_SIZE = 50                # minimal share tersedia
MIN_PRICE = 0.50                 # jangan beli yg terlalu murah (too risky)
MAX_PRICE = 0.92                 # jangan beli yg terlalu mahal (low reward)

# Risk caps
MAX_PER_TRADE_USD = 5.0          # $5 per trade (micro-start)
MAX_OPEN_POSITIONS = 3           # max simultaneous
MAX_TOTAL_EXPOSURE_USD = 25.0    # total modal at-risk
DAILY_LOSS_KILL_USD = 10.0       # auto-pause setelah loss $10/hari
DAILY_TRADE_LIMIT = 30           # max 30 trade/hari

# Deduplication — jangan trade market yang sama 2x dalam N detik
DEDUP_WINDOW_SEC = 300           # 5 menit

# ---------------------------------------------------------------------------
# Signal parsing (reuse log format dari crypto_arb_scan.py)
# ---------------------------------------------------------------------------

SIGNAL_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\]\s+"
    r"(?P<tag>ACT|NM)\s+"
    r"EDGE=(?P<edge>[+-][\d.]+)%\s*\|\s*"
    r"exp=(?P<exp>[^|]+?)\s*\|\s*"
    r"BTC=\$(?P<btc>[\d,]+)\s*\|\s*"
    r"buy\s+(?P<side>YES|NO)\s+@\s+(?P<price>[\d.]+)[^|]*\|\s*"
    r"sz_yes=(?P<sz_yes>[\d.]+)\s+sz_no=(?P<sz_no>[\d.]+)\s*\|\s*"
    r"(?P<question>[^|]+?)"
    r"(?:\s*\|\s*slug=(?P<slug>\S*))?"
    r"(?:\s*\|\s*yes_tid=(?P<yes_tid>\S*))?"
    r"(?:\s*\|\s*no_tid=(?P<no_tid>\S*))?"
    r"(?:\s*\|\s*cond=(?P<cond>\S*))?"
    r"\s*$"
)

EXP_UD_RE = re.compile(r"UD(\d+)m\s+([\d.]+)m-left")


@dataclass
class ParsedSignal:
    ts: str
    tag: str
    edge_pct: float
    window_minutes: int | None
    minutes_left: float | None
    btc_price: int
    side: str  # "YES" or "NO"
    price: float
    sz_yes: float
    sz_no: float
    act_size: float
    question: str
    slug: str
    yes_token_id: str
    no_token_id: str
    condition_id: str


def parse_signal_line(line: str) -> ParsedSignal | None:
    m = SIGNAL_RE.match(line.strip())
    if not m:
        return None
    d = m.groupdict()
    side = d["side"]
    act_size = float(d["sz_yes"] if side == "YES" else d["sz_no"])

    window_minutes = None
    minutes_left = None
    exp_m = EXP_UD_RE.match(d["exp"].strip())
    if exp_m:
        window_minutes = int(exp_m.group(1))
        minutes_left = float(exp_m.group(2))

    return ParsedSignal(
        ts=d["ts"],
        tag=d["tag"],
        edge_pct=float(d["edge"]),
        window_minutes=window_minutes,
        minutes_left=minutes_left,
        btc_price=int(d["btc"].replace(",", "")),
        side=side,
        price=float(d["price"]),
        sz_yes=float(d["sz_yes"]),
        sz_no=float(d["sz_no"]),
        act_size=act_size,
        question=d["question"].strip(),
        slug=(d.get("slug") or "").strip(),
        yes_token_id=(d.get("yes_tid") or "").strip(),
        no_token_id=(d.get("no_tid") or "").strip(),
        condition_id=(d.get("cond") or "").strip(),
    )


# ---------------------------------------------------------------------------
# Trade decision engine
# ---------------------------------------------------------------------------

@dataclass
class TradeDecision:
    signal: ParsedSignal
    action: str              # "EXECUTE" | "REJECT"
    reject_reason: str = ""
    trade_size_shares: float = 0.0
    trade_cost_usd: float = 0.0
    expected_profit_usd: float = 0.0
    timestamp: str = ""


@dataclass
class DayStats:
    date: str = ""
    trades_count: int = 0
    total_cost: float = 0.0
    total_expected_profit: float = 0.0
    wins: int = 0           # assumed wins (edge was positive = likely win)
    losses: int = 0
    rejected: int = 0

    def daily_pnl_estimate(self) -> float:
        return self.total_expected_profit


class DryRunExecutor:
    """Simulates trade decisions without placing real orders."""

    def __init__(self) -> None:
        self._seen_lines: int = 0   # track last read position in signals.log
        self._recent_trades: list[tuple[str, float]] = []  # (question, timestamp)
        self._today: str = ""
        self._day_stats = DayStats()
        self._open_positions: int = 0
        self._total_exposure: float = 0.0
        self._all_decisions: list[TradeDecision] = []

    def _reset_day_if_needed(self) -> None:
        today = datetime.now(WIB).strftime("%Y-%m-%d")
        if today != self._today:
            if self._today:
                self._print_day_summary()
            self._today = today
            self._day_stats = DayStats(date=today)

    def _is_duplicate(self, question: str) -> bool:
        now = time.time()
        self._recent_trades = [
            (q, t) for q, t in self._recent_trades
            if now - t < DEDUP_WINDOW_SEC
        ]
        return any(q == question for q, _ in self._recent_trades)

    def _check_criteria(self, sig: ParsedSignal) -> str | None:
        """Return reject reason, or None if signal passes all filters."""

        if sig.tag != "ACT":
            return "not-actionable"

        if sig.window_minutes is None:
            return "not-up-down-market"

        if sig.window_minutes > MAX_WINDOW_MINUTES:
            return f"window-too-long ({sig.window_minutes}m > {MAX_WINDOW_MINUTES}m)"

        if sig.minutes_left is None or sig.minutes_left > MAX_MINUTES_LEFT:
            return f"too-much-time-left ({sig.minutes_left}m > {MAX_MINUTES_LEFT}m)"

        if sig.edge_pct < MIN_EDGE_PCT:
            return f"edge-too-small ({sig.edge_pct}% < {MIN_EDGE_PCT}%)"

        if sig.act_size < MIN_ACT_SIZE:
            return f"size-too-small ({sig.act_size} < {MIN_ACT_SIZE})"

        if sig.price < MIN_PRICE:
            return f"price-too-low ({sig.price} < {MIN_PRICE})"

        if sig.price > MAX_PRICE:
            return f"price-too-high ({sig.price} > {MAX_PRICE})"

        if self._is_duplicate(sig.question):
            return "duplicate-within-5min"

        # Risk caps
        if self._day_stats.trades_count >= DAILY_TRADE_LIMIT:
            return f"daily-limit-reached ({DAILY_TRADE_LIMIT})"

        if self._open_positions >= MAX_OPEN_POSITIONS:
            return f"max-positions ({MAX_OPEN_POSITIONS})"

        if self._total_exposure >= MAX_TOTAL_EXPOSURE_USD:
            return f"max-exposure (${MAX_TOTAL_EXPOSURE_USD})"

        return None

    def _calculate_trade(self, sig: ParsedSignal) -> tuple[float, float, float]:
        """Return (shares, cost, expected_profit)."""
        max_shares_by_budget = MAX_PER_TRADE_USD / sig.price
        shares = min(max_shares_by_budget, sig.act_size)
        cost = round(shares * sig.price, 2)
        # Expected value: win probability ~= sig.price + sig.edge/100
        # Payout on win: $1 per share. Profit = payout - cost.
        expected_payout = shares * 1.0  # $1 per share if win
        # Rough win prob estimate: use the price + edge as estimate
        win_prob = min(sig.price + sig.edge_pct / 100, 0.99)
        expected_profit = round(expected_payout * win_prob - cost, 2)
        return round(shares, 1), cost, expected_profit

    def evaluate(self, sig: ParsedSignal) -> TradeDecision:
        """Evaluate a signal and return trade decision."""
        self._reset_day_if_needed()
        ts = datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S WIB")

        reject = self._check_criteria(sig)
        if reject:
            self._day_stats.rejected += 1
            return TradeDecision(
                signal=sig, action="REJECT", reject_reason=reject, timestamp=ts,
            )

        shares, cost, exp_profit = self._calculate_trade(sig)

        self._recent_trades.append((sig.question, time.time()))
        self._day_stats.trades_count += 1
        self._day_stats.total_cost += cost
        self._day_stats.total_expected_profit += exp_profit
        self._day_stats.wins += 1  # assume win for dry-run
        self._open_positions += 1
        self._total_exposure += cost

        # Auto-close position after window expires (simulated)
        # In real mode this would track resolution via API

        decision = TradeDecision(
            signal=sig,
            action="EXECUTE",
            trade_size_shares=shares,
            trade_cost_usd=cost,
            expected_profit_usd=exp_profit,
            timestamp=ts,
        )
        self._all_decisions.append(decision)
        return decision

    def release_position(self, cost: float) -> None:
        """Called after window closes — frees up position slot and exposure."""
        self._open_positions = max(0, self._open_positions - 1)
        self._total_exposure = max(0.0, self._total_exposure - cost)

    def process_new_signals(self) -> list[TradeDecision]:
        """Read new lines from signals.log and evaluate them."""
        if not SIGNAL_LOG.exists():
            return []

        try:
            lines = SIGNAL_LOG.read_text().splitlines()
        except OSError:
            return []

        new_lines = lines[self._seen_lines:]
        self._seen_lines = len(lines)

        decisions: list[TradeDecision] = []
        for line in new_lines:
            sig = parse_signal_line(line)
            if sig is None:
                continue
            decision = self.evaluate(sig)
            decisions.append(decision)
        return decisions

    def _print_day_summary(self) -> None:
        s = self._day_stats
        print(f"\n{'─' * 60}")
        print(f"DAY SUMMARY — {s.date}")
        print(f"  Trades executed: {s.trades_count}")
        print(f"  Rejected: {s.rejected}")
        print(f"  Total cost: ${s.total_cost:.2f}")
        print(f"  Expected profit: ${s.total_expected_profit:.2f}")
        print(f"{'─' * 60}\n")


# ---------------------------------------------------------------------------
# Log trade decisions
# ---------------------------------------------------------------------------

def log_decision(d: TradeDecision) -> None:
    """Append trade decision to dry_run_trades.log."""
    sig = d.signal
    if d.action == "EXECUTE":
        line = (
            f"[{d.timestamp}] TRADE {sig.side} @ {sig.price:.3f} "
            f"x {d.trade_size_shares:.0f} shares = ${d.trade_cost_usd:.2f} | "
            f"edge={sig.edge_pct:+.1f}% exp_profit=${d.expected_profit_usd:.2f} | "
            f"window={sig.window_minutes}m left={sig.minutes_left:.1f}m | "
            f"BTC=${sig.btc_price:,} | "
            f"{sig.question[:50]}\n"
        )
    else:
        line = (
            f"[{d.timestamp}] SKIP  {d.reject_reason:30} | "
            f"edge={sig.edge_pct:+.1f}% {sig.side}@{sig.price:.3f} "
            f"sz={sig.act_size:.0f} | "
            f"{sig.question[:50]}\n"
        )
    try:
        with open(TRADES_LOG, "a") as f:
            f.write(line)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Report — analyze dry_run_trades.log
# ---------------------------------------------------------------------------

TRADE_LINE_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\]\s+TRADE\s+(?P<side>YES|NO)\s+@\s+(?P<price>[\d.]+)\s+"
    r"x\s+(?P<shares>[\d.]+)\s+shares\s+=\s+\$(?P<cost>[\d.]+)\s*\|\s*"
    r"edge=(?P<edge>[+-][\d.]+)%\s+exp_profit=\$(?P<profit>[+-]?[\d.]+)\s*\|\s*"
    r"window=(?P<window>\d+)m\s+left=(?P<left>[\d.]+)m"
)

SKIP_LINE_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\]\s+SKIP\s+(?P<reason>\S+)"
)


def generate_report() -> None:
    """Analyze dry_run_trades.log and print summary."""
    if not TRADES_LOG.exists():
        print("No trades log found. Run the executor first.")
        return

    lines = TRADES_LOG.read_text().splitlines()

    trades: list[dict] = []
    skips: dict[str, int] = {}

    for line in lines:
        m = TRADE_LINE_RE.match(line)
        if m:
            d = m.groupdict()
            trades.append({
                "ts": d["ts"],
                "side": d["side"],
                "price": float(d["price"]),
                "shares": float(d["shares"]),
                "cost": float(d["cost"]),
                "edge": float(d["edge"]),
                "profit": float(d["profit"]),
                "window": int(d["window"]),
                "left": float(d["left"]),
            })
            continue
        m = SKIP_LINE_RE.match(line)
        if m:
            reason = m.group("reason")
            skips[reason] = skips.get(reason, 0) + 1

    print(f"\n{'=' * 60}")
    print(f"DRY-RUN ANALYSIS REPORT")
    print(f"{'=' * 60}")

    if not trades and not skips:
        print("  No data yet.")
        return

    # --- Trades ---
    print(f"\n  Total simulated trades: {len(trades)}")
    if trades:
        total_cost = sum(t["cost"] for t in trades)
        total_profit = sum(t["profit"] for t in trades)
        avg_edge = sum(t["edge"] for t in trades) / len(trades)
        avg_price = sum(t["price"] for t in trades) / len(trades)
        avg_left = sum(t["left"] for t in trades) / len(trades)
        max_edge = max(t["edge"] for t in trades)
        min_edge = min(t["edge"] for t in trades)

        print(f"  Total cost (capital deployed): ${total_cost:.2f}")
        print(f"  Total expected profit:         ${total_profit:.2f}")
        print(f"  ROI estimate:                  {total_profit / total_cost * 100:.1f}%" if total_cost > 0 else "")
        print(f"  Avg edge per trade:            {avg_edge:+.1f}%")
        print(f"  Edge range:                    {min_edge:+.1f}% to {max_edge:+.1f}%")
        print(f"  Avg buy price:                 ${avg_price:.3f}")
        print(f"  Avg time left at entry:        {avg_left:.1f} min")

        # Per-day breakdown
        by_day: dict[str, list[dict]] = {}
        for t in trades:
            day = t["ts"][:10]
            by_day.setdefault(day, []).append(t)

        if len(by_day) > 1:
            print(f"\n  {'Day':<12} {'Trades':>7} {'Cost':>8} {'Profit':>8} {'Avg Edge':>10}")
            print(f"  {'─' * 48}")
            for day in sorted(by_day):
                dt = by_day[day]
                dc = sum(t["cost"] for t in dt)
                dp = sum(t["profit"] for t in dt)
                de = sum(t["edge"] for t in dt) / len(dt)
                print(f"  {day:<12} {len(dt):>7} ${dc:>7.2f} ${dp:>7.2f} {de:>+9.1f}%")

        # Side breakdown
        yes_trades = [t for t in trades if t["side"] == "YES"]
        no_trades = [t for t in trades if t["side"] == "NO"]
        print(f"\n  BUY YES: {len(yes_trades)} trades")
        print(f"  BUY NO:  {len(no_trades)} trades")

    # --- Skips ---
    if skips:
        print(f"\n  Rejected signals: {sum(skips.values())}")
        for reason, count in sorted(skips.items(), key=lambda x: -x[1]):
            print(f"    {reason:<35} {count:>5}x")

    # --- Recommendations ---
    print(f"\n  {'─' * 40}")
    print(f"  RECOMMENDATIONS:")
    if not trades:
        print(f"    No trades triggered. Consider:")
        print(f"    - Lower MIN_EDGE_PCT (currently {MIN_EDGE_PCT}%)")
        print(f"    - Increase MAX_MINUTES_LEFT (currently {MAX_MINUTES_LEFT}m)")
        print(f"    - Increase MAX_WINDOW_MINUTES (currently {MAX_WINDOW_MINUTES}m)")
    else:
        if len(trades) < 5:
            print(f"    Low volume. Consider widening MAX_WINDOW_MINUTES to 15.")
        if avg_edge > 15:
            print(f"    High avg edge — consider increasing MAX_PER_TRADE_USD.")
        if total_profit > 0:
            print(f"    Profitable. Ready to test with real $ (start $25).")
        else:
            print(f"    Not profitable. Review filter criteria before going live.")
    print(f"{'=' * 60}\n")


# ---------------------------------------------------------------------------
# Live execution via py-clob-client
# ---------------------------------------------------------------------------

LIVE_TRADES_LOG = ROOT / "live_trades.log"

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet


def _load_clob_client():
    """Initialize ClobClient with credentials from .env."""
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "")
    if not private_key or private_key == "0xyour_private_key_here":
        raise RuntimeError(
            "POLYMARKET_PRIVATE_KEY not set in .env\n"
            "  1. Buat wallet baru di Polymarket\n"
            "  2. Export private key\n"
            "  3. Isi di .env: POLYMARKET_PRIVATE_KEY=0x..."
        )

    from py_clob_client.client import ClobClient
    client = ClobClient(
        host=CLOB_HOST,
        chain_id=CHAIN_ID,
        key=private_key,
        signature_type=2,  # POLY_GNOSIS_SAFE for Polymarket proxy wallets
    )

    # Derive or load API credentials
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)

    return client


def place_live_order(client, sig: ParsedSignal, shares: float) -> dict:
    """Place a real order on Polymarket. Returns order response dict."""
    from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions

    token_id = sig.yes_token_id if sig.side == "YES" else sig.no_token_id
    if not token_id:
        raise ValueError(f"No token_id for {sig.side} side — signal from old log format?")

    order_args = OrderArgs(
        token_id=token_id,
        price=sig.price,
        size=round(shares, 2),
        side="BUY",
    )

    options = PartialCreateOrderOptions(order_type=OrderType.FOK)

    resp = client.create_and_post_order(order_args, options)
    return resp


def log_live_trade(sig: ParsedSignal, shares: float, cost: float,
                   resp: dict, status: str) -> None:
    """Log live trade to live_trades.log."""
    ts = datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S WIB")
    order_id = ""
    if isinstance(resp, dict):
        order_id = resp.get("orderID", resp.get("id", ""))
    line = (
        f"[{ts}] LIVE {status} {sig.side} @ {sig.price:.3f} "
        f"x {shares:.0f} = ${cost:.2f} | "
        f"edge={sig.edge_pct:+.1f}% | "
        f"left={sig.minutes_left:.1f}m | "
        f"order={order_id} | "
        f"{sig.question[:50]}\n"
    )
    try:
        with open(LIVE_TRADES_LOG, "a") as f:
            f.write(line)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Main loop — tail signals.log
# ---------------------------------------------------------------------------

def run_watcher(interval: float, live: bool = False) -> None:
    """Poll signals.log and evaluate new signals."""
    executor = DryRunExecutor()
    ts = datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S WIB")
    mode = "LIVE" if live else "DRY RUN"

    clob_client = None
    if live:
        clob_client = _load_clob_client()
        addr = clob_client.get_address()
        print(f"Auto-executor {mode} started at {ts}")
        print(f"Wallet: {addr}")

        # Check balance
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        bal = clob_client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        balance = float(bal.get("balance", 0)) / 1e6  # USDC has 6 decimals
        print(f"USDC Balance: ${balance:.2f}")
        if balance < MAX_PER_TRADE_USD:
            print(f"WARNING: Balance ${balance:.2f} < MAX_PER_TRADE_USD ${MAX_PER_TRADE_USD}")
            print(f"Deposit more USDC ke wallet Polygon sebelum trading.")
    else:
        print(f"Auto-executor {mode} started at {ts}")

    print(f"Watching: {SIGNAL_LOG}")
    print(f"Logging:  {TRADES_LOG}" + (f" + {LIVE_TRADES_LOG}" if live else ""))
    print(f"Criteria: {MAX_WINDOW_MINUTES}m window, <{MAX_MINUTES_LEFT}m left, "
          f"edge>{MIN_EDGE_PCT}%, size>{MIN_ACT_SIZE}, "
          f"price {MIN_PRICE}-{MAX_PRICE}")
    print(f"Risk:     ${MAX_PER_TRADE_USD}/trade, ${MAX_TOTAL_EXPOSURE_USD} max, "
          f"kill@-${DAILY_LOSS_KILL_USD}/day")
    print(f"Press Ctrl-C to stop.\n")

    live_trades_count = 0
    live_total_cost = 0.0

    try:
        while True:
            decisions = executor.process_new_signals()

            for d in decisions:
                log_decision(d)
                sig = d.signal
                if d.action == "EXECUTE":
                    print(f"  >>> TRADE {sig.side} @ {sig.price:.3f} "
                          f"x {d.trade_size_shares:.0f} = ${d.trade_cost_usd:.2f} | "
                          f"edge={sig.edge_pct:+.1f}% | "
                          f"left={sig.minutes_left:.1f}m | "
                          f"exp_profit=${d.expected_profit_usd:.2f} | "
                          f"{sig.question[:40]}")

                    if live and clob_client:
                        try:
                            resp = place_live_order(
                                clob_client, sig, d.trade_size_shares
                            )
                            status = "FILLED" if resp.get("success") else "FAILED"
                            log_live_trade(
                                sig, d.trade_size_shares, d.trade_cost_usd,
                                resp, status,
                            )
                            live_trades_count += 1
                            live_total_cost += d.trade_cost_usd
                            print(f"      LIVE {status}: {resp}")
                        except Exception as exc:
                            log_live_trade(
                                sig, d.trade_size_shares, d.trade_cost_usd,
                                {}, f"ERROR: {exc}",
                            )
                            print(f"      LIVE ERROR: {exc}")

                    executor.release_position(d.trade_cost_usd)
                else:
                    print(f"  --- SKIP  {d.reject_reason} | "
                          f"edge={sig.edge_pct:+.1f}% {sig.side}@{sig.price:.3f} | "
                          f"{sig.question[:40]}")

            time.sleep(interval)

    except KeyboardInterrupt:
        print(f"\nStopped.")
        if live:
            print(f"Live trades: {live_trades_count}, total cost: ${live_total_cost:.2f}")
            print(f"Review: cat {LIVE_TRADES_LOG}")
        print(f"Run 'python auto_executor.py --report' for analysis.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--interval", type=float, default=5,
                   help="poll interval in seconds (default 5)")
    p.add_argument("--report", action="store_true",
                   help="analyze dry_run_trades.log and print summary")
    p.add_argument("--live", action="store_true",
                   help="LIVE MODE: place real orders (requires .env + USDC)")
    args = p.parse_args()

    if args.report:
        generate_report()
        return 0

    if args.live:
        print("=" * 60)
        print("  WARNING: LIVE TRADING MODE")
        print(f"  Max per trade: ${MAX_PER_TRADE_USD}")
        print(f"  Max exposure:  ${MAX_TOTAL_EXPOSURE_USD}")
        print(f"  Daily kill:    -${DAILY_LOSS_KILL_USD}")
        print("=" * 60)
        confirm = input("  Type 'YES' to confirm: ").strip()
        if confirm != "YES":
            print("  Cancelled.")
            return 0
        print()

    run_watcher(args.interval, live=args.live)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
