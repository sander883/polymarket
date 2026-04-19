"""Dashboard super simple — mirror output terminal scanner ke browser.

Cara pakai:

    # terminal A: jalankan scanner sambil tee ke file
    python crypto_arb_scan.py 2>&1 | tee scan_output.log

    # terminal B: jalankan dashboard
    python dashboard.py

    # browser: http://<ip-vps>:8080

Dashboard cuma tail file `scan_output.log` dan nampilin sebagai teks
monospace dengan auto-refresh 3 detik. Tidak ada parsing, tidak ada
tabel — yang kamu lihat di terminal = yang kamu lihat di browser.
"""
from __future__ import annotations

from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parent
OUTPUT_FILE = ROOT / "scan_output.log"
MAX_LINES = 500  # ekor terakhir yang ditampilkan


def tail(path: Path, n: int) -> str:
    if not path.exists():
        return f"(file {path.name} belum ada — jalankan: python crypto_arb_scan.py 2>&1 | tee {path.name})"
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError as exc:
        return f"(gagal baca {path.name}: {exc})"
    return "\n".join(lines[-n:])


async def api_tail(request: web.Request) -> web.Response:
    n = int(request.query.get("n", str(MAX_LINES)))
    return web.Response(text=tail(OUTPUT_FILE, n), content_type="text/plain")


INDEX_HTML = """<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="utf-8">
<title>Scanner Live Output</title>
<style>
  :root { color-scheme: dark; }
  html, body { margin: 0; padding: 0; height: 100%; background: #0b0d12; color: #d4d4d4;
               font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  header { padding: 8px 14px; border-bottom: 1px solid #1e232d; background: #0f1218;
           font-size: 12px; display: flex; align-items: center; gap: 12px; }
  header .dot { width: 8px; height: 8px; border-radius: 50%; background: #4ade80;
                animation: pulse 1.6s infinite; }
  header .dot.stale { background: #f87171; animation: none; }
  header .label { color: #8a93a6; }
  header .value { color: #d4d4d4; }
  header .spacer { flex: 1; }
  header label { color: #8a93a6; font-size: 11px; }
  header input[type=checkbox] { accent-color: #4ade80; vertical-align: middle; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .3; } }
  pre { margin: 0; padding: 12px 14px; white-space: pre-wrap; word-break: break-word;
        font-size: 12.5px; line-height: 1.45; height: calc(100vh - 40px);
        overflow-y: auto; }
  pre .hl-edge    { color: #4ade80; font-weight: 600; }
  pre .hl-buy-yes { color: #86efac; font-weight: 600; }
  pre .hl-buy-no  { color: #fca5a5; font-weight: 600; }
  pre .hl-skip    { color: #6a7388; }
  pre .hl-sep     { color: #3b4250; }
  pre .hl-wib     { color: #7dd3fc; }
  pre .hl-btc     { color: #fde68a; }
</style>
</head>
<body>
  <header>
    <span class="dot" id="dot"></span>
    <span class="label">status</span><span class="value" id="status">loading…</span>
    <span class="spacer"></span>
    <span class="label">last update</span><span class="value" id="now">–</span>
    <label><input type="checkbox" id="autoscroll" checked> auto-scroll</label>
  </header>
  <pre id="out">loading…</pre>

<script>
const out = document.getElementById('out');
const statusEl = document.getElementById('status');
const dot = document.getElementById('dot');
const nowEl = document.getElementById('now');
const autoscroll = document.getElementById('autoscroll');
let lastLen = 0;
let lastChange = Date.now();

// Minimal highlighter — regex ganti ke span bewarna.
function highlight(text) {
  // Escape HTML dulu
  text = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  // Edge percentages
  text = text.replace(/(EDGE=[+-]?\\d+(?:\\.\\d+)?%)/g, '<span class="hl-edge">$1</span>');
  text = text.replace(/(\\+\\d+(?:\\.\\d+)?%)/g, (m) => `<span class="hl-edge">${m}</span>`);
  // buy YES / buy NO
  text = text.replace(/\\b(buy YES)\\b/gi, '<span class="hl-buy-yes">$1</span>');
  text = text.replace(/\\b(buy NO)\\b/gi, '<span class="hl-buy-no">$1</span>');
  // SKIP
  text = text.replace(/\\bSKIP\\b/g, '<span class="hl-skip">SKIP</span>');
  // BTC prices: $74,500 / $74,500.00
  text = text.replace(/(\\$\\d{2,3}(?:,\\d{3})*(?:\\.\\d+)?)/g, '<span class="hl-btc">$1</span>');
  // WIB timestamps
  text = text.replace(/(\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2} WIB)/g, '<span class="hl-wib">$1</span>');
  // Separator pipes
  text = text.replace(/ \\| /g, ' <span class="hl-sep">|</span> ');
  return text;
}

async function refresh() {
  try {
    const r = await fetch('/api/tail?n=500');
    const text = await r.text();
    const atBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 40;
    if (text.length !== lastLen) {
      lastLen = text.length;
      lastChange = Date.now();
    }
    out.innerHTML = highlight(text);
    if (autoscroll.checked || atBottom) {
      out.scrollTop = out.scrollHeight;
    }
    const ageSec = (Date.now() - lastChange) / 1000;
    if (ageSec < 30) {
      dot.classList.remove('stale');
      statusEl.textContent = 'LIVE';
    } else if (ageSec < 300) {
      dot.classList.remove('stale');
      statusEl.textContent = `idle ${Math.round(ageSec)}s`;
    } else {
      dot.classList.add('stale');
      statusEl.textContent = `STALE ${Math.round(ageSec / 60)}m`;
    }
    nowEl.textContent = new Date().toLocaleTimeString('id-ID');
  } catch (e) {
    statusEl.textContent = 'err: ' + e.message;
  }
}

refresh();
setInterval(refresh, 3000);
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
    return app


if __name__ == "__main__":
    web.run_app(build_app(), host="0.0.0.0", port=8080)
