# polymarket

Personal no-arbitrage trading bot untuk Polymarket. Strategi: **Kandidat A, Tipe 1**
— deteksi `YES_ask + NO_ask < $1` di binary market yang sama, beli dua-duanya, lock
profit saat resolusi (terlepas dari outcome).

Filosofi: fondasi dulu, baru strategi. Tidak ada LLM, tidak ada multi-agent, tidak
ada tebakan outcome — cuma eksploit inkonsistensi harga yang matematis.

## Struktur

- `requirements.txt` — dependensi Python
- `.env.example` — template env (wallet + API keys, diisi nanti saat fase live)
- `polymarket_client.py` — **Fase 1.1**: async client untuk Gamma + CLOB API.
  Satu class dipakai semua fase berikutnya (retry, rate limit, parsing, typed
  dataclasses). Read-only.
- `market_discovery.py` — **Fase 1.2**: CLI market explorer. Two-stage pipeline:
  metadata filter (Gamma) → orderbook filter (CLOB). Outputs tabel atau JSON.
- `snapshot.py` — **Fase 1.3**: capture orderbook snapshot → parquet. One file
  per batch, stored under `data/snapshots/YYYY-MM-DD/`. DuckDB-queryable.
- `poll_loop.py` — **Fase 1.4**: continuous polling service. Two cadences:
  5s book refresh + 5min re-discovery. Graceful shutdown, stats on exit.
- `verify_polymarket_access.py` — **Fase 0**: cek konektivitas Gamma + CLOB,
  dry-run deteksi Tipe 1 arb. Sekarang pakai `polymarket_client.py` di bawah.
- `fetch_data.py` — utility lama, ambil OHLCV BTC/USDT dari Binance via ccxt
  (dipakai nanti sebagai external signal kalau perlu)

## Roadmap fase

| Fase  | Isi | Status |
|---|---|---|
| 0     | Verifikasi akses Gamma + CLOB dari lokasi Anda | ✅ |
| 1.1   | `polymarket_client.py` — async client foundation | ✅ |
| 1.2   | `market_discovery.py` — smart market filter CLI | ✅ |
| 1.3   | `snapshot.py` — orderbook snapshot + parquet storage | ✅ |
| 1.4   | `poll_loop.py` — periodic snapshot service | ✅ |
| 2     | Query explorer via DuckDB | belum |
| 3     | Wallet + execution layer (paper + live behind flag) | belum |
| 4     | Risk guards + monitoring | belum |
| 5     | Backtest harness generic | belum |
| 6     | Strategy module: Tipe 1 arb | belum |

## Install (Linux)

```bash
cd polymarket
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env        # isi nanti saat fase wallet
```

## Fase 0: verifikasi akses Polymarket

**Tujuan**: sebelum invest waktu ke fase berikutnya, konfirmasi bahwa mesin Anda
bisa reach Gamma API + CLOB API Polymarket. Ini read-only, tanpa wallet, tanpa
risiko finansial.

```bash
python verify_polymarket_access.py
```

Opsional flag:

```bash
python verify_polymarket_access.py --limit 20 --min-volume 50000 --verbose
```

**Apa yang dilakukan script**:

1. GET `https://gamma-api.polymarket.com/markets` — discover N market aktif
   dengan volume di atas threshold
2. Untuk tiap market → fetch orderbook YES + NO dari `clob.polymarket.com/book`
3. Hitung `edge = 1 - (yes_ask + no_ask) - fee - safety`
4. Report tabel scan + flag arb candidate (kalau ada)
5. Exit 0 kalau semua check lolos

**Interpretasi exit code**:

| Exit | Arti | Next step |
|---|---|---|
| 0 | Semua lolos, data shape sehat | Lanjut ke Fase 1 |
| 2 | Gamma API tidak reachable (network/geo-block) | Cek VPN / lokasi |
| 3 | Gamma response shape tidak sesuai | Schema API berubah, investigate |
| 4 | CLOB API tidak reachable | Network check |
| 5 | CLOB response shape tidak sesuai | Schema CLOB berubah |

**Expected output (contoh)**:

```
[STEP 1] Gamma API (market discovery)
  OK    GET https://gamma-api.polymarket.com/markets -> 200 (XXX ms)
  OK    fetched 10 markets above $10,000 volume
  OK    parsed 10 simple binary markets with 2 CLOB tokens

[STEP 2] CLOB API (orderbook per market)
  OK    CLOB reachable; scanned 10 markets

[STEP 3] Scan summary
  | market_id | question        | yes_ask | no_ask | sum    | edge*   |
  |-----------|-----------------|---------|--------|--------|---------|
  | ...       | ...             | 0.58    | 0.43   | 1.01   | -1.50%  |
  ...

[STEP 4] Type-1 arb candidates (edge > 0)
  no arb detected in this scan (expected — arbs are rare and fleeting).
```

