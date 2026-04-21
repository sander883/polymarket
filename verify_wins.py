"""Verifikasi win rate actual dari dry-run trades.

Untuk setiap trade di dry_run_trades.log, script ini:
1. Parse question → dapatkan window timing (start/end)
2. Fetch Binance kline di window close → dapatkan BTC close price
3. Bandingkan close vs open → tentukan apakah UP atau DOWN menang
4. Cocokkan dengan sisi yang di-trade (YES=UP, NO=DOWN)
5. Hitung win rate, actual PnL, dan perbandingan vs estimated

Usage:
    python verify_wins.py

Output: tabel per trade dengan ✓/✗, dan summary win rate + actual PnL.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ccxt

ROOT = Path(__file__).resolve().parent
TRADES_LOG = ROOT / "dry_run_trades.log"

WIB = timezone(timedelta(hours=7))
ET_OFFSET = timedelta(hours=-4)  # EDT (April = DST)

# ---------------------------------------------------------------------------
# Parse trade log lines
# ---------------------------------------------------------------------------

TRADE_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\]\s+TRADE\s+(?P<side>YES|NO)\s+@\s+(?P<price>[\d.]+)\s+"
    r"x\s+(?P<shares>[\d.]+)\s+shares\s+=\s+\$(?P<cost>[\d.]+)\s*\|\s*"
    r"edge=(?P<edge>[+-][\d.]+)%\s+exp_profit=\$(?P<profit>[+-]?[\d.]+)\s*\|\s*"
    r"window=(?P<window>\d+)m\s+left=(?P<left>[\d.]+)m\s*\|\s*"
    r"BTC=\$(?P<btc>[\d,]+)\s*\|\s*"
    r"(?P<question>.+?)\s*$"
)


def parse_trades() -> list[dict]:
    if not TRADES_LOG.exists():
        return []
    trades = []
    for line in TRADES_LOG.read_text().splitlines():
        m = TRADE_RE.match(line.strip())
        if not m:
            continue
        d = m.groupdict()
        trades.append({
            "ts": d["ts"],
            "side": d["side"],
            "price": float(d["price"]),
            "shares": float(d["shares"]),
            "cost": float(d["cost"]),
            "edge": float(d["edge"]),
            "exp_profit": float(d["profit"]),
            "window": int(d["window"]),
            "left": float(d["left"]),
            "btc": int(d["btc"].replace(",", "")),
            "question": d["question"],
        })
    return trades


# ---------------------------------------------------------------------------
# Parse window timing from question
# ---------------------------------------------------------------------------

MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

RANGE_RE = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\s*-\s*(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\s*ET",
    re.IGNORECASE,
)
SINGLE_TIME_RE = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\s*ET",
    re.IGNORECASE,
)
DATE_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})",
    re.IGNORECASE,
)
EXPLICIT_DUR_RE = re.compile(r"(\d+)\s*Minute", re.IGNORECASE)


def _et_to_utc(hour: int, minute: int, ampm: str) -> tuple[int, int]:
    """Convert 12h ET to UTC (hour, minute). Assumes EDT = UTC-4."""
    h = hour % 12
    if ampm.upper() == "PM":
        h += 12
    h += 4  # EDT → UTC
    return h, minute


def parse_window(question: str, trade_ts: str) -> tuple[datetime, datetime] | None:
    """Extract (window_start_utc, window_end_utc) from question text."""
    # Parse date
    now = datetime.now(timezone.utc)
    year, month, day = now.year, now.month, now.day

    date_m = DATE_RE.search(question)
    if date_m:
        mn = MONTH_MAP.get(date_m.group(1).lower())
        if mn:
            month = mn
            day = int(date_m.group(2))

    # Try range: "8:00PM-12:00AM ET"
    range_m = RANGE_RE.search(question)
    if range_m:
        sh, sm = _et_to_utc(int(range_m.group(1)), int(range_m.group(2) or "0"), range_m.group(3))
        eh, em = _et_to_utc(int(range_m.group(4)), int(range_m.group(5) or "0"), range_m.group(6))
        try:
            start = datetime(year, month, day, tzinfo=timezone.utc) + timedelta(hours=sh, minutes=sm)
            end = datetime(year, month, day, tzinfo=timezone.utc) + timedelta(hours=eh, minutes=em)
            if end <= start:
                end += timedelta(days=1)
            return start, end
        except ValueError:
            return None

    # Single time: "2AM ET" → hourly, or "4:00 AM ET" with explicit duration
    time_m = SINGLE_TIME_RE.search(question)
    if time_m:
        sh, sm = _et_to_utc(int(time_m.group(1)), int(time_m.group(2) or "0"), time_m.group(3))
        dur_m = EXPLICIT_DUR_RE.search(question)
        window_min = int(dur_m.group(1)) if dur_m else 60
        try:
            start = datetime(year, month, day, tzinfo=timezone.utc) + timedelta(hours=sh, minutes=sm)
            end = start + timedelta(minutes=window_min)
            return start, end
        except ValueError:
            return None

    return None


# ---------------------------------------------------------------------------
# Fetch Binance kline at specific timestamp
# ---------------------------------------------------------------------------

_exchange = None


def _get_exchange() -> ccxt.binance:
    global _exchange
    if _exchange is None:
        _exchange = ccxt.binance({"enableRateLimit": True})
    return _exchange


def get_btc_price_at(ts_ms: int) -> float | None:
    """Get BTC/USDT price at timestamp (open of containing 1-min candle)."""
    try:
        ohlcv = _get_exchange().fetch_ohlcv("BTC/USDT", "1m", since=ts_ms, limit=1)
        if ohlcv and len(ohlcv) > 0:
            return float(ohlcv[0][4])  # close price
    except Exception:
        pass
    return None


def get_btc_open_at(ts_ms: int) -> float | None:
    """Get BTC/USDT open price at timestamp."""
    try:
        ohlcv = _get_exchange().fetch_ohlcv("BTC/USDT", "1m", since=ts_ms, limit=1)
        if ohlcv and len(ohlcv) > 0:
            return float(ohlcv[0][1])  # open price
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Verify each trade
# ---------------------------------------------------------------------------

def verify_trade(trade: dict) -> dict:
    """Verify a single trade against Binance kline data.

    Returns trade dict with added fields:
      - window_start, window_end: parsed UTC timestamps
      - open_price: BTC at window start
      - close_price: BTC at window end
      - actual_winner: "YES" (up) or "NO" (down)
      - won: True/False
      - actual_pnl: dollar profit/loss for this trade
      - status: "VERIFIED" | "PENDING" | "ERROR"
    """
    result = dict(trade)

    window = parse_window(trade["question"], trade["ts"])
    if not window:
        result["status"] = "ERROR"
        result["error"] = "can't parse window"
        return result

    start_utc, end_utc = window
    result["window_start"] = start_utc.strftime("%Y-%m-%d %H:%M UTC")
    result["window_end"] = end_utc.strftime("%Y-%m-%d %H:%M UTC")

    now = datetime.now(timezone.utc)
    if end_utc > now:
        result["status"] = "PENDING"
        result["error"] = "window not closed yet"
        return result

    # Fetch open and close prices
    start_ms = int(start_utc.timestamp() * 1000)
    end_ms = int(end_utc.timestamp() * 1000) - 60_000  # last 1-min candle before close

    open_price = get_btc_open_at(start_ms)
    close_price = get_btc_price_at(end_ms)

    if open_price is None or close_price is None:
        result["status"] = "ERROR"
        result["error"] = f"kline fetch failed (open={open_price}, close={close_price})"
        return result

    result["open_price"] = open_price
    result["close_price"] = close_price
    result["btc_change_pct"] = round((close_price - open_price) / open_price * 100, 4)

    # Determine winner: YES = UP (close > open), NO = DOWN (close <= open)
    if close_price > open_price:
        actual_winner = "YES"
    else:
        actual_winner = "NO"

    result["actual_winner"] = actual_winner
    result["won"] = trade["side"] == actual_winner

    # Calculate actual PnL
    if result["won"]:
        payout = trade["shares"] * 1.0  # $1 per share
        result["actual_pnl"] = round(payout - trade["cost"], 2)
    else:
        result["actual_pnl"] = -trade["cost"]

    result["status"] = "VERIFIED"
    return result


# ---------------------------------------------------------------------------
# Main report
# ---------------------------------------------------------------------------

def run_verification() -> None:
    trades = parse_trades()
    if not trades:
        print("No trades found in dry_run_trades.log")
        return

    print(f"\nVerifying {len(trades)} trades against Binance klines...")
    print(f"{'─' * 90}")

    results: list[dict] = []
    for i, t in enumerate(trades):
        print(f"  [{i+1}/{len(trades)}] {t['question'][:45]}...", end=" ", flush=True)
        r = verify_trade(t)
        results.append(r)

        if r["status"] == "VERIFIED":
            icon = "✓" if r["won"] else "✗"
            print(f"{icon} {r['side']}@{r['price']:.3f} | "
                  f"BTC {r['open_price']:,.0f}→{r['close_price']:,.0f} "
                  f"({r['btc_change_pct']:+.3f}%) | "
                  f"winner={r['actual_winner']} | "
                  f"PnL=${r['actual_pnl']:+.2f}")
        elif r["status"] == "PENDING":
            print(f"⏳ pending (window closes {r.get('window_end', '??')})")
        else:
            print(f"⚠ {r.get('error', 'unknown error')}")

        time.sleep(0.3)  # rate limit Binance

    # Summary
    verified = [r for r in results if r["status"] == "VERIFIED"]
    pending = [r for r in results if r["status"] == "PENDING"]
    errors = [r for r in results if r["status"] == "ERROR"]
    wins = [r for r in verified if r["won"]]
    losses = [r for r in verified if not r["won"]]

    print(f"\n{'=' * 90}")
    print(f"WIN RATE VERIFICATION REPORT")
    print(f"{'=' * 90}")

    print(f"\n  Total trades:    {len(trades)}")
    print(f"  Verified:        {len(verified)}")
    print(f"  Pending:         {len(pending)}")
    print(f"  Errors:          {len(errors)}")

    if verified:
        win_rate = len(wins) / len(verified) * 100
        total_pnl = sum(r["actual_pnl"] for r in verified)
        total_cost = sum(r["cost"] for r in verified)
        avg_win_pnl = sum(r["actual_pnl"] for r in wins) / len(wins) if wins else 0
        avg_loss_pnl = sum(r["actual_pnl"] for r in losses) / len(losses) if losses else 0

        print(f"\n  ┌────────────────────────────────────────┐")
        print(f"  │  WIN RATE:  {win_rate:5.1f}%  ({len(wins)}W / {len(losses)}L)     │")
        print(f"  │  ACTUAL PnL: ${total_pnl:+8.2f}               │")
        print(f"  │  ACTUAL ROI: {total_pnl/total_cost*100:+7.1f}%                │")
        print(f"  └────────────────────────────────────────┘")

        print(f"\n  Avg win:   ${avg_win_pnl:+.2f}")
        print(f"  Avg loss:  ${avg_loss_pnl:+.2f}")
        print(f"  Total cost deployed: ${total_cost:.2f}")

        # Compare estimated vs actual
        est_profit = sum(r["exp_profit"] for r in verified)
        print(f"\n  Estimated profit (dry-run): ${est_profit:.2f}")
        print(f"  Actual profit (verified):  ${total_pnl:.2f}")
        diff = total_pnl - est_profit
        print(f"  Difference:                ${diff:+.2f} "
              f"({'better' if diff > 0 else 'worse'} than estimated)")

        # Per-side breakdown
        yes_trades = [r for r in verified if r["side"] == "YES"]
        no_trades = [r for r in verified if r["side"] == "NO"]
        if yes_trades:
            yes_wins = sum(1 for r in yes_trades if r["won"])
            print(f"\n  BUY YES: {yes_wins}/{len(yes_trades)} wins "
                  f"({yes_wins/len(yes_trades)*100:.0f}%)")
        if no_trades:
            no_wins = sum(1 for r in no_trades if r["won"])
            print(f"  BUY NO:  {no_wins}/{len(no_trades)} wins "
                  f"({no_wins/len(no_trades)*100:.0f}%)")

        # Edge vs outcome correlation
        print(f"\n  Edge distribution:")
        for bucket in ["8-15%", "15-25%", "25-40%"]:
            if bucket == "8-15%":
                subset = [r for r in verified if 8 <= r["edge"] < 15]
            elif bucket == "15-25%":
                subset = [r for r in verified if 15 <= r["edge"] < 25]
            else:
                subset = [r for r in verified if r["edge"] >= 25]
            if subset:
                sw = sum(1 for r in subset if r["won"])
                print(f"    edge {bucket:>6}: {sw}/{len(subset)} wins "
                      f"({sw/len(subset)*100:.0f}%) | "
                      f"PnL=${sum(r['actual_pnl'] for r in subset):+.2f}")

    # Verdict
    print(f"\n  {'─' * 40}")
    print(f"  VERDICT:")
    if not verified:
        print(f"    Belum ada trade terverifikasi. Tunggu window close.")
    elif win_rate >= 80:
        print(f"    ✓ Win rate {win_rate:.0f}% — EXCELLENT. Ready for live trading.")
        print(f"    Estimasi profit live: ${total_pnl/len(verified)*22:.0f}/hari "
              f"(22 trade/hari)")
    elif win_rate >= 60:
        print(f"    ~ Win rate {win_rate:.0f}% — OK tapi perlu evaluasi.")
        print(f"    Cek apakah losses terjadi di edge rendah atau tinggi.")
    else:
        print(f"    ✗ Win rate {win_rate:.0f}% — BELOW target. DO NOT go live.")
        print(f"    Review: mungkin threshold perlu diketatkan.")
    print(f"{'=' * 90}\n")


if __name__ == "__main__":
    run_verification()
