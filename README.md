# Ethol Notifier — Notifikasi Absensi ke WhatsApp

> Kamu tidak perlu jago coding. Ikuti panduan ini langkah demi langkah, copy-paste perintahnya saja.

Program ini mengecek website kampus `ethol.pens.ac.id` setiap 3 menit. Kalau ada **presensi dibuka, tugas baru, atau pengumuman**, kamu langsung dapat chat WhatsApp. Cocok jalan 24 jam di home server / laptop.

Isi: **Python** (cek ethol) + **wa-gateway** (kirim WA via Baileys) jalan bareng pakai **Docker**.

---

## 1. Penjelasan Super Singkat (untuk pemula)

- **Docker** = aplikasi biar program bisa jalan di mana saja tanpa pusing install ini-itu. Anggap seperti "kotak" yang sudah berisi semua yang dibutuhkan.
- **`.env`** = file berisi username/password kamu. Tidak ikut ke-upload ke internet. Kamu isi sekali, program baca dari situ.
- **`docker compose up`** = perintah untuk menyalakan kotaknya.
- **`state.json`** = catatan notifikasi yang sudah dikirim biar tidak spam berulang.
- **QR WhatsApp** = scan sekali saja pakai HP pengirim (bisa pakai nomor kedua biar aman).

---

## 2. Yang Perlu Disiapkan

1. **macOS / Linux / Raspberry Pi** yang bisa menyala lama
2. **Akun Ethol** (NETID & password)
3. **Nomor WhatsApp penerima** (nomor kamu sendiri, format `628xxx` tanpa `+`, contoh `6281234567890`)
4. **Nomor WhatsApp pengirim** — disarankan nomor kedua, bukan nomor utama harian

---

## 3. Install Docker (Pilih Salah Satu)

### macOS - Paling Gampang & Hemat Storage: OrbStack
```bash
brew install orbstack
open -a OrbStack
# tunggu icon OrbStack muncul di menu bar atas
docker --version
# harus muncul versi, berarti siap
```

### Alternatif Gratis: Colima
```bash
brew install colima docker docker-compose
colima start --cpu 2 --memory 2 --disk 10
```

### Linux (Ubuntu/Debian)
```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# logout lalu login lagi
docker --version
docker compose version
```

### windows
bisa di lihat panduan install docker di dokumentasi resminya 

```
https://docs.docker.com/desktop/
```


> Cek berhasil: ketik `docker compose version` dan `docker --version` harus keluar angka versi, bukan error.

---

## 4. Download Project Ini

Jika belum punya foldernya:

```bash
git clone <url-repo-kamu>
cd ethol-service
```

Atau jika sudah ada folder `ethol-service`, masuk ke situ:
```bash
cd ~/alfianSpace/ngoding/personal/ethol-service
ls
# harus ada file: docker-compose.yml, .env.example, ethol-notification.py
```

---

## 5. Isi `.env` (Wajib)

Ini file berisi password kamu. **Jangan pernah upload `.env` ke GitHub.**

```bash
cp .env.example .env
nano .env
# atau open -a TextEdit .env (macOS)
```

Isi 3 baris penting:
```
NETID=alfian2@it.student.pens.ac.id
PASSWORD=password_ethol_kamu
WA_TARGET=6281234567890
```

Penjelasan tiap baris di `.env`:

| Baris | Contoh | Artinya |
|---|---|---|
| `NETID` | `nim@it.student.pens.ac.id` | Username login Ethol |
| `PASSWORD` | `Semuasalah@17` | Password Ethol |
| `WA_TARGET` | `6281234567890` | Nomor HP kamu yang **menerima** notif (pakai 628, bukan 08) |
| `POLL_INTERVAL_SECONDS` | `180` | Cek setiap 180 detik (3 menit), biarkan saja |
| `KODE_YANG_DIPANTAU` | kosong | Kosong = semua notif. Isi `PRESENSI-KULIAH` kalau cuma mau absensi |
| `LOG_LEVEL` | `INFO` | `INFO` = ringkas, `DEBUG` = tampil JSON lengkap |

