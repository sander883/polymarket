# polymarket

Project trading Polymarket. Step pertama: ambil data market BTC dari Binance via
[`ccxt`](https://github.com/ccxt/ccxt) sebagai fondasi sinyal/analisis.

## Struktur

- `requirements.txt` — dependensi Python
- `.env.example` — template variabel env (copy ke `.env` dan isi dengan kunci Anda)
- `fetch_data.py` — fetch OHLCV + ticker BTC/USDT dari Binance (public API)

## Install (Linux)

Disarankan pakai virtualenv supaya dependensi project terisolasi.

```bash
# 1. Masuk ke folder project
cd polymarket

# 2. Buat & aktifkan virtualenv
python3 -m venv .venv
source .venv/bin/activate

# 3. Upgrade pip lalu install dependensi
pip install --upgrade pip
pip install -r requirements.txt

# 4. (Opsional untuk step ini) siapkan file .env
cp .env.example .env
# edit .env dan isi kunci API jika sudah punya
```

> Catatan: `fetch_data.py` hanya memakai endpoint publik Binance, jadi Anda
> **tidak perlu** API key untuk menjalankan script ini. API key baru dibutuhkan
> nanti saat kita mulai baca saldo / kirim order.

## Menjalankan

Pastikan virtualenv aktif, lalu:

```bash
python fetch_data.py
```

Output yang diharapkan:

1. Tabel 100 candle terakhir `BTC/USDT` timeframe 1 menit
   (kolom: Time UTC, Open, High, Low, Close, Volume).
2. Ringkasan ticker real-time (last, bid, ask, timestamp).

## Troubleshooting

- `ccxt.NetworkError` / timeout → cek koneksi internet, beberapa ISP / region
  memblok endpoint Binance. Coba lewat VPN atau ganti exchange.
- `ModuleNotFoundError: ccxt` → pastikan virtualenv aktif dan
  `pip install -r requirements.txt` sudah dijalankan.
