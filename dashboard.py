"""Dashboard untuk memantau signals.log + near_miss.log secara real-time.

Usage:
    python dashboard.py
    # buka http://localhost:8080 di browser

Auto-refresh tiap 5 detik. Menampilkan sinyal BUY terkini dengan warna
berdasar besar edge, serta statistik sinyal per jam.
"""
from __future__ import annotations

import re
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).parent
SIGNALS_LOG = ROOT / "signals.log"
NEAR_MISS_LOG = ROOT / "near_miss.log"

# ET → WIB: +11 jam saat EDT (Mar-Nov), +12 saat EST (Nov-Mar).
# Default EDT karena mayoritas musim trading BTC jatuh di DST.
ET_TO_WIB_HOURS = 11


def _et_to_wib_str(hour: int, minute: int, ampm: str) -> str:
    """Konversi jam ET (12h format) ke string WIB (24h)."""
    h = hour % 12
    if ampm.upper() == "PM":
        h += 12
    total = h * 60 + minute + ET_TO_WIB_HOURS * 60
    total %= 24 * 60
    return f"{total // 60:02d}:{total % 60:02d}"


_ET_TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?(AM|PM)", re.IGNORECASE)


def question_to_wib(question: str) -> str:
    """Ubah setiap jam 'hh(:mm)?(AM|PM)' di question jadi 'HH:MM WIB'.

    'April 19, 12:00AM-4:00AM ET' → 'April 19, 11:00-15:00 WIB'
    'April 19, 2AM ET'            → 'April 19, 13:00 WIB'
    """
    if " ET" not in question and " et" not in question:
        return ""

    def repl(m: re.Match) -> str:
        h = int(m.group(1))
        mm = int(m.group(2) or "0")
        return _et_to_wib_str(h, mm, m.group(3))

    converted = _ET_TIME_RE.sub(repl, question)
    # Ganti label ET → WIB (case-insensitive, cuma yang berdiri sendiri)
    converted = re.sub(r"\bET\b", "WIB", converted)
    return converted

# Contoh baris:
# [2026-04-19 13:19:26 WIB] ACT EDGE=+26.0% | exp=UD5m 0.6m-left | BTC=$75,350 | buy NO @ 0.640, BTC $75,350 down from open $75,389 | sz_yes=137 sz_no=122 | Bitcoin Up or Down - April 19, 2:15AM-2:20AM ET
LINE_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\]\s+"
    r"(?P<tag>ACT|NM)\s+"
    r"EDGE=(?P<edge>[+-][\d.]+)%\s*\|\s*"
    r"exp=(?P<exp>[^|]+?)\s*\|\s*"
    r"BTC=\$(?P<btc>[\d,]+)\s*\|\s*"
    r"buy\s+(?P<side>YES|NO)\s+@\s+(?P<price>[\d.]+)[^|]*\|\s*"
    r"sz_yes=(?P<sz_yes>[\d.]+)\s+sz_no=(?P<sz_no>[\d.]+)\s*\|\s*"
    r"(?P<question>.+?)\s*$"
)


_EXP_UD_RE = re.compile(r"UD(\d+)m\s+([\d.]+)m-left")
_EXP_H_RE = re.compile(r"([\d.]+)h")


def _parse_exp(exp: str) -> tuple[float | None, str | None]:
    """Return (minutes_left, window_label).

    window_label is human-readable like '5m', '4h', or None for strike.
    """
    m = _EXP_UD_RE.match(exp)
    if m:
        window_min = int(m.group(1))
        mins_left = float(m.group(2))
        if window_min >= 60 and window_min % 60 == 0:
            window_label = f"{window_min // 60}h"
        else:
            window_label = f"{window_min}m"
        return mins_left, window_label
    m = _EXP_H_RE.match(exp)
    if m:
        return float(m.group(1)) * 60, None
    return None, None


def parse_line(line: str) -> dict | None:
    m = LINE_RE.match(line)
    if not m:
        return None
    d = m.groupdict()
    edge = float(d["edge"])
    price = float(d["price"])
    side = d["side"]
    act_size = float(d["sz_yes"] if side == "YES" else d["sz_no"])
    exp_raw = d["exp"].strip()
    mins_left, window_label = _parse_exp(exp_raw)
    return {
        "ts": d["ts"],
        "tag": d["tag"],
        "edge": edge,
        "exp": exp_raw,
        "exp_min_left": mins_left,
        "exp_window": window_label,
        "btc": int(d["btc"].replace(",", "")),
        "side": side,
        "price": price,
        "sz_yes": float(d["sz_yes"]),
        "sz_no": float(d["sz_no"]),
        "act_size": act_size,
        "question": d["question"],
        "question_wib": question_to_wib(d["question"]),
        "notional_usd": round(act_size * price, 2),
        "potential_profit_usd": round(act_size * (1 - price), 2) if side == "YES" else round(act_size * price * (edge / 100), 2),
    }


