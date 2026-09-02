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

# jika HTTP_PROXY mengarah ke gluetun tapi gluetun tidak jalan (tanpa --profile vpn), fallback ke direct
if os.getenv("HTTP_PROXY", "").find("gluetun") != -1:
    try:
        import socket

        socket.gethostbyname("gluetun")
    except Exception:
        log.warning("HTTP_PROXY gluetun tidak terjangkau (jalan tanpa --profile vpn?) -> fallback direct tanpa proxy")
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ.pop(k, None)

# ── KONFIGURASI — wajib via .env, tidak ada default kredensial di code ──────
NETID = os.getenv("NETID", "")
PASSWORD = os.getenv("PASSWORD", "")
AUTO_PRESENSI = os.getenv("AUTO_PRESENSI", "false").lower() in ("1", "true", "ya", "yes")

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
WA_NOTIFY_ERROR = os.getenv("WA_NOTIFY_ERROR", "true").lower() in ("1", "true", "ya", "yes")
WA_NOTIFY_ERROR_COOLDOWN = int(os.getenv("WA_NOTIFY_ERROR_COOLDOWN", "3600"))  # detik, anti spam error
_last_error_wa = 0

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"


class LoginFailed(Exception):
    pass


def _request(session: requests.Session, method: str, url: str, **kwargs):
    """Wrapper request dengan fallback direct jika proxy gluetun gagal."""
    try:
        return getattr(session, method)(url, **kwargs)
    except requests.exceptions.ProxyError as e:
        if "gluetun" in str(e):
            log.warning("Proxy gluetun gagal (%s) -> retry direct tanpa proxy", e)
            kwargs["proxies"] = {"http": None, "https": None}
            return getattr(session, method)(url, **kwargs)
        raise


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    # hormati NO_PROXY untuk wa-gateway
    s.trust_env = True
    return s


def login(session: requests.Session) -> None:
    """Ikuti flow CAS lengkap sampai session dapat cookie `token` dari ethol."""
    # Step 1: GET cas-redirect (akan redirect ke https://login.pens.ac.id/cas/login?service=...)
    # requests akan follow redirect otomatis, jadi kita dapat HTML CAS langsung
    try:
        r = _request(session, "get", CAS_REDIRECT_URL, timeout=15, allow_redirects=True)
    except requests.exceptions.ProxyError as e:
        _notify_error_wa("Proxy gluetun error saat login CAS", str(e)[:300])
        raise
    if r.status_code in (403, 429):
        _notify_error_wa(f"Ethol block {r.status_code} saat CAS redirect", f"URL: {CAS_REDIRECT_URL}")
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
    r2 = _request(session, "post", login_post_url, data=payload, timeout=15, allow_redirects=True)
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
    try:
        r = _request(session, "get", NOTIF_URL, timeout=15)
    except requests.exceptions.ProxyError as e:
        _notify_error_wa("Proxy gluetun error saat fetch notifikasi", str(e)[:300])
        raise
    if r.status_code in (403, 429):
        _notify_error_wa(f"Ethol block {r.status_code} saat fetch notifikasi", f"URL: {NOTIF_URL} | Response: {r.text[:300]}")
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


def _should_notify_error() -> bool:
    """Rate limit notif error WA biar tidak spam."""
    global _last_error_wa
    if not WA_NOTIFY_ERROR:
        return False
    now = time.time()
    if now - _last_error_wa < WA_NOTIFY_ERROR_COOLDOWN:
        return False
    _last_error_wa = now
    return True


def _notify_error_wa(judul: str, detail: str) -> None:
    """Kirim notifikasi error ke WA (dipakai untuk 403/429, login gagal, proxy down, dll)."""
    if not _should_notify_error():
        log.info("Skip notif error WA (cooldown %ds): %s", WA_NOTIFY_ERROR_COOLDOWN, judul)
        return
    msg = f"⚠️ *ETHOL ERROR*\n{judul}\n\nDetail: {detail}\n\nWaktu: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\nCek: docker logs -f ethol_notifier"
    # WA error tetap coba direct tanpa proxy biar pasti masuk (jangan lewat gluetun yang mungkin down)
    # pakai send_whatsapp tapi force tanpa proxy sudah dihandle di _request, jadi panggil langsung
    send_whatsapp(msg)


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


def _decode_mahasiswa_id(session: requests.Session) -> int | None:
    """Ambil mahasiswa id dari JWT token cookie."""
    token = session.cookies.get("token", "")
    if not token or "." not in token:
        return None
    try:
        import base64

        payload = token.split(".")[1]
        # padding
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return int(data.get("nomor") or data.get("id") or 0) or None
    except Exception:
        return None


