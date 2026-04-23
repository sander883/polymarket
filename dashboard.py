"""Dashboard web — mirror terminal scanner + stats panel real-time.

Cara pakai:

    # terminal A: jalankan scanner (--live untuk auto-execute)
    python crypto_arb_scan.py --loop 5 --live

    # terminal B: jalankan dashboard
    python dashboard.py

    # browser: http://<ip-vps>:8080

Scanner auto-tee stdout ke scan_output.log. Dashboard tampilin:
  - stats panel (trades, fills, errors, PnL estimate, exposure)
  - terminal output persis dengan highlight warna
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parent
OUTPUT_FILE = ROOT / "scan_output.log"
LIVE_TRADES_LOG = ROOT / "live_trades.log"
MAX_LINES = 800


def tail(path: Path, n: int) -> str:
    if not path.exists():
        return (f"(file {path.name} belum ada — jalankan: "
                f"python crypto_arb_scan.py --loop 5)")
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError as exc:
        return f"(gagal baca {path.name}: {exc})"
    return "\n".join(lines[-n:])


# ---------------------------------------------------------------------------
# Stats parser — extract key metrics from scan_output.log + live_trades.log
# ---------------------------------------------------------------------------

TRADE_RE = re.compile(
    r">>>\s+(?:TRADE\s+)?(YES|NO)\s+@\s+([\d.]+)\s+"
    r"x\s+([\d.]+)\s+=\s+\$([\d.]+)"
)
LIVE_STATUS_RE = re.compile(r"\[LIVE\]\s+(FILLED|FAILED|ERROR)")
LIVE_SKIP_RE = re.compile(r"\[LIVE\]\s+SKIP\s+(\S+)")
BTC_RE = re.compile(r"BTC=\$([\d,.]+)")
CYCLE_RE = re.compile(r"Cycle\s+(\d+)")
EDGE_RE = re.compile(r"edge=([+-][\d.]+)%")
SLIPPAGE_RE = re.compile(r"slippage\s+([+-][\d.]+)%")

LIVE_TRADE_LINE_RE = re.compile(
    r"\[(?P<ts>[^\]]+)\]\s+LIVE\s+(?P<status>\S+)\s+(?P<side>YES|NO)\s+"
    r"@\s+(?P<price>[\d.]+)\s+x\s+(?P<shares>[\d.]+)\s+=\s+\$(?P<cost>[\d.]+)\s*\|\s*"
    r"edge=(?P<edge>[+-][\d.]+)%"
)


def compute_stats() -> dict:
    """Parse scan_output.log and live_trades.log for stats."""
    stats = {
        "btc_price": None,
        "cycle": 0,
        "trades_attempted": 0,
        "filled": 0,
        "failed": 0,
        "errors": 0,
        "skip_reasons": {},
        "deployed_usd": 0.0,
        "expected_pnl": 0.0,
        "recent_trades": [],
    }

    # Parse scan_output.log for live runtime info
    if OUTPUT_FILE.exists():
        try:
            text = OUTPUT_FILE.read_text(errors="replace")
            lines = text.splitlines()
        except OSError:
            lines = []

        for line in lines:
            m = BTC_RE.search(line)
            if m:
                try:
                    stats["btc_price"] = float(m.group(1).replace(",", ""))
                except ValueError:
                    pass
            m = CYCLE_RE.search(line)
            if m:
                try:
                    stats["cycle"] = int(m.group(1))
                except ValueError:
                    pass

    # Parse live_trades.log for authoritative trade counts
    if LIVE_TRADES_LOG.exists():
        try:
            trade_lines = LIVE_TRADES_LOG.read_text(errors="replace").splitlines()
        except OSError:
            trade_lines = []

        for line in trade_lines:
            m = LIVE_TRADE_LINE_RE.search(line)
            if not m:
                continue
            d = m.groupdict()
            status = d["status"]
            cost = float(d["cost"])
            edge = float(d["edge"])
            price = float(d["price"])
            shares = float(d["shares"])

            stats["trades_attempted"] += 1

            if status == "FILLED":
                stats["filled"] += 1
                stats["deployed_usd"] += cost
                # Expected PnL: shares paid out $1 if win, cost to enter
                # win_prob ~ price + edge/100
                win_prob = min(price + edge / 100, 0.99)
                expected_payout = shares * 1.0
                stats["expected_pnl"] += (expected_payout * win_prob) - cost
            elif status == "FAILED":
                stats["failed"] += 1
            else:
                stats["errors"] += 1

            stats["recent_trades"].append({
                "ts": d["ts"],
                "status": status,
                "side": d["side"],
                "price": price,
                "shares": shares,
                "cost": cost,
                "edge": edge,
            })

        # keep last 10
        stats["recent_trades"] = stats["recent_trades"][-10:]

    # Count SKIP reasons from scan_output
    if OUTPUT_FILE.exists():
        try:
            text = OUTPUT_FILE.read_text(errors="replace")
        except OSError:
            text = ""
        for m in LIVE_SKIP_RE.finditer(text):
            reason = m.group(1).split("-")[0]
            stats["skip_reasons"][reason] = stats["skip_reasons"].get(reason, 0) + 1

    stats["deployed_usd"] = round(stats["deployed_usd"], 2)
    stats["expected_pnl"] = round(stats["expected_pnl"], 2)
    return stats


async def api_tail(request: web.Request) -> web.Response:
    n = int(request.query.get("n", str(MAX_LINES)))
    return web.Response(text=tail(OUTPUT_FILE, n), content_type="text/plain")


async def api_stats(request: web.Request) -> web.Response:
    return web.Response(
        text=json.dumps(compute_stats()),
        content_type="application/json",
    )


INDEX_HTML = """<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Polymarket Latency Arb — Live</title>
<style>
  :root {
    color-scheme: dark;
    --bg: #0a0d14;
    --bg-panel: #10141d;
    --bg-card: #141924;
    --border: #1e2533;
    --border-strong: #2a3446;
    --text: #e4e7ee;
    --text-dim: #8a93a6;
    --text-muted: #5a6478;
    --green: #4ade80;
    --green-dim: #86efac;
    --red: #f87171;
    --red-dim: #fca5a5;
    --yellow: #fde68a;
    --blue: #7dd3fc;
    --purple: #c4b5fd;
    --cyan: #67e8f9;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0; height: 100%;
    background: var(--bg); color: var(--text);
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 13px;
  }
  body { display: flex; flex-direction: column; }

  /* ─── Header ─────────────────────────────────────────────────── */
  .topbar {
    display: flex; align-items: center; gap: 16px;
    padding: 10px 18px;
    background: linear-gradient(180deg, #141924 0%, #10141d 100%);
    border-bottom: 1px solid var(--border-strong);
    font-size: 12px;
  }
  .topbar .brand {
    font-weight: 700; font-size: 14px;
    letter-spacing: 0.5px; color: var(--text);
  }
  .topbar .brand .accent { color: var(--green); }
  .topbar .status {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 3px 10px;
    background: rgba(74,222,128,0.12);
    border: 1px solid rgba(74,222,128,0.3);
    border-radius: 12px;
    color: var(--green);
    font-weight: 600;
  }
  .topbar .status.stale {
    background: rgba(248,113,113,0.12);
    border-color: rgba(248,113,113,0.3);
    color: var(--red);
  }
  .topbar .dot {
    width: 7px; height: 7px; border-radius: 50%;
    background: currentColor;
    animation: pulse 1.6s infinite;
  }
  .topbar .status.stale .dot { animation: none; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .35; } }
  .topbar .sep { color: var(--border-strong); }
  .topbar .label { color: var(--text-dim); }
  .topbar .value { color: var(--text); font-weight: 600; }
  .topbar .spacer { flex: 1; }
  .topbar .meta { display: flex; align-items: center; gap: 10px; }
  .topbar label.toggle {
    color: var(--text-dim); font-size: 11px;
    display: inline-flex; align-items: center; gap: 5px; cursor: pointer;
  }
  .topbar input[type=checkbox] { accent-color: var(--green); }

  /* ─── Stats panel ─────────────────────────────────────────────── */
  .stats {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 10px;
    padding: 12px 18px;
    background: var(--bg-panel);
    border-bottom: 1px solid var(--border);
  }
  .card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 12px;
    display: flex; flex-direction: column; gap: 4px;
    transition: border-color 0.2s;
  }
  .card:hover { border-color: var(--border-strong); }
  .card .label {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: var(--text-dim);
    font-weight: 600;
  }
  .card .value {
    font-size: 18px;
    font-weight: 700;
    line-height: 1.2;
  }
  .card .sub {
    font-size: 10.5px;
    color: var(--text-muted);
  }
  .card.green .value { color: var(--green); }
  .card.red .value { color: var(--red); }
  .card.yellow .value { color: var(--yellow); }
  .card.blue .value { color: var(--blue); }
  .card.cyan .value { color: var(--cyan); }

  /* ─── Recent trades list ─────────────────────────────────────── */
  .recent-trades {
    padding: 10px 18px;
    background: var(--bg-panel);
    border-bottom: 1px solid var(--border);
    max-height: 120px;
    overflow-y: auto;
  }
  .recent-trades .title {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: var(--text-dim);
    font-weight: 600;
    margin-bottom: 6px;
  }
  .trade-row {
    display: grid;
    grid-template-columns: 120px 70px 50px 80px 80px 80px 1fr;
    gap: 10px;
    padding: 3px 6px;
    font-size: 11.5px;
    border-radius: 4px;
  }
  .trade-row.filled { background: rgba(74,222,128,0.06); }
  .trade-row.failed, .trade-row.error { background: rgba(248,113,113,0.06); }
  .trade-row .status-filled { color: var(--green); font-weight: 600; }
  .trade-row .status-failed,
  .trade-row .status-error { color: var(--red); font-weight: 600; }
  .trade-row .side-YES { color: var(--green-dim); font-weight: 600; }
  .trade-row .side-NO { color: var(--red-dim); font-weight: 600; }
  .empty {
    font-size: 11.5px; color: var(--text-muted); font-style: italic;
  }

  /* ─── Terminal output ─────────────────────────────────────────── */
  .terminal-head {
    padding: 8px 18px;
    background: var(--bg-panel);
    border-bottom: 1px solid var(--border);
    display: flex; align-items: center; gap: 12px;
    font-size: 10.5px;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.8px;
    font-weight: 600;
  }
  .terminal-head .dot-tiny {
    width: 6px; height: 6px; border-radius: 50%;
    background: var(--green); animation: pulse 1.6s infinite;
  }
  pre.term {
    margin: 0; padding: 14px 18px;
    white-space: pre-wrap; word-break: break-word;
    font-size: 12px; line-height: 1.55;
    background: var(--bg);
    flex: 1; overflow-y: auto;
  }
  pre.term::-webkit-scrollbar { width: 10px; }
  pre.term::-webkit-scrollbar-track { background: var(--bg-panel); }
  pre.term::-webkit-scrollbar-thumb {
    background: var(--border-strong); border-radius: 5px;
  }

  /* syntax highlights in terminal */
  pre.term .hl-edge       { color: var(--green); font-weight: 700; }
  pre.term .hl-edge-neg   { color: var(--red); font-weight: 700; }
  pre.term .hl-buy-yes    { color: var(--green-dim); font-weight: 600; }
  pre.term .hl-buy-no     { color: var(--red-dim); font-weight: 600; }
  pre.term .hl-trade      { color: var(--green); font-weight: 700;
                            background: rgba(74,222,128,0.08);
                            padding: 0 4px; border-radius: 3px; }
  pre.term .hl-filled     { color: var(--green); font-weight: 700; }
  pre.term .hl-failed     { color: var(--red); font-weight: 700; }
  pre.term .hl-error      { color: var(--red); font-weight: 700;
                            background: rgba(248,113,113,0.08);
                            padding: 0 4px; border-radius: 3px; }
  pre.term .hl-skip       { color: var(--text-muted); }
  pre.term .hl-slippage   { color: var(--yellow); font-weight: 600; }
  pre.term .hl-live-tag   { color: var(--purple); font-weight: 700;
                            background: rgba(196,181,253,0.08);
                            padding: 0 3px; border-radius: 3px; }
  pre.term .hl-sep        { color: var(--border-strong); }
  pre.term .hl-wib        { color: var(--blue); }
  pre.term .hl-btc        { color: var(--yellow); font-weight: 600; }
  pre.term .hl-cycle      { color: var(--cyan); font-weight: 600; }
  pre.term .hl-header-line { color: var(--border-strong); }
