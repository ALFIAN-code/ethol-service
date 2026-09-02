# Ethol Notifier - Absensi WA

Service Python polling `ethol.pens.ac.id` tiap 3 menit, kirim notifikasi WhatsApp via Baileys (gateway Node ringan). Deploy 1 perintah di home server.

Stack: `ethol-notification.py` (Python) + `wa-gateway/` (Node Baileys) via `docker-compose.yml`.

## Deploy Cepat (1 Menit)

**1. Siapkan `.env`**
```bash
cp .env.example .env
nano .env
```
Isi 3 field wajib:
```
NETID=nim@it.student.pens.ac.id
PASSWORD=password_ethol
WA_TARGET=6281515867972   # nomor kamu penerima notif, format 628xxx
```
Lainnya biarkan default.

**2. Jalankan**
```bash
docker compose up --build -d
```

**3. Scan QR WhatsApp (1x saja)**
```bash
docker logs -f wa_gateway
```
QR muncul di log. Scan via **WhatsApp > Perangkat Tertaut > Tautkan Perangkat**.

Alternatif tanpa lihat log:
```bash
curl http://localhost:3000/qr
# copy string -> buka https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=STRING_QR
```

**4. Verifikasi**
```bash
docker compose ps
curl http://localhost:3000/status
# {"connected":true} = siap

docker logs -f ethol_notifier
# Run pertama: "Baseline X notifikasi disimpan" (tidak spam WA lama)
# Notif baru: "Notifikasi baru: PRESENSI-KULIAH ..." + "WA terkirim via Baileys gateway"
```

Tes manual kirim WA:
```bash
curl -X POST http://localhost:3000/send -H "Content-Type: application/json" \
  -d '{"number":"6281515867972","text":"tes ethol-notifier"}'
```

## Perintah Harian

```bash
docker compose logs -f              # lihat semua log
docker compose logs -f ethol-notifier
docker compose logs -f wa_gateway
docker compose restart              # restart service
docker compose down                 # stop
docker compose up -d --build        # update setelah edit .env / code
```

## Konfigurasi

Edit `.env`:

| Var | Default | Keterangan |
|---|---|---|
| `WA_TARGET` | `6281515867972` | Nomor penerima |
| `KODE_YANG_DIPANTAU` | kosong (semua) | Filter: `PRESENSI-KULIAH` saja isi `PRESENSI-KULIAH`, multi `PRESENSI-KULIAH,TUGAS-BARU` |
| `POLL_INTERVAL_SECONDS` | `180` | Interval polling ( detik ) |
| `WA_GATEWAY_API_KEY` | kosong | Kosong = tanpa auth |

`state.json` menyimpan `idNotifikasi` yang sudah dikirim biar tidak duplikat. Hapus file ini jika mau re-kirim baseline.

## Troubleshooting

| Gejala | Solusi |
|---|---|
| `whatsapp not connected` di log | `docker logs -f wa_gateway` cek QR, scan ulang. `curl http://localhost:3000/status` harus `connected:true` |
| `Login CAS gagal` berulang | Cek `NETID`/`PASSWORD` di `.env`, `docker compose restart ethol-notifier` |
| QR tidak muncul | `docker compose restart wa_gateway && docker logs -f wa_gateway` |
| Mau ganti nomor WA pengirim | `docker volume rm ethol-service_wa_auth` lalu `up -d` dan scan ulang (auth tersimpan di volume `wa_auth`) |
| `state.json` permission error | `touch state.json && chmod 666 state.json` lalu `docker compose restart ethol-notifier` |

## Struktur

```
.
├── docker-compose.yml      # 2 service: wa-gateway + ethol-notifier
├── Dockerfile              # Python 3.11 slim
├── requirements.txt
├── ethol-notification.py   # logic polling ethol
├── wa-gateway/             # Baileys gateway (Node)
│   ├── index.js
│   ├── package.json
│   └── Dockerfile
├── .env.example            # template env
└── state.json              # auto-generate, jangan commit
```