def _fetch_presensi_key(session: requests.Session, kuliah: int, jenis_schema: int) -> tuple[str | None, int | None]:
    """Ambil key & kuliah_asal untuk auto presensi. Return (key, kuliah_asal)."""
    # endpoint terverifikasi: GET /api/presensi/aktif-kuliah?kuliah=220827&jenis_schema=4 -> [{"kuliah":220827,"key":"brfSY04u8j","jenisSchema":4,"open":1}]
    candidates = [
        f"{BASE_URL}/api/presensi/aktif-kuliah?kuliah={kuliah}&jenis_schema={jenis_schema}",
        f"{BASE_URL}/api/presensi/aktif-kuliah?kuliah={kuliah}&jenisSchema={jenis_schema}",
        f"{BASE_URL}/api/presensi/mahasiswa?kuliah={kuliah}",
        f"{BASE_URL}/api/matakuliah/{kuliah}",
    ]
    for url in candidates:
        try:
            r = _request(session, "get", url, timeout=10)
            if r.ok:
                j = r.json()
                if isinstance(j, list):
                    for item in j:
                        if isinstance(item, dict) and "key" in item and str(item.get("kuliah")) == str(kuliah):
                            # cocokkan jenisSchema jika ada
                            if item.get("jenisSchema") is not None and int(item.get("jenisSchema")) != jenis_schema:
                                continue
                            # open 1 = presensi masih buka
                            if item.get("open") == 0:
                                log.warning("Presensi %s jenis %s sudah closed (open=0)", kuliah, jenis_schema)
                            return item.get("key"), item.get("kuliah_asal") or item.get("kuliahAsal")
                    # fallback: ambil item pertama yang ada key
                    for item in j:
                        if isinstance(item, dict) and "key" in item:
                            return item.get("key"), item.get("kuliah_asal")
                elif isinstance(j, dict):
                    if "key" in j:
                        return j.get("key"), j.get("kuliah_asal") or j.get("kuliahAsal")
                    for v in j.values():
                        if isinstance(v, dict) and "key" in v:
                            return v.get("key"), v.get("kuliah_asal")
                        if isinstance(v, list):
                            for item in v:
                                if isinstance(item, dict) and "key" in item:
                                    return item.get("key"), item.get("kuliah_asal")
        except Exception as e:
            log.debug("Gagal fetch key dari %s: %s", url, e)
    return None, None


def _check_riwayat_presensi(session: requests.Session, kuliah: int, jenis_schema: int, mahasiswa: int) -> bool:
    """Cek apakah sudah absen via GET /api/presensi/riwayat?kuliah=... True = sudah absen, skip auto."""
    try:
        url = f"{BASE_URL}/api/presensi/riwayat?kuliah={kuliah}&jenis_schema={jenis_schema}&nomor={mahasiswa}"
        r = _request(session, "get", url, timeout=10)
        if r.status_code == 304:
            # 304 = cache, anggap sudah pernah fetch, coba tanpa If-None-Match
            r = _request(session, "get", url, headers={"If-None-Match": ""}, timeout=10)
        if r.ok:
            j = r.json()
            if isinstance(j, list) and len(j) > 0:
                # cek tanggal hari ini
                today = _today_wib()
                for item in j:
                    tgl = item.get("tanggal", "")  # "02-09-2026 10:40:20"
                    try:
                        # parse "02-09-2026" -> YYYY-MM-DD
                        d = datetime.strptime(tgl.split()[0], "%d-%m-%Y").date().isoformat()
                        if d == today:
                            log.info("Riwayat: sudah absen hari ini %s untuk kuliah %s", today, kuliah)
                            return True
                    except Exception:
                        # kalau parse gagal, anggap sudah absen jika list tidak kosong
                        return True
                # jika ada riwayat tapi bukan hari ini, belum absen hari ini
                return False
    except Exception as e:
        log.debug("Gagal cek riwayat %s: %s", kuliah, e)
    return False