</style>
</head>
<body>

  <!-- Top bar -->
  <div class="topbar">
    <span class="brand">POLY<span class="accent">·</span>ARB</span>
    <span class="status" id="status"><span class="dot"></span><span id="statusText">loading…</span></span>
    <span class="sep">│</span>
    <span class="label">BTC</span><span class="value" id="btcPrice">—</span>
    <span class="sep">│</span>
    <span class="label">cycle</span><span class="value" id="cycle">—</span>
    <span class="spacer"></span>
    <div class="meta">
      <span class="label">last update</span><span class="value" id="now">—</span>
      <label class="toggle"><input type="checkbox" id="autoscroll" checked> auto-scroll</label>
    </div>
  </div>

  <!-- Stats cards -->
  <div class="stats">
    <div class="card blue">
      <span class="label">Trades Attempted</span>
      <span class="value" id="stTrades">0</span>
      <span class="sub">via CLOB API</span>
    </div>
    <div class="card green">
      <span class="label">Filled</span>
      <span class="value" id="stFilled">0</span>
      <span class="sub" id="stFillRate">0% fill rate</span>
    </div>
    <div class="card red">
      <span class="label">Failed + Errors</span>
      <span class="value" id="stFailed">0</span>
      <span class="sub" id="stFailDetail">—</span>
    </div>
    <div class="card yellow">
      <span class="label">Deployed</span>
      <span class="value" id="stDeployed">$0.00</span>
      <span class="sub">total capital in trades</span>
    </div>
    <div class="card cyan">
      <span class="label">Est. PnL</span>
      <span class="value" id="stPnl">$0.00</span>
      <span class="sub">(expected, needs verify)</span>
    </div>
    <div class="card">
      <span class="label">Skipped</span>
      <span class="value" id="stSkipped">0</span>
      <span class="sub" id="stSkipTop">—</span>
    </div>
  </div>

  <!-- Recent trades -->
  <div class="recent-trades">
    <div class="title">Recent Live Trades</div>
    <div id="recentList" class="empty">No live trades yet.</div>
  </div>

  <!-- Terminal mirror -->
  <div class="terminal-head">
    <span class="dot-tiny"></span>
    <span>scanner output (scan_output.log)</span>
  </div>
  <pre class="term" id="out">loading…</pre>