Simpan: di `nano` tekan `Ctrl+O` → Enter → `Ctrl+X`.

> Cek: `cat .env` harus menampilkan yang kamu isi. Jika salah ketik, `docker compose restart` setelah edit.

---

## 6. Jalankan (1 Perintah)

```bash
docker compose up --build -d
```

Artinya:
- `--build` = rakit kotaknya
- `-d` = jalan di background (tidak menutup terminal)

Tunggu 10 detik, lalu cek:
```bash
docker compose ps
# harus ada 2 baris: wa_gateway (Up) dan ethol_notifier (Up)
```

Kalau ada `Error` atau `Exit`, lihat log:
```bash
docker logs wa_gateway --tail 50
docker logs ethol_notifier --tail 50
```

---

## 7. Scan QR WhatsApp (Hanya Sekali)

Panduan otomatis juga muncul di log `wa_gateway` setelah `docker compose up`.

**Cara A (Paling Mudah — Buka di Browser):**
```
http://localhost:3000/qr
```
Akan muncul QR besar. Scan pakai HP pengirim:
**WhatsApp > Setelan > Perangkat Tertaut > Tautkan Perangkat > Scan**

**Cara B (PNG):**
```
http://localhost:3000/qr.png
```

**Cara C (Lihat di Terminal):**
```bash
docker logs -f wa_gateway
# QR ascii muncul di terminal
```

Cek berhasil:
```bash
curl http://localhost:3000/status
# harus: {"connected":true,"hasQR":false}
# jika masih {"connected":false,"hasQR":true} berarti belum scan
```

> QR ganti setiap 30 detik. Jika expired, refresh `http://localhost:3000/qr`.

---

## 8. Lihat Hasil Data di Log (Sudah Ditambahkan)

Program sekarang print **isi datanya** di log, jadi kamu tahu apa yang dibaca dari Ethol.

```bash
docker logs -f ethol_notifier
```

Contoh log yang akan kamu lihat:
```
2026-09-02 07:20:58 [INFO] Login CAS berhasil. Token exp ~15 menit.
2026-09-02 07:20:58 [INFO] [BASELINE (disimpan, tidak dikirim WA)] 20 data:
2026-09-02 07:20:58 [INFO]   • PRESENSI-KULIAH | Rabu, 02 September 2026 - 10:33 | Dosen telah membuka presensi... | /matakuliah/220837 | id= eacdb371
2026-09-02 07:20:58 [INFO]   • TUGAS-BARU | Rabu, 02 September 2026 - 10:42 | Anda mempunyai tugas baru... | /matakuliah/220837/tugas | id= bcb318fb
2026-09-02 07:20:59 [INFO] [SEMUA] 20 data:
2026-09-02 07:20:59 [INFO] Tidak ada notifikasi baru.
# kalau ada yang baru:
2026-09-02 07:23:59 [INFO] [BARU (akan dikirim WA)] 1 data:
2026-09-02 07:23:59 [INFO]   • PRESENSI-KULIAH | Rabu, 02 September 2026 - 10:45 | Dosen telah membuka presensi... | /matakuliah/220834
2026-09-02 07:23:59 [INFO] → Kirim WA:
✅ *PRESENSI-KULIAH*
Dosen telah membuka presensi untuk matakuliah Kewirausahaan

Waktu: Rabu, 02 September 2026 - 10:45
Link: https://ethol.pens.ac.id/matakuliah/220834
2026-09-02 07:23:59 [INFO] Notifikasi WA terkirim via Baileys gateway.
```

Mau lihat JSON lengkap?
```bash
# di .env ubah:
LOG_LEVEL=DEBUG
docker compose up -d --build
docker logs -f ethol_notifier
# akan muncul JSON indent 2
```