def _do_auto_presensi(session: requests.Session, notif: dict) -> tuple[bool, str]:
    """Eksekusi POST /api/presensi/mahasiswa. Return (sukses, pesan)."""
    if not AUTO_PRESENSI or notif.get("kodeNotifikasi") != "PRESENSI-KULIAH":
        return False, "skip (bukan presensi atau AUTO_PRESENSI off)"
    data_terkait = notif.get("dataTerkait", "")
    if not data_terkait or "-" not in str(data_terkait):
        return False, f"dataTerkait invalid: {data_terkait}"
    try:
        kuliah_s, jenis_s = str(data_terkait).split("-", 1)
        kuliah = int(kuliah_s)
        jenis_schema = int(jenis_s)
    except Exception:
        return False, f"parse dataTerkait gagal: {data_terkait}"

    mahasiswa = _decode_mahasiswa_id(session)
    if not mahasiswa:
        mahasiswa = 31988  # fallback dari token contoh, akan diisi via JWT
        log.warning("Gagal decode mahasiswa id dari token, pakai fallback %s", mahasiswa)

    # cek riwayat dulu biar tidak absen dobel
    if _check_riwayat_presensi(session, kuliah, jenis_schema, mahasiswa):
        return False, "sudah absen hari ini (cek /riwayat)"

    # ambil key via endpoint terverifikasi: /aktif-kuliah?kuliah=...&jenis_schema=...
    key, kuliah_asal = _fetch_presensi_key(session, kuliah, jenis_schema)
    if not key:
        log.warning("Key presensi untuk kuliah %s jenis %s tidak ditemukan di /aktif-kuliah (closed atau sudah absen)", kuliah, jenis_schema)
        return False, "key tidak ditemukan (sudah absen/closed, cek /riwayat — /aktif-kuliah return [])"

    # kuliah_asal tidak ada di /aktif-kuliah, coba ambil via matakuliah detail jika masih None
    if not kuliah_asal:
        try:
            r2 = _request(session, "get", f"{BASE_URL}/api/matakuliah/{kuliah}", timeout=10)
            if r2.ok:
                j2 = r2.json()
                # bisa langsung dict atau nested
                if isinstance(j2, dict):
                    kuliah_asal = j2.get("kuliah_asal") or j2.get("kuliahAsal") or j2.get("id_kuliah_asal")
                    if not kuliah_asal and "data" in j2 and isinstance(j2["data"], dict):
                        kuliah_asal = j2["data"].get("kuliah_asal")
        except Exception:
            pass
        kuliah_asal = kuliah_asal or kuliah  # fallback ke kuliah itu sendiri (server biasanya terima)

    payload = {
        "kuliah": kuliah,
        "jenis_schema": jenis_schema,
        "mahasiswa": mahasiswa,
        "key": key,
        "kuliah_asal": kuliah_asal,
    }

    log.info("🚀 Auto presensi: POST %s payload=%s", f"{BASE_URL}/api/presensi/mahasiswa", payload)
    try:
        r = _request(session, "post", f"{BASE_URL}/api/presensi/mahasiswa", json=payload, timeout=15)
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.ok and j.get("sukses"):
            return True, j.get("pesan", "Presensi berhasil disimpan")
        # kadang sukses true tapi status 200
        if r.ok:
            return True, j.get("pesan") or r.text[:200]
        return False, j.get("pesan") or r.text[:200]
    except Exception as e:
        return False, str(e)


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


def _send_first_run_test(session: requests.Session, notifikasi: list[dict]) -> None:
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
        auto_info = ""
        if AUTO_PRESENSI and n.get("kodeNotifikasi") == "PRESENSI-KULIAH":
            ok, pesan = _do_auto_presensi(session, n)
            auto_info = f"\n\n{'✅ Auto presensi: ' + pesan if ok else '❌ Auto presensi gagal: ' + pesan}"
        test_msg = f"🧪 *TEST {idx}/{len(candidates)} - Notifikasi terbaru hari ini*\n\n" + format_message(n) + auto_info
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
        # auto presensi jika PRESENSI-KULIAH
        auto_info = ""
        if AUTO_PRESENSI and n.get("kodeNotifikasi") == "PRESENSI-KULIAH":
            ok, pesan = _do_auto_presensi(session, n)
            auto_info = f"\n\n{'✅ Auto presensi: ' + pesan if ok else '❌ Auto presensi gagal: ' + pesan}"
            log.info("Auto presensi %s: %s", "sukses" if ok else "gagal", pesan)
        msg = format_message(n) + auto_info
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
            _send_first_run_test(session, notifikasi)
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
            _notify_error_wa("Login CAS gagal / token expired", str(e)[:400])
            session = new_session()
            try:
                login(session)
            except LoginFailed as le:
                log.error("Login ulang gagal: %s -> retry 30 detik", le)
                _notify_error_wa("Login ulang gagal 2x", str(le)[:400])
                time.sleep(30)
            continue
        except requests.exceptions.ProxyError as e:
            log.error("Proxy error (gluetun down?): %s", e)
            _notify_error_wa("Proxy gluetun down / DNS gagal", str(e)[:400] + "\nSolusi: docker compose up -d (tanpa proxy) atau --profile vpn")
            time.sleep(30)
            continue
        except Exception as e:
            msg = str(e)
            is_block = "403" in msg or "429" in msg or "block" in msg.lower()
            log.error("Error polling: %s", e, exc_info=True)
            if is_block or "Max retries" in msg:
                _notify_error_wa("Ethol polling error (mungkin IP ter-block)", msg[:500])
            else:
                _notify_error_wa("Ethol polling error umum", msg[:500])

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
