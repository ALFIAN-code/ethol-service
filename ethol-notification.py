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
from datetime import datetime, timedelta, timezone
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

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), format="%(asctime)s [%(levelname)s] %(message)s")
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
WA_TARGETS = [t.strip() for t in WA_TARGET.split(",") if t.strip()] if WA_TARGET else []

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

_DATA_DIR = Path(__file__).parent / "data"
# di Docker, /app/data adalah named volume; di lokal, pakai ./data atau ./state.json fallback
if Path("/app/data").exists() or os.getenv("WA_GATEWAY_URL", "").startswith("http://wa-gateway"):
    STATE_FILE = Path("/app/data/state.json")
    _DATA_DIR = Path("/app/data")
else:
    STATE_FILE = Path(__file__).parent / "state.json"
    # backward compat: kalau ada state.json lama di root, pakai itu
    if not STATE_FILE.exists() and (Path(__file__).parent / "data" / "state.json").exists():
        STATE_FILE = Path(__file__).parent / "data" / "state.json"

STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "180"))
SEND_FIRST_RUN_TEST = os.getenv("SEND_FIRST_RUN_TEST", "true").lower() in ("1", "true", "ya", "yes")
SEND_FIRST_RUN_TEST_COUNT = int(os.getenv("SEND_FIRST_RUN_TEST_COUNT", "3"))  # 2 atau 3 untuk testing multi

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
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        if STATE_FILE.exists() and not STATE_FILE.is_file():
            import shutil

            shutil.rmtree(STATE_FILE)
        STATE_FILE.write_text(json.dumps(list(ids)))
        # backward compat: hapus file lama di root jika pakai volume baru
        _old = Path(__file__).parent / "state.json"
        if STATE_FILE != _old and _old.is_file():
            try:
                _old.unlink()
            except Exception:
                pass
    except Exception as e:
        log.error("Gagal save state.json (%s): %s", STATE_FILE, e)


def send_whatsapp(message: str) -> None:
    """Kirim WA via Baileys gateway (prioritas) lalu fallback ke Evolution. Support multi target (personal + grup)."""
    targets = WA_TARGETS if WA_TARGETS else [WA_TARGET]
    for target in targets:
        sent = False
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
                    json={"number": target, "text": message},
                    timeout=15,
                )
                if resp.status_code in (200, 201):
                    log.info("Notifikasi WA terkirim via Baileys gateway ke %s.", target)
                    sent = True
                else:
                    log.warning("Baileys gateway gagal ke %s (%s): %s -> coba fallback Evolution", target, resp.status_code, resp.text)
            except Exception as e:
                log.warning("Gagal hit Baileys gateway ke %s (%s) -> coba fallback Evolution", target, e)
            if sent:
                continue

        # 2. Fallback Evolution API
        try:
            resp = requests.post(
                f"{EVOLUTION_BASE_URL.rstrip('/')}/message/sendText/{EVOLUTION_INSTANCE}",
                headers={"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"},
                json={"number": target, "text": message},
                timeout=15,
            )
            if resp.status_code not in (200, 201):
                log.error("Gagal kirim WA via Evolution ke %s (%s): %s", target, resp.status_code, resp.text)
            else:
                log.info("Notifikasi WA terkirim via Evolution API ke %s.", target)
        except Exception as e:
            log.error("Gagal kirim WA via Evolution ke %s: %s", target, e)


def format_message(notif: dict) -> str:
    # Format rapi, sesuai field yang ada di pasted-context-2.txt
    kode = notif.get("kodeNotifikasi", "-")
    ket = notif.get("keterangan", "-")
    waktu = notif.get("createdAtIndonesia", notif.get("waktuNotifikasi", "-"))
    url = notif.get("urlWeb", "")
    link = f"{BASE_URL}{url}" if url else "-"
    emoji = {"PRESENSI-KULIAH": "✅", "TUGAS-BARU": "📝", "PENGUMUMAN-BARU": "📢"}.get(kode, "🔔")
    return f"{emoji} *{kode}*\n{ket}\n\nWaktu: {waktu}\nLink: {link}"