<script>
const out = document.getElementById('out');
const statusEl = document.getElementById('status');
const statusText = document.getElementById('statusText');
const nowEl = document.getElementById('now');
const autoscroll = document.getElementById('autoscroll');

let lastLen = 0;
let lastChange = Date.now();

function escapeHtml(t) {
  return t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function highlight(text) {
  text = escapeHtml(text);
  // Separator lines (=== and ───)
  text = text.replace(/(={5,})/g, '<span class="hl-header-line">$1</span>');
  text = text.replace(/(─{5,})/g, '<span class="hl-header-line">$1</span>');
  // [LIVE] tag
  text = text.replace(/\\[LIVE\\]/g, '<span class="hl-live-tag">[LIVE]</span>');
  // TRADE / >>> TRADE  — full line match
  text = text.replace(/(&gt;&gt;&gt;\\s+(?:TRADE\\s+)?(?:YES|NO)[^\\n]*)/g,
                      '<span class="hl-trade">$1</span>');
  // FILLED / FAILED / ERROR status
  text = text.replace(/\\bFILLED\\b/g, '<span class="hl-filled">FILLED</span>');
  text = text.replace(/\\bFAILED\\b/g, '<span class="hl-failed">FAILED</span>');
  text = text.replace(/\\bERROR\\b/g, '<span class="hl-error">ERROR</span>');
  // slippage warning
  text = text.replace(/(slippage\\s+[+-]?\\d+(?:\\.\\d+)?%[^|\\n]*)/gi,
                      '<span class="hl-slippage">$1</span>');
  // Edge percentages (positive = green, negative = red)
  text = text.replace(/(EDGE=|edge=)([+-]?\\d+(?:\\.\\d+)?%)/g, (m, p1, p2) => {
    const cls = p2.startsWith('-') ? 'hl-edge-neg' : 'hl-edge';
    return `<span class="${cls}">${p1}${p2}</span>`;
  });
  // buy YES / buy NO
  text = text.replace(/\\b(buy YES)\\b/gi, '<span class="hl-buy-yes">$1</span>');
  text = text.replace(/\\b(buy NO)\\b/gi, '<span class="hl-buy-no">$1</span>');
  // SKIP tag
  text = text.replace(/(^|\\s)SKIP(?=\\s|$)/g, '$1<span class="hl-skip">SKIP</span>');
  // Cycle counter
  text = text.replace(/(Cycle\\s+\\d+)/g, '<span class="hl-cycle">$1</span>');
  // Dollar amounts (but not just "$")
  text = text.replace(/(\\$\\d{1,3}(?:,\\d{3})*(?:\\.\\d+)?)/g,
                      '<span class="hl-btc">$1</span>');
  // WIB timestamps
  text = text.replace(/(\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2} WIB)/g,
                      '<span class="hl-wib">$1</span>');
  // HH:MM:SS at start of cycle header
  text = text.replace(/(\\d{2}:\\d{2}:\\d{2} WIB)/g,
                      '<span class="hl-wib">$1</span>');
  text = text.replace(/(\\d{2}:\\d{2}:\\d{2} UTC)/g,
                      '<span class="hl-wib">$1</span>');
  // Separator pipes
  text = text.replace(/ \\| /g, ' <span class="hl-sep">|</span> ');
  return text;
}

