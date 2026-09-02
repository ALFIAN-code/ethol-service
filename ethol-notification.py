"""
Absensi Notifier — memantau notifikasi di ethol.pens.ac.id dan kirim WA via Baileys.

Data flow (sudah terverifikasi dari context user):
  1. GET https://ethol.pens.ac.id/api/auth/cas-redirect -> redirect ke CAS login HTML
  2. Parse HTML form #fm1, ambil lt + action (/cas/login;jsessionid=...?service=.../cas-callback)
  3. POST username/password/lt -> CAS set cookie, redirect ke cas-callback -> set cookie `token` (JWT, exp ~15 menit)
  4. GET https://ethol.pens.ac.id/api/notifikasi/mahasiswa?filterNotif=SEMUA + Cookie token -> JSON list

WA gateway: bisa pakai Evolution API (lama) ATAU Baileys gateway ringan (baru, recommended).
  - Jika WA_GATEWAY_URL diisi (default http://wa-gateway:3000), akan hit Baileys gateway.
  - Jika tidak, fallback ke Evolution API (EVOLUTION_BASE_URL).

Cara pakai:
  1. pip install requests beautifulsoup4 python-dotenv
  2. copy .env.example -> .env, isi NETID/PASSWORD/WA_TARGET
  3. python ethol-notification.py
"""

import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# load .env kalau ada (opsional, tidak wajib install python-dotenv)
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("absensi-notifier")

# ── KONFIGURASI — wajib via .env, tidak ada default kredensial di code ──────
NETID = os.getenv("NETID", "")
PASSWORD = os.getenv("PASSWORD", "")

# WA gateway pilihan:
# - Baileys gateway ringan (recommended untuk home server): http://wa-gateway:3000 atau http://localhost:3000
# - Evolution API (lama): http://localhost:8080
WA_GATEWAY_URL = os.getenv("WA_GATEWAY_URL", "http://wa-gateway:3000")  # kosongkan untuk pakai Evolution
WA_GATEWAY_API_KEY = os.getenv("WA_GATEWAY_API_KEY", "")  # isi jika gateway pakai auth

# Fallback Evolution (kalau WA_GATEWAY_URL kosong)
EVOLUTION_BASE_URL = os.getenv("EVOLUTION_BASE_URL", "http://localhost:8080")
EVOLUTION_INSTANCE = os.getenv("EVOLUTION_INSTANCE", "absensi-notifier")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")
WA_TARGET = os.getenv("WA_TARGET", "")

# Filter notifikasi: None = semua, atau set seperti {"PRESENSI-KULIAH"}
# Bisa diisi via env: KODE_YANG_DIPANTAU=PRESENSI-KULIAH,TUGAS-BARU
_kode_env = os.getenv("KODE_YANG_DIPANTAU", "")
if _kode_env:
    KODE_YANG_DIPANTAU = {k.strip() for k in _kode_env.split(",") if k.strip()}
else:
    KODE_YANG_DIPANTAU = None  # pantau SEMUA (sudah diverifikasi: PRESENSI, TUGAS, PENGUMUMAN ada semua)

BASE_URL = "https://ethol.pens.ac.id"
CAS_REDIRECT_URL = f"{BASE_URL}/api/auth/cas-redirect"
NOTIF_URL = f"{BASE_URL}/api/notifikasi/mahasiswa?filterNotif=SEMUA"

STATE_FILE = Path(__file__).parent / "state.json"
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "180"))

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"


class LoginFailed(Exception):
    pass


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    return s