def _today_wib() -> str:
    """Tanggal hari ini di WIB (Asia/Jakarta) format YYYY-MM-DD."""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Jakarta")).date().isoformat()
    except Exception:
        # fallback UTC+7
        return (datetime.now(timezone.utc) + timedelta(hours=7)).date().isoformat()


def _notif_date_wib(notif: dict) -> str | None:
    """Ambil tanggal notifikasi dalam WIB dari createdAt (UTC) atau createdAtIndonesia."""
    # coba parse createdAt dulu (ISO8601 UTC)
    created = notif.get("createdAt")
    if created:
        try:
            # handle "2026-09-02T03:42:05.000Z"
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            wib = dt.astimezone(timezone(timedelta(hours=7)))
            return wib.date().isoformat()
        except Exception:
            pass
    # fallback parse createdAtIndonesia: "Rabu, 02 September 2026 - 10:42"
    indo = notif.get("createdAtIndonesia", "")
    try:
        # ambil "02 September 2026"
        part = indo.split(",")[-1].split("-")[0].strip()  # "02 September 2026"
        dt = datetime.strptime(part, "%d %B %Y")
        return dt.date().isoformat()
    except Exception:
        return None


def _wait_wa_ready(timeout: int = 30) -> bool:
    """Tunggu wa-gateway sampai connected, polling /status."""
    if not WA_GATEWAY_URL:
        return True
    url = f"{WA_GATEWAY_URL.rstrip('/')}/status"
    for _ in range(timeout):
        try:
            r = requests.get(url, timeout=3)
            if r.ok and r.json().get("connected"):
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def _send_first_run_test(notifikasi: list[dict]) -> None:
    """Kirim 2-3 notifikasi terbaru hari ini sebagai test saat pertama jalan."""
    if not notifikasi or not SEND_FIRST_RUN_TEST:
        return
    today = _today_wib()
    today_notifs = [n for n in notifikasi if _notif_date_wib(n) == today]
    if KODE_YANG_DIPANTAU is not None:
        today_notifs = [n for n in today_notifs if n.get("kodeNotifikasi") in KODE_YANG_DIPANTAU]
    candidates = today_notifs if today_notifs else []
    if not candidates:
        log.info("Tidak ada notifikasi hari ini (%s) untuk test pertama — skip kirim test.", today)
        return
    # ambil N terbaru hari ini; kalau hari ini < N, lengkapi dari hari sebelumnya biar tetap test multi
    need = max(1, SEND_FIRST_RUN_TEST_COUNT)
    candidates = sorted(candidates, key=lambda x: x.get("createdAt", ""), reverse=True)
    if len(candidates) < need:
        # fallback: ambil terbaru overall untuk melengkapi
        all_sorted = sorted(notifikasi, key=lambda x: x.get("createdAt", ""), reverse=True)
        seen = {c.get("idNotifikasi") for c in candidates}
        for n in all_sorted:
            if n.get("idNotifikasi") not in seen:
                candidates.append(n)
                seen.add(n.get("idNotifikasi"))
            if len(candidates) >= need:
                break
        candidates = candidates[:need]
        log.info("Hari ini hanya %d notif, dilengkapi dari hari sebelumnya jadi %d untuk test multi", len(today_notifs), len(candidates))
    else:
        candidates = candidates[:need]
    # kirim dari yang terlama ke terbaru biar urutan chat natural (kebalikan reverse)
    candidates = list(reversed(candidates))
    log.info("🧪 Test pertama: kirim %d notifikasi terbaru hari ini (%s) sebagai verifikasi WA", len(candidates), today)
    _log_notif_data(f"TEST HARI INI ({len(candidates)} dikirim sebagai verifikasi)", candidates)
    if not _wait_wa_ready(30):
        log.warning("WA gateway belum connected setelah 30s — test akan dikirim tetap, mungkin gagal. Scan QR di http://localhost:3000/qr")
    for idx, n in enumerate(candidates, 1):
        test_msg = f"🧪 *TEST {idx}/{len(candidates)} - Notifikasi terbaru hari ini*\n\n" + format_message(n)
        log.info("→ Test %d/%d: %s | %s", idx, len(candidates), n.get("kodeNotifikasi"), n.get("keterangan", "")[:60])
        send_whatsapp(test_msg)
        if idx < len(candidates):
            time.sleep(1.5)  # jeda biar tidak rate-limit