async function refreshTail() {
  try {
    const r = await fetch('/api/tail?n=800');
    const text = await r.text();
    const atBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 50;
    if (text.length !== lastLen) {
      lastLen = text.length;
      lastChange = Date.now();
    }
    out.innerHTML = highlight(text);
    if (autoscroll.checked || atBottom) {
      out.scrollTop = out.scrollHeight;
    }
  } catch (e) {
    statusText.textContent = 'err: ' + e.message;
  }
}

function renderRecent(trades) {
  const el = document.getElementById('recentList');
  if (!trades || trades.length === 0) {
    el.className = 'empty';
    el.textContent = 'No live trades yet.';
    return;
  }
  el.className = '';
  el.innerHTML = trades.slice().reverse().map(t => {
    const statusCls = 'status-' + t.status.toLowerCase();
    const rowCls = 'trade-row ' + t.status.toLowerCase();
    const sideCls = 'side-' + t.side;
    const ts = t.ts.replace(' WIB', '');
    return `
      <div class="${rowCls}">
        <span class="hl-wib">${escapeHtml(ts)}</span>
        <span class="${statusCls}">${t.status}</span>
        <span class="${sideCls}">${t.side}</span>
        <span>@ ${t.price.toFixed(3)}</span>
        <span>x ${t.shares.toFixed(0)}</span>
        <span>$${t.cost.toFixed(2)}</span>
        <span class="hl-edge">edge ${t.edge >= 0 ? '+' : ''}${t.edge.toFixed(1)}%</span>
      </div>`;
  }).join('');
}