def login(session: requests.Session) -> None:
    """Ikuti flow CAS lengkap sampai session dapat cookie `token` dari ethol."""
    # Step 1: GET cas-redirect (akan redirect ke https://login.pens.ac.id/cas/login?service=...)
    # requests akan follow redirect otomatis, jadi kita dapat HTML CAS langsung
    r = session.get(CAS_REDIRECT_URL, timeout=15, allow_redirects=True)
    r.raise_for_status()

    if 'id="fm1"' not in r.text or 'name="lt"' not in r.text:
        # Cek apakah sudah login (langsung dapat token tanpa form)
        if "token" in session.cookies.get_dict():
            log.info("Sudah ada cookie token, skip form login.")
            return
        raise LoginFailed(
            f"Response bukan halaman login CAS (url akhir: {r.url}). "
            "Cek apakah cas-redirect berubah atau CAS menampilkan captcha."
        )

    soup = BeautifulSoup(r.text, "html.parser")
    form = soup.find("form", {"id": "fm1"})
    if not form:
        raise LoginFailed("Form #fm1 tidak ditemukan di halaman CAS.")
    lt_input = form.find("input", {"name": "lt"})
    if not lt_input or not lt_input.get("value"):
        raise LoginFailed("Input lt tidak ditemukan.")
    lt_value = lt_input["value"]
    # action bisa relatif: /cas/login;jsessionid=xxx?service=...
    # urljoin dengan r.url (yang sudah di login.pens.ac.id) agar benar
    login_post_url = urljoin(r.url, form["action"])

    payload = {
        "username": NETID,
        "password": PASSWORD,
        "lt": lt_value,
        "_eventId": "submit",
        "submit": "LOGIN",
    }

    # Step 2: POST ke CAS login (akan redirect 302 ke /api/auth/cas-callback?ticket=ST-xxx)
    # lalu cas-callback akan set cookie `token` dan redirect lagi ke ethol
    r2 = session.post(login_post_url, data=payload, timeout=15, allow_redirects=True)
    r2.raise_for_status()

    cookies = session.cookies.get_dict()
    if "token" not in cookies:
        # Kadang token ada di header Set-Cookie tapi tidak terbaca karena domain
        # Cek apakah ada ticket error
        if "Invalid credentials" in r2.text or "Authentication Failure" in r2.text:
            raise LoginFailed("Login CAS gagal: NetID/password salah.")
        raise LoginFailed(
            f"Login CAS tidak menghasilkan cookie 'token'. Cookies: {list(cookies.keys())} | url akhir: {r2.url}"
        )

    log.info("Login CAS berhasil. Token exp ~15 menit.")


def fetch_notifikasi(session: requests.Session) -> list[dict]:
    r = session.get(NOTIF_URL, timeout=15)
    if r.status_code == 401:
        raise LoginFailed("Token expired (401 dari API notifikasi).")
    r.raise_for_status()
    data = r.json()
    # API return list langsung (verified via pasted-context-2.txt)
    if isinstance(data, dict) and "data" in data:
        return data["data"]
    return data


def load_seen_ids() -> set:
    try:
        if STATE_FILE.is_file():
            text = STATE_FILE.read_text().strip()
            if not text:
                return set()
            return set(json.loads(text))
    except Exception as e:
        log.warning("Gagal load state.json: %s", e)
    return set()


def save_seen_ids(ids: set) -> None:
    try:
        # jika state.json terlanjur jadi directory (bind mount bug), hapus dulu
        if STATE_FILE.exists() and not STATE_FILE.is_file():
            import shutil

            shutil.rmtree(STATE_FILE)
        STATE_FILE.write_text(json.dumps(list(ids)))
    except Exception as e:
        log.error("Gagal save state.json: %s", e)


