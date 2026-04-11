# polymarket

Personal no-arbitrage trading bot untuk Polymarket. Strategi: **Kandidat A, Tipe 1**
— deteksi `YES_ask + NO_ask < $1` di binary market yang sama, beli dua-duanya, lock
profit saat resolusi (terlepas dari outcome).

Filosofi: fondasi dulu, baru strategi. Tidak ada LLM, tidak ada multi-agent, tidak
ada tebakan outcome — cuma eksploit inkonsistensi harga yang matematis.

## Struktur

- `requirements.txt` — dependensi Python
- `.env.example` — template env (wallet + API keys, diisi nanti saat fase live)
- `verify_polymarket_access.py` — **Fase 0**: cek konektivitas Gamma + CLOB, dry-run
  deteksi Tipe 1 arb. Read-only, tanpa wallet.
- `fetch_data.py` — utility lama, ambil OHLCV BTC/USDT dari Binance via ccxt
  (dipakai nanti sebagai external signal kalau perlu)

## Roadmap fase

| Fase | Isi | Status |
|---|---|---|
| 0   | Verifikasi akses Gamma + CLOB dari lokasi Anda | **← saat ini** |
| 1   | Data foundation (indexer Polymarket, parquet, DuckDB) | belum |
| 2   | Query & market explorer CLI | belum |
| 3   | Wallet + execution layer (paper + live behind flag) | belum |
| 4   | Risk guards + monitoring | belum |
| 5   | Backtest harness generic | belum |
| 6   | Strategy module: Tipe 1 arb | belum |

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

## Catatan `fetch_data.py`

Script ini dari iterasi awal — fetch OHLCV + ticker BTC dari Binance via ccxt.
Dibiarkan karena tidak mengganggu dan mungkin berguna nanti sebagai external
signal. Jalankan dengan:

```bash
python fetch_data.py
```

## Troubleshooting Fase 0

- `httpx.ConnectError` / timeout → koneksi diblok atau ISP throttle Cloudflare
- Gamma `403` → kemungkinan geo-block (Polymarket blokir US). Kalau Anda non-US
  dan masih 403, coba VPN atau ubah user-agent
- `ModuleNotFoundError: httpx` → `pip install -r requirements.txt` belum dijalankan
  dalam virtualenv aktif
- Scan muncul tapi semua market "no liquidity" → turunkan `--min-volume`, atau
  market yang diambil kebetulan resolve-nya dekat dan orderbook sudah thin