> **Tidak dapet arb candidate itu normal dan expected** di scan pertama. Tujuan
> Fase 0 cuma konfirmasi pipeline data jalan, bukan cari uang.

## Phase 0 findings (penting — baca sebelum Phase 1)

Run pertama Fase 0 mengembalikan 10 market, dan **semua 10 baris menunjukkan
`yes_ask + no_ask = 1.001` persis**. Ini bukan kebetulan.

**Interpretasi**: Polymarket CLOB pakai **tick size 0.001** ($0.001 / 0.1 sen).
Market maker aktif mem-park order di floor minimum mereka, yaitu `fair + 1 tick`
di kedua sisi. Akibatnya lantai natural `yes_ask + no_ask` di top-of-book adalah
**1.001, bukan 1.000**. Di market dengan MM aktif, sum hampir tidak pernah
turun ke ≤ 1.000 di best ask.

**Implikasi untuk strategi Tipe 1 (YES + NO arb)**:

- Arb Tipe 1 di top-of-book di market **liquid** ≈ nol peluang. MM enforce 1.001.
- Arb Tipe 1 **hanya muncul** pada window spesifik:
  1. Market baru listing, sebelum MM arrive (detik–menit)
  2. Market illiquid yang MM tidak cover (tapi exit liquidity jadi masalah)
  3. Fast move sepihak, satu sisi lag sesaat (kompetisi HFT)
  4. Panic/whale dump event
- Frekuensi-nya **belum diketahui empiris** — inilah yang harus Phase 1 ukur.

**Keputusan**: tetap kejar Tipe 1 sebagai MVP, tapi dengan **ekspektasi realistis**.
Phase 1 di-design untuk **mengukur empiris**:

1. Seberapa sering `sum < 0.995` muncul dalam 24–72 jam polling?
2. Di kategori market apa (politics, crypto, sports, culture)?
3. Berapa lama jendela arb terbuka (latency matters)?
4. Berapa size yang fillable di jendela itu?

Kalau data Phase 1 bilang Tipe 1 terlalu jarang / terlalu kecil → pivot ke Tipe 2
(sum-to-1 multi-outcome) yang MM **tidak enforce** karena butuh pair-matching
lintas market. Pipeline client/discovery/snapshot 100% reusable — tidak ada
kerja sia-sia.

**Side finding**: sort by `volumeNum` descending di Gamma API mengembalikan
market novelty dead-tail (Jesus return, LeBron president, Chelsea Clinton
nomination) karena "volume kumulatif historis" ≠ "liquiditas aktif sekarang".
Phase 1.2 (market discovery) akan ganti filter ke `liquidityNum` + `endDate`
window + `yes_price ∈ [0.1, 0.9]` untuk hindari dead-tail.

## Phase 1.1: `polymarket_client.py`

Async client tunggal untuk semua komunikasi dengan Polymarket (Gamma + CLOB).
Read-only. Dipakai oleh `verify_polymarket_access.py` dan semua script fase
berikutnya. Semua retry, concurrency limit, parsing, dan error handling hidup
di sini supaya kita punya satu tempat untuk reason tentang network behavior.

**Apa yang ada di dalamnya**:

- `PolymarketClient` — async context manager (`async with ...:`)
- `get_markets(...)` — fetch market list dengan filter client-side
- `get_orderbook(token_id)` — 1 orderbook
- `get_orderbooks(token_ids)` — batch concurrent fetch (semaphore-bounded)
- Dataclass: `Market`, `OrderBook`, `OrderBookLevel`
- Exception hierarchy: `PolymarketError` → `NetworkError`, `APIError`,
  `ParseError` (caller bisa pilih mana yang fatal)
- Retry: 3x exponential backoff (0.5s, 1s, 2s) untuk network + 5xx. 4xx
  langsung raise.
- Concurrency: semaphore default 8 (tunable via konstruktor).
- Default sort: `liquidityNum` desc — avoid dead-tail novelty bias yang
  kita temukan di Phase 0.

**Demo**:

```bash
python polymarket_client.py
```

Output contoh (expected): list 5 market paling liquid + bid/ask/spread untuk
token YES mereka. Kalau ini jalan, Fase 1.1 confirmed working end-to-end.

**Library usage** (untuk script Anda sendiri):

```python
import asyncio
from polymarket_client import PolymarketClient

async def main():
    async with PolymarketClient() as client:
        markets = await client.get_markets(limit=20, min_liquidity=50_000)
        token_ids = [m.yes_token_id for m in markets if m.yes_token_id]
        books = await client.get_orderbooks(token_ids)
        for m in markets:
            book = books.get(m.yes_token_id)
            if book and book.best_ask:
                print(m.market_id, m.question[:60], book.best_ask.price)

asyncio.run(main())
```