def send_whatsapp(message: str) -> None:
    """Kirim WA via Baileys gateway (prioritas) lalu fallback ke Evolution."""
    # 1. Coba Baileys gateway ringan
    if WA_GATEWAY_URL:
        try:
            url = f"{WA_GATEWAY_URL.rstrip('/')}/send"
            headers = {"Content-Type": "application/json"}
            if WA_GATEWAY_API_KEY:
                headers["x-api-key"] = WA_GATEWAY_API_KEY
            resp = requests.post(
                url,
                headers=headers,
                json={"number": WA_TARGET, "text": message},
                timeout=15,
            )
            if resp.status_code in (200, 201):
                log.info("Notifikasi WA terkirim via Baileys gateway.")
                return
            else:
                log.warning("Baileys gateway gagal (%s): %s -> coba fallback Evolution", resp.status_code, resp.text)
        except Exception as e:
            log.warning("Gagal hit Baileys gateway (%s) -> coba fallback Evolution", e)

    # 2. Fallback Evolution API
    try:
        resp = requests.post(
            f"{EVOLUTION_BASE_URL.rstrip('/')}/message/sendText/{EVOLUTION_INSTANCE}",
            headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
            json={"number": WA_TARGET, "text": message},
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            log.error("Gagal kirim WA via Evolution (%s): %s", resp.status_code, resp.text)
        else:
            log.info("Notifikasi WA terkirim via Evolution API.")
    except Exception as e:
        log.error("Gagal kirim WA via Evolution: %s", e)


def format_message(notif: dict) -> str:
    # Format rapi, sesuai field yang ada di pasted-context-2.txt
    kode = notif.get("kodeNotifikasi", "-")
    ket = notif.get("keterangan", "-")
    waktu = notif.get("createdAtIndonesia", notif.get("waktuNotifikasi", "-"))
    url = notif.get("urlWeb", "")
    link = f"{BASE_URL}{url}" if url else "-"
    emoji = {"PRESENSI-KULIAH": "✅", "TUGAS-BARU": "📝", "PENGUMUMAN-BARU": "📢"}.get(kode, "🔔")
    return f"{emoji} *{kode}*\n{ket}\n\nWaktu: {waktu}\nLink: {link}"


def run_once(session: requests.Session, seen_ids: set) -> set:
    notifikasi = fetch_notifikasi(session)
    log.info("Fetch %d notifikasi dari API.", len(notifikasi))
    baru = [
        n
        for n in notifikasi
        if (KODE_YANG_DIPANTAU is None or n["kodeNotifikasi"] in KODE_YANG_DIPANTAU)
        and n["idNotifikasi"] not in seen_ids
    ]

    for n in sorted(baru, key=lambda x: x["createdAt"]):
        log.info("Notifikasi baru: %s | %s", n["kodeNotifikasi"], n["keterangan"])
        send_whatsapp(format_message(n))
        seen_ids.add(n["idNotifikasi"])

    if not baru:
        log.info("Tidak ada notifikasi baru.")

    return seen_ids


def main() -> None:
    if not NETID or not PASSWORD:
        log.error("NETID/PASSWORD kosong. Isi .env dulu (lihat .env.example).")
        raise SystemExit(1)
    if not WA_TARGET:
        log.error("WA_TARGET kosong. Isi nomor tujuan di .env (format 628xxx).")
        raise SystemExit(1)
    log.info("NetID: %s | WA target: %s | Gateway: %s", NETID, WA_TARGET, WA_GATEWAY_URL or EVOLUTION_BASE_URL)
    session = new_session()
    login(session)

    seen_ids = load_seen_ids()
    # baseline jika file belum ada, kosong, atau invalid -> jangan spam WA lama
    if not STATE_FILE.is_file() or len(seen_ids) == 0:
        log.info("Run pertama / state kosong: menyimpan baseline tanpa kirim WA (biar tidak spam notifikasi lama).")
        try:
            notifikasi = fetch_notifikasi(session)
            seen_ids = {n["idNotifikasi"] for n in notifikasi}
            save_seen_ids(seen_ids)
            log.info("Baseline %d notifikasi disimpan. Menunggu polling...", len(seen_ids))
            log.info(
                "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                "✅ Ethol notifier jalan! Cek QR WA di:\n"
                "   http://localhost:3000/qr\n"
                "   Jika belum scan, buka link di atas lalu scan\n"
                "   WhatsApp > Perangkat Tertaut > Tautkan\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
            )
        except Exception as e:
            log.error("Gagal baseline: %s", e)
            seen_ids = set()
    else:
        log.info("Load %d seen_ids dari state.json", len(seen_ids))
        log.info("Polling tiap %ds | WA gateway: %s | Cek QR: http://localhost:3000/qr", POLL_INTERVAL_SECONDS, WA_GATEWAY_URL)

    while True:
        try:
            seen_ids = run_once(session, seen_ids)
            save_seen_ids(seen_ids)
        except LoginFailed as e:
            log.warning("Sesi bermasalah (%s) -> login ulang.", e)
            session = new_session()
            try:
                login(session)
            except LoginFailed as le:
                log.error("Login ulang gagal: %s -> retry 30 detik", le)
                time.sleep(30)
            continue
        except Exception as e:
            log.error("Error polling: %s", e, exc_info=True)

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
