"""cfv-sabah: sabah kestirmesinin tek giriş ucu (PLAN cfv/iphone §9). Yalnız Python stdlib, 127.0.0.1:3070.

  POST /sabah/v1   Authorization: Bearer <token>, JSON gövde (v=1, gonderim_id küçük harf UUID)
                   → data/raw/sabah/<UTC YYYY-MM>/<gonderim_id>.json = {"alindi_at", "govde"} (gövde bayt bayt aynen)
  Gerisi 404. Yanıtlar: 200 {"ok":true[, "tekrar":true]}, 400 bozuk, 401 token, 413 büyük, 429 günlük sınır.

Atomik ve yalnız ekleyen yazım: tmp (data/work/sabah-tmp, aynı disk) → fsync → link() hedefe (hedef varsa başarısız,
yani üzerine yazma imkânsız) → tmp adı kaldırılır. IP, kimlik, cihaz bilgisi kaydedilmez; erişim logu yok.

Komutlar:  serve        (cfv-sabah.service)
           token-uret   (Tuna, kendi terminalinde: yeni token üretir, dosyaya yazar, ekrana BİR KEZ basar)
"""

from __future__ import annotations

import datetime as dt
import hmac
import json
import os
import re
import secrets
import base64
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

YOL = "/sabah/v1"
AZAMI_GOVDE = 256 * 1024
GUNLUK_SINIR = 200
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TOKEN_DOSYASI = Path("/root/.config/cfv-sabah/token")


def data() -> Path:
    return Path(os.environ.get("CFV_DATA", "/mnt/depo1/cfv/data"))


def token_oku() -> bytes:
    cred = os.environ.get("CREDENTIALS_DIRECTORY")
    yol = Path(cred) / "token" if cred else TOKEN_DOSYASI
    t = yol.read_bytes().strip()
    if len(t) < 32:
        raise SystemExit("token geçersiz/kısa")
    return t


class Sayac:
    """Günlük (UTC) kimliği doğrulanmış POST sayısı. Bellekte; yeniden başlatma sıfırlar (kaba koruma yeter)."""

    def __init__(self, sinir: int = GUNLUK_SINIR):
        self.sinir, self.gun, self.n = sinir, None, 0
        self.kilit = threading.Lock()

    def al(self, bugun: str) -> bool:
        with self.kilit:
            if bugun != self.gun:
                self.gun, self.n = bugun, 0
            if self.n >= self.sinir:
                return False
            self.n += 1
            return True


def kaydet(govde: bytes, gonderim_id: str, simdi: dt.datetime, kok: Path | None = None) -> bool:
    """True = yeni yazıldı, False = aynı gonderim_id bu ay zaten var (tekrar). Hiçbir dosyanın üzerine yazmaz."""
    kok = kok or data()
    if not UUID.match(gonderim_id):
        raise ValueError("gonderim_id")
    ay = kok / "raw" / "sabah" / simdi.strftime("%Y-%m")
    hedef = ay / f"{gonderim_id}.json"
    if hedef.exists():
        return False
    tmpdir = kok / "work" / "sabah-tmp"
    tmpdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    kayit = json.dumps({"alindi_at": simdi.isoformat(timespec="seconds").replace("+00:00", "Z"),
                        "govde": govde.decode("utf-8")}, ensure_ascii=False).encode("utf-8")
    tmp = tmpdir / f"{gonderim_id}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(kayit)
            f.flush()
            os.fsync(f.fileno())
        ay.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.link(tmp, hedef)
        except FileExistsError:
            return False
        dfd = os.open(ay, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        return True
    finally:
        tmp.unlink(missing_ok=True)


def dogrula(govde: bytes) -> str:
    """gonderim_id döner; geçersizse ValueError."""
    try:
        d = json.loads(govde.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError("json") from e
    if not isinstance(d, dict) or d.get("v") != 1:
        raise ValueError("v")
    gid = d.get("gonderim_id")
    if not isinstance(gid, str) or not UUID.match(gid):
        raise ValueError("gonderim_id")
    return gid


def isleyici(token: bytes, sayac: Sayac, kok: Path | None = None, saat=lambda: dt.datetime.now(dt.timezone.utc)):
    class H(BaseHTTPRequestHandler):
        server_version = "cfv-sabah"
        sys_version = ""
        timeout = 15

        def log_message(self, *a):   # erişim logu yok (IP/yol yazılmaz)
            pass

        def _yanit(self, kod: int, govde: dict) -> None:
            b = json.dumps(govde).encode()
            self.send_response(kod)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            self._yanit(404, {"ok": False})

        do_PUT = do_DELETE = do_HEAD = do_PATCH = do_OPTIONS = do_GET

        def do_POST(self):
            if self.path != YOL:
                return self._yanit(404, {"ok": False})
            yetki = self.headers.get("Authorization", "")
            verilen = yetki[7:].strip().encode() if yetki.startswith("Bearer ") else b""
            if not hmac.compare_digest(verilen, token):
                return self._yanit(401, {"ok": False})
            try:
                uzunluk = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return self._yanit(411, {"ok": False})
            if uzunluk > AZAMI_GOVDE:
                return self._yanit(413, {"ok": False})
            simdi = saat()
            if not sayac.al(simdi.date().isoformat()):
                return self._yanit(429, {"ok": False})
            govde = self.rfile.read(uzunluk)
            if len(govde) != uzunluk:
                return self._yanit(400, {"ok": False})
            try:
                gid = dogrula(govde)
            except ValueError as e:
                return self._yanit(400, {"ok": False, "hata": str(e)})
            try:
                yeni = kaydet(govde, gid, simdi, kok)
            except OSError as e:
                print(f"yazım hatası: {type(e).__name__}", file=sys.stderr, flush=True)
                return self._yanit(500, {"ok": False})
            self._yanit(200, {"ok": True} if yeni else {"ok": True, "tekrar": True})

    return H


def serve(host: str = "127.0.0.1", port: int = 3070) -> None:
    srv = ThreadingHTTPServer((host, port), isleyici(token_oku(), Sayac()))
    srv.daemon_threads = True
    print(f"cfv-sabah {host}:{port} dinliyor", flush=True)
    srv.serve_forever()


def token_uret() -> None:
    if os.geteuid() != 0:
        raise SystemExit("root olarak çalıştır")
    t = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    TOKEN_DOSYASI.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = TOKEN_DOSYASI.with_suffix(".yeni")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(t + "\n")
    os.replace(tmp, TOKEN_DOSYASI)
    aktif = subprocess.run(["systemctl", "is-active", "--quiet", "cfv-sabah.service"]).returncode == 0
    if aktif:
        subprocess.run(["systemctl", "restart", "cfv-sabah.service"], check=True)
    print("Yeni token (kestirmedeki Authorization alanına 'Bearer ' + bu değer; bir daha gösterilmez):\n")
    print(t)
    print("\nEski token artık geçersiz." if aktif else "\n(servis çalışmıyordu; başlayınca bu token geçerli)")


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    komut = argv[0] if argv else ""
    if komut == "serve":
        serve(port=int(os.environ.get("CFV_SABAH_PORT", "3070")))
    elif komut == "token-uret":
        token_uret()
    else:
        raise SystemExit("kullanım: cfv_sabah.py serve | token-uret")


if __name__ == "__main__":
    main()