Melihat log `wa_gateway`:
```bash
docker logs -f wa_gateway
# akan ada "wa-gateway listening on :3000" + panduan + QR
```

---

## 9. Tes Kirim WA Manual

Pastikan WA sudah connected dulu (`curl http://localhost:3000/status` = true).

```bash
curl -X POST http://localhost:3000/send -H "Content-Type: application/json" \
  -d '{"number":"6281234567890","text":"tes dari ethol-notifier"}'
# harus balik {"ok":true}
# cek HP penerima, harus masuk chat
```
Ganti `6281234567890` dengan `WA_TARGET` kamu.

---

## 10. Perintah Sehari-hari (Copy-Paste Saja)

```bash
docker compose logs -f              # lihat semua log (Ctrl+C untuk keluar)
docker compose logs -f ethol_notifier  # cuma log python
docker compose logs -f wa_gateway      # cuma log WA

docker compose ps                   # cek status Up / Down
docker compose restart              # restart kalau habis edit .env
docker compose down                 # matikan semua
docker compose up -d --build        # nyalakan lagi setelah edit code

# cek apakah WA masih connect:
curl http://localhost:3000/status

# buka QR lagi:
open http://localhost:3000/qr    # macOS
xdg-open http://localhost:3000/qr   # Linux
```

---

## 11. Kalau Error — Panduan Bahasa Awam

| Kamu Lihat | Artinya | Solusi |
|---|---|---|
| `NETID/PASSWORD kosong` di log | `.env` belum diisi | `nano .env` isi lagi, `docker compose restart ethol_notifier` |
| `Login CAS gagal: NetID/password salah` | Password salah atau captcha | Coba login manual di browser `ethol.pens.ac.id`, kalau bisa berarti password di `.env` typo |
| `whatsapp not connected` | Belum scan QR | Buka `http://localhost:3000/qr` scan lagi |
| `{"connected":false,"hasQR":true}` terus | QR belum discan / expired | Refresh `http://localhost:3000/qr` dan scan cepat |
| `Is a directory: '/app/state.json'` | Bug lama | `rm -rf state.json && echo "[]" > state.json && docker compose restart ethol_notifier` (sudah diperbaiki di versi terbaru) |
| `ENOENT spawn git` saat build | Lupa install git di image | Sudah diperbaiki — `docker compose build wa-gateway` lagi |
| WA tidak masuk tapi log `terkirim` | WA pengirim ke-disconnect | Cek `curl http://localhost:3000/status`, kalau false scan ulang |
| Mau ganti HP pengirim | Auth tersimpan di volume | `docker volume rm ethol-service_wa_auth && docker compose up -d` lalu scan ulang |

---

## 12. Struktur File (Tidak Perlu Diutak-atik)

```
.
├── docker-compose.yml      # menyalakan 2 kotak: wa-gateway + ethol-notifier
├── Dockerfile              # resep kotak Python
├── requirements.txt        # library Python
├── ethol-notification.py   # otak: login + cek ethol + kirim WA
├── wa-gateway/             # kotak WA
│   ├── index.js            # server WA (GET /qr, POST /send)
│   ├── package.json        # library Node
│   └── Dockerfile          # resep kotak WA
├── .env.example            # contoh .env (aman di-commit)
├── .env                    # punyamu (jangan di-commit!)
└── state.json              # catatan notif yang sudah dikirim (auto)
```

Kamu cuma perlu sentuh `.env`. Sisanya biarkan.

---

## 13. Keamanan

- `.env` sudah ada di `.gitignore`, tidak akan ter-upload ke GitHub.
- Jangan screenshot `.env` dan share.
- Jangan expose port `3000` ke internet publik tanpa password.
- Pakai nomor WA kedua untuk pengirim biar nomor utama aman dari banned.

Selamat, sekarang absensi tidak akan kelewat lagi!