def read_log(path: Path, limit: int) -> list[dict]:
    if not path.exists():
        return []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    parsed: list[dict] = []
    for line in reversed(lines):
        p = parse_line(line)
        if p is not None:
            parsed.append(p)
            if len(parsed) >= limit:
                break
    return parsed


async def api_signals(request: web.Request) -> web.Response:
    limit = int(request.query.get("limit", "100"))
    data = {
        "actionable": read_log(SIGNALS_LOG, limit),
        "near_miss": read_log(NEAR_MISS_LOG, limit),
    }
    return web.json_response(data)


INDEX_HTML = """<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="utf-8">
<title>Polymarket Arb Dashboard</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: -apple-system, ui-sans-serif, system-ui, sans-serif;
         background: #0f1115; color: #e6e6e6; margin: 0; padding: 18px; }
  h1 { font-size: 18px; margin: 0 0 4px; }
  .sub { color: #8a93a6; font-size: 12px; margin-bottom: 14px; }
  .tabs { display: flex; gap: 6px; margin-bottom: 10px; }
  .tab { background: #1a1e28; border: 1px solid #242a36; color: #cbd3e1;
         padding: 6px 12px; border-radius: 6px; cursor: pointer; font-size: 13px; }
  .tab.active { background: #2a3247; border-color: #3a4663; color: #fff; }
  table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #20242e; }
  th { color: #8a93a6; font-weight: 500; background: #13161c; position: sticky; top: 0; }
  tr:hover { background: #141822; }
  .edge { font-weight: 600; font-variant-numeric: tabular-nums; }
  .edge.huge { color: #4ade80; }
  .edge.good { color: #facc15; }
  .edge.small { color: #94a3b8; }
  .side-yes { color: #4ade80; font-weight: 600; }
  .side-no  { color: #f87171; font-weight: 600; }
  .price { font-variant-numeric: tabular-nums; }
  .stats { display: flex; gap: 18px; margin-bottom: 14px; }
  .stat { background: #151925; border: 1px solid #222836; border-radius: 8px;
          padding: 10px 14px; min-width: 120px; }
  .stat .label { font-size: 11px; color: #8a93a6; text-transform: uppercase; letter-spacing: 0.5px; }
  .stat .value { font-size: 20px; font-weight: 600; margin-top: 2px; }
  .question { color: #cbd3e1; max-width: 460px; }
  .question .q-main { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .question .q-wib  { color: #7dd3fc; font-size: 11.5px; margin-top: 2px;
                      overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .exp { font-variant-numeric: tabular-nums; white-space: nowrap; }
  .exp-left { font-weight: 600; }
  .exp-left.urgent { color: #f87171; }      /* < 5 min */
  .exp-left.soon   { color: #fb923c; }      /* < 30 min */
  .exp-left.near   { color: #facc15; }      /* < 2h */
  .exp-left.far    { color: #a5b4cc; }      /* >= 2h */
  .exp-window { color: #6a7388; font-size: 11px; margin-left: 4px; }
  .ts { color: #6a7388; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .empty { padding: 30px; text-align: center; color: #6a7388; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
         background: #4ade80; margin-right: 6px; animation: pulse 1.6s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .3; } }
</style>
</head>
<body>
  <h1>Polymarket Latency Arb — Live Dashboard</h1>
  <div class="sub"><span class="dot"></span>auto-refresh 5s · <span id="now"></span></div>

  <div class="stats">
    <div class="stat"><div class="label">Actionable (1h)</div><div class="value" id="act-1h">–</div></div>
    <div class="stat"><div class="label">Actionable (24h)</div><div class="value" id="act-24h">–</div></div>
    <div class="stat"><div class="label">Avg edge (actionable)</div><div class="value" id="avg-edge">–</div></div>
    <div class="stat"><div class="label">Near-miss (1h)</div><div class="value" id="nm-1h">–</div></div>
  </div>

  <div class="tabs">
    <div class="tab active" data-tab="act">Actionable</div>
    <div class="tab" data-tab="nm">Near Miss</div>
  </div>

  <table>
    <thead>
      <tr>
        <th>Time</th><th>Edge</th><th>Action</th><th>Price</th>
        <th>Size</th><th>Exp</th><th>BTC</th><th>Market</th>
      </tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>

<script>
let currentTab = 'act';
let latest = { actionable: [], near_miss: [] };

document.querySelectorAll('.tab').forEach(t => {
  t.onclick = () => {
    document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    currentTab = t.dataset.tab;
    render();
  };
});

function edgeClass(e) {
  if (e >= 10) return 'edge huge';
  if (e >= 3)  return 'edge good';
  return 'edge small';
}

function formatExp(r) {
  const m = r.exp_min_left;
  if (m === null || m === undefined) return `<span class="exp-left far">${r.exp}</span>`;
  let cls = 'far';
  if (m < 5)       cls = 'urgent';
  else if (m < 30) cls = 'soon';
  else if (m < 120) cls = 'near';
  let txt;
  if (m < 1)        txt = Math.max(1, Math.round(m * 60)) + ' det';
  else if (m < 60)  txt = m.toFixed(1) + ' mnt';
  else {
    const h = Math.floor(m / 60);
    const mm = Math.round(m % 60);
    txt = mm === 0 ? `${h} jam` : `${h}j ${mm}m`;
  }
  const win = r.exp_window ? `<span class="exp-window">(${r.exp_window})</span>` : '';
  return `<span class="exp-left ${cls}">${txt}</span>${win}`;
}

function tsToDate(ts) {
  // "2026-04-19 13:19:26 WIB" → Date
  const m = ts.match(/(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})/);
  if (!m) return null;
  // WIB = UTC+7, so subtract 7h to get UTC then create Date
  return new Date(Date.UTC(+m[1], +m[2]-1, +m[3], +m[4]-7, +m[5], +m[6]));
}

function minutesAgo(ts) {
  const d = tsToDate(ts);
  if (!d) return Infinity;
  return (Date.now() - d.getTime()) / 60000;
}

function render() {
  const rows = currentTab === 'act' ? latest.actionable : latest.near_miss;
  const tbody = document.getElementById('rows');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty">Belum ada sinyal. Scanner masih jalan?</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map(r => `
    <tr>
      <td class="ts">${r.ts}</td>
      <td class="${edgeClass(r.edge)}">+${r.edge.toFixed(1)}%</td>
      <td class="side-${r.side.toLowerCase()}">BUY ${r.side}</td>
      <td class="price">${r.price.toFixed(3)}</td>
      <td class="price">${r.act_size.toFixed(0)}</td>
      <td class="exp">${formatExp(r)}</td>
      <td class="price">$${r.btc.toLocaleString()}</td>
      <td class="question" title="${r.question}">
        <div class="q-main">${r.question}</div>
        ${r.question_wib ? `<div class="q-wib">→ ${r.question_wib}</div>` : ''}
      </td>
    </tr>
  `).join('');
}

function updateStats() {
  const act1h = latest.actionable.filter(r => minutesAgo(r.ts) <= 60).length;
  const act24h = latest.actionable.filter(r => minutesAgo(r.ts) <= 1440).length;
  const nm1h = latest.near_miss.filter(r => minutesAgo(r.ts) <= 60).length;
  const actRecent = latest.actionable.filter(r => minutesAgo(r.ts) <= 1440);
  const avgEdge = actRecent.length
    ? (actRecent.reduce((s,r) => s + r.edge, 0) / actRecent.length).toFixed(1) + '%'
    : '–';
  document.getElementById('act-1h').textContent = act1h;
  document.getElementById('act-24h').textContent = act24h;
  document.getElementById('avg-edge').textContent = avgEdge;
  document.getElementById('nm-1h').textContent = nm1h;
}

async function refresh() {
  try {
    const r = await fetch('/api/signals?limit=200');
    latest = await r.json();
    render();
    updateStats();
    document.getElementById('now').textContent =
      'last update ' + new Date().toLocaleTimeString('id-ID');
  } catch (e) { /* ignore */ }
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


async def index(request: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type="text/html")


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/signals", api_signals)
    return app


if __name__ == "__main__":
    web.run_app(build_app(), host="0.0.0.0", port=8080)