async function refreshStats() {
  try {
    const r = await fetch('/api/stats');
    const s = await r.json();

    document.getElementById('btcPrice').textContent =
      s.btc_price ? '$' + s.btc_price.toLocaleString('en-US', {maximumFractionDigits: 0}) : '—';
    document.getElementById('cycle').textContent = s.cycle || '—';
    document.getElementById('stTrades').textContent = s.trades_attempted;
    document.getElementById('stFilled').textContent = s.filled;
    const fillRate = s.trades_attempted > 0
      ? ((s.filled / s.trades_attempted) * 100).toFixed(0) + '%'
      : '—';
    document.getElementById('stFillRate').textContent = fillRate + ' fill rate';

    const failTotal = s.failed + s.errors;
    document.getElementById('stFailed').textContent = failTotal;
    document.getElementById('stFailDetail').textContent =
      `${s.failed} failed · ${s.errors} errors`;

    document.getElementById('stDeployed').textContent =
      '$' + s.deployed_usd.toFixed(2);

    const pnlEl = document.getElementById('stPnl');
    pnlEl.textContent = (s.expected_pnl >= 0 ? '+' : '') +
      '$' + s.expected_pnl.toFixed(2);
    pnlEl.style.color = s.expected_pnl >= 0 ? 'var(--green)' : 'var(--red)';

    const skips = s.skip_reasons || {};
    const totalSkip = Object.values(skips).reduce((a, b) => a + b, 0);
    document.getElementById('stSkipped').textContent = totalSkip;
    const topSkip = Object.entries(skips).sort((a,b) => b[1] - a[1])[0];
    document.getElementById('stSkipTop').textContent =
      topSkip ? `most: ${topSkip[0]} (${topSkip[1]})` : '—';

    renderRecent(s.recent_trades);
  } catch (e) {
    /* ignore */
  }
}

function refreshStatus() {
  const ageSec = (Date.now() - lastChange) / 1000;
  if (ageSec < 30) {
    statusEl.classList.remove('stale');
    statusText.textContent = 'LIVE';
  } else if (ageSec < 300) {
    statusEl.classList.remove('stale');
    statusText.textContent = `idle ${Math.round(ageSec)}s`;
  } else {
    statusEl.classList.add('stale');
    statusText.textContent = `STALE ${Math.round(ageSec/60)}m`;
  }
  nowEl.textContent = new Date().toLocaleTimeString('id-ID');
}

async function tick() {
  await Promise.all([refreshTail(), refreshStats()]);
  refreshStatus();
}

tick();
setInterval(tick, 3000);
</script>
</body>
</html>
"""


async def index(request: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type="text/html")


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/tail", api_tail)
    app.router.add_get("/api/stats", api_stats)
    return app


if __name__ == "__main__":
    web.run_app(build_app(), host="0.0.0.0", port=8080)