## Phase 1.2: `market_discovery.py`

Two-stage pipeline yang memecahkan masalah "Phase 0 cuma dapat dead-tail":

1. **Stage 1 (Gamma, metadata filter)**: paginate markets sorted by `liquidityNum`,
   filter binary + active + endDate window + min liquidity.
2. **Stage 2 (CLOB, orderbook filter)**: batch-fetch books, apply `yes_ask ∈
   [price_lo, price_hi]` to exclude 0.01/0.99 zombie markets, compute edge.

```bash
# default: target 30, liq>$50k, end 2h–90d, yes∈[0.05,0.95]
python market_discovery.py

# tighter
python market_discovery.py --min-liquidity 200000 --price-lo 0.10 --price-hi 0.90

# JSON output for Phase 1.3 piping
python market_discovery.py --json > markets.json

# verbose logging (Gamma pagination + book fetch details)
python market_discovery.py -v
```

Output sorted by edge descending (closest to arb first).

## Phase 1.3: `snapshot.py`

Captures orderbook snapshots to parquet. One file per batch, crash-safe (no
in-memory buffer that can be lost).

```bash
# one-shot: discover → snapshot → parquet
python snapshot.py

# custom liquidity floor
python snapshot.py --min-liquidity 100000 --target 50

# custom output dir
python snapshot.py --data-dir /tmp/poly-snapshots
```

Storage layout:

```
data/snapshots/
  2026-04-12/
    snap_143022.parquet    # 30 rows, ~15KB
    snap_143527.parquet
  2026-04-13/
    ...
```

Query with DuckDB:

```sql
SELECT timestamp_ms, market_id, question, book_sum, edge
FROM read_parquet('data/snapshots/**/*.parquet')
WHERE edge > 0
ORDER BY timestamp_ms;
```

Schema per row: `timestamp_ms`, `market_id`, `question`, `yes/no_best_bid/ask_price/size`,
`yes/no_depth_5_ask/bid`, `book_sum`, `edge`, `fillable_size`, `days_remaining`, plus
metadata (`condition_id`, `slug`, `category`, `end_date`, `liquidity`, `volume`).

## Phase 1.4: `poll_loop.py`

Continuous polling service — **ini yang Anda run overnight** untuk kumpulkan
data empiris.

```bash
# default: 5s book interval, 5min re-discovery, 30 markets
python poll_loop.py

# faster polling, more markets
python poll_loop.py --interval 2 --rediscover 120 --target 50

# run overnight (background)
nohup python poll_loop.py > poll.log 2>&1 &
```

Dua cadence:
- **Book refresh** (5s): fetch CLOB books untuk market list yang sudah
  diketahui, write parquet. Cepat, ~1s per cycle.
- **Market re-discovery** (5min): re-run Gamma pipeline untuk pick up
  market baru / drop expired. Lambat, ~3-5s.

Graceful shutdown: Ctrl-C → finish cycle → print summary stats (uptime,
cycles, rows, errors, edge range, arb detections).

Setelah run overnight (~12 jam di 5s interval = ~8,640 snapshots × 30 market
= ~259,200 rows), query dengan DuckDB:

```sql
-- pernah ada arb?
SELECT COUNT(*) FROM read_parquet('data/snapshots/**/*.parquet')
WHERE edge > 0;

-- distribusi sum
SELECT ROUND(book_sum, 3) AS sum_bucket, COUNT(*) AS n
FROM read_parquet('data/snapshots/**/*.parquet')
GROUP BY sum_bucket ORDER BY sum_bucket;
```

## Catatan `fetch_data.py`

Script ini dari iterasi awal — fetch OHLCV + ticker BTC dari Binance via ccxt.
Dibiarkan karena tidak mengganggu dan mungkin berguna nanti sebagai external
signal. Jalankan dengan:

```bash
python fetch_data.py
```

## Troubleshooting

- `httpx.ConnectError` / timeout → koneksi diblok atau ISP throttle Cloudflare
- Gamma `403` → kemungkinan geo-block (Polymarket blokir US). Kalau Anda non-US
  dan masih 403, coba VPN atau ubah user-agent
- `ModuleNotFoundError: httpx` → `pip install -r requirements.txt` belum dijalankan
  dalam virtualenv aktif
- Scan muncul tapi semua market "no liquidity" → turunkan `--min-liquidity`, atau
  market yang diambil kebetulan resolve-nya dekat dan orderbook sudah thin
- `PolymarketNetworkError` setelah 3x retry → network unstable. Tambah timeout
  via konstruktor: `PolymarketClient(timeout=30)`
- `RuntimeError: PolymarketClient must be used inside async with context` →
  Anda instantiate tanpa `async with`. Fix: `async with PolymarketClient() as c:`