def _log_notif_data(tag: str, notifs: list[dict]) -> None:
    """Print isi data notifikasi ke log biar kelihatan jelas."""
    if not notifs:
        log.info("[%s] kosong", tag)
        return
    log.info("[%s] %d data:", tag, len(notifs))
    for n in notifs:
        # pretty print 1 baris per notif
        log.info(
            "  • %s | %s | %s | %s | id=%s",
            n.get("kodeNotifikasi"),
            n.get("createdAtIndonesia", n.get("waktuNotifikasi")),
            n.get("keterangan", "")[:90],
            n.get("urlWeb"),
            n.get("idNotifikasi", "")[:8],
        )
    # dump JSON lengkap di level DEBUG (aktifkan via LOG_LEVEL=DEBUG)
    if log.isEnabledFor(logging.DEBUG):
        log.debug(json.dumps(notifs, indent=2, ensure_ascii=False))


def run_once(session: requests.Session, seen_ids: set) -> set:
    notifikasi = fetch_notifikasi(session)
    log.info("Fetch %d notifikasi dari API.", len(notifikasi))
    _log_notif_data("SEMUA", notifikasi)

    baru = [
        n
        for n in notifikasi
        if (KODE_YANG_DIPANTAU is None or n["kodeNotifikasi"] in KODE_YANG_DIPANTAU)
        and n["idNotifikasi"] not in seen_ids
    ]

    if baru:
        _log_notif_data("BARU (akan dikirim WA)", sorted(baru, key=lambda x: x["createdAt"]))
    for n in sorted(baru, key=lambda x: x["createdAt"]):
        msg = format_message(n)
        log.info("→ Kirim WA:\n%s", msg)
        send_whatsapp(msg)
        seen_ids.add(n["idNotifikasi"])

    if not baru:
        log.info("Tidak ada notifikasi baru.")

    return seen_ids


def main() -> None:
    if not NETID or not PASSWORD:
        log.error("NETID/PASSWORD kosong. Isi .env dulu (lihat .env.example).")
        raise SystemExit(1)
    if not WA_TARGETS:
        log.error("WA_TARGET kosong. Isi nomor tujuan di .env (628xxx atau 120363...@g.us untuk grup).")
        raise SystemExit(1)
    log.info("NetID: %s | WA target: %s | Gateway: %s", NETID, ", ".join(WA_TARGETS), WA_GATEWAY_URL or EVOLUTION_BASE_URL)
    session = new_session()
    login(session)

    seen_ids = load_seen_ids()
    # baseline jika file belum ada, kosong, atau invalid -> jangan spam WA lama
    if not STATE_FILE.is_file() or len(seen_ids) == 0:
        log.info("Run pertama / state kosong: menyimpan baseline + kirim 1 test hari ini untuk verifikasi.")
        try:
            notifikasi = fetch_notifikasi(session)
            _log_notif_data("BASELINE (disimpan)", notifikasi)
            seen_ids = {n["idNotifikasi"] for n in notifikasi}
            save_seen_ids(seen_ids)
            log.info("Baseline %d notifikasi disimpan.", len(seen_ids))
            # kirim 1 notifikasi terbaru hari ini sebagai test (biar ketahuan WA jalan)
            _send_first_run_test(notifikasi)
            log.info("Menunggu polling selanjutnya tiap %ds...", POLL_INTERVAL_SECONDS)
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
