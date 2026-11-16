"""cfv-zepp: Zepp bulutundan günlük çekim (resmî OLMAYAN API; PLAN cfv/iphone §4A, Tuna onayı 2026-11-15).

  cfv-zepp giris      Tuna kendi terminalinde, bir kez: e-posta + şifre (ekranda görünmez). Yalnız tokenlar saklanır:
                      /root/.config/cfv-zepp/token.json (root 600). Şifre hiçbir yere yazılmaz.
  cfv-zepp cek        son N günün özet (uyku+adım), ayrıntı (dakikalık nabız) ve egzersiz listesi → yanıtlar AYNEN
                      data/work/zepp-tmp/<damga>/ → .tamam → tek rename → data/raw/zepp-cloud/<YYYY-MM-DD>T<HHMM>/
      --sabah         timer'ın sabah turları: bugün dün gecenin uykusu zaten geldiyse hemen çıkar.
      --gecmis T      bir kerelik geçmiş: T'den bugüne → data/raw/zepp-cloud/gecmis-<bugün>/ (okuyucu bunu her
                      günlük çekimden eski sayar)
  cfv-zepp durum

Giriş akışı huami-token'dan (MIT, Kirill Snezhko, 2025 Zepp girişi): şifreli kimlik → 303 yönlendirmede access
→ /v2/client/login → token_info{login_token, app_token, user_id}. Veri uçları topluluk belgelerinden
(band_data.json, sport/run/history.json): biçim değişirse okuyucu bozulmaz, ham yanıt yine saklanır, ilk gerçek
çekimden sonra okuyucu (sources/zepp_cloud.py) gerçek biçime göre denetlenir.
Giriş ucunda sıkı istek sınırı var: giriş yalnız elle yapılır, timer asla giriş denemez.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from cfv_iphone import ayar
from cfv_iphone.goruntu import TAMAM, borg_bekle, tasi_yerine_yazmadan

TOKEN = Path("/root/.config/cfv-zepp/token.json")
DURUM = "zepp-durum.json"
LOG = "zepp-bulut.log"
VER = "9.12.5"
UA = f"Zepp/{VER} (Pixel 4; Android 12; Density/2.75)"
_KEY, _IV = b"xeNtBVqzDc6tuNTh", b"MAAAYAAAAAAAAABg"
GIRIS_KOMUTU = "cd /srv/cfv/prod/src/iphone && uv run cfv-zepp giris"
URL_TOKENS = "https://api-user-us2.zepp.com/v2/registrations/tokens"
URL_LOGIN = "https://api-mifit-us2.zepp.com/v2/client/login"
# veri uçları için denenecek sunucular (hesabın bölgesine göre biri yanıt verir; çalışanı token dosyasına yazılır)
HOSTS = ("api-mifit.zepp.com", "api-mifit-de2.huami.com", "api-mifit-us2.zepp.com", "api-mifit.huami.com")
APP_HEADERS = {"appname": "com.huami.midong", "appplatform": "android_phone", "v": "2.0", "vn": VER,
               "cv": f"151689_{VER}", "user-agent": UA, "lang": "en_US", "country": "TR"}


class TokenHatasi(Exception):
    """Token yok, geçersiz ya da süresi dolmuş (çözüm: GIRIS_KOMUTU)."""


def raw() -> Path:
    return ayar.data() / "raw" / "zepp-cloud"


def tmp() -> Path:
    return ayar.work() / "zepp-tmp"


def log(satir: str) -> None:
    ayar.logs().mkdir(parents=True, exist_ok=True)
    with open(ayar.logs() / LOG, "a") as f:
        f.write(f"{dt.datetime.now(ayar.TZ).isoformat(timespec='seconds')} {satir}\n")


def durum_oku() -> dict:
    try:
        return json.loads((ayar.logs() / DURUM).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def durum_yaz(**k) -> None:
    d = durum_oku() | k
    ayar.logs().mkdir(parents=True, exist_ok=True)
    yol = ayar.logs() / DURUM
    yol.with_suffix(".yeni").write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n")
    os.replace(yol.with_suffix(".yeni"), yol)


def _istek(url: str, *, veri: bytes | None = None, basliklar: dict | None = None, yonlendirme: bool = True,
           zaman: int = 30) -> tuple[int, dict, bytes]:
    class Yok(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    acici = urllib.request.build_opener(*([] if yonlendirme else [Yok()]))
    req = urllib.request.Request(url, data=veri, headers=basliklar or {}, method="POST" if veri is not None else "GET")
    try:
        with acici.open(req, timeout=zaman) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


# ---------- giriş (yalnız elle) ----------

def giris_akisi(eposta: str, sifre: str, istek=_istek) -> dict:
    yuk = {"emailOrPhone": eposta, "state": "REDIRECTION", "client_id": "HuaMi", "password": sifre,
           "redirect_uri": "https://s3-us-west-2.amazonaws.com/hm-registration/successsignin.html",
           "region": "us-west-2", "token": ["access", "refresh"], "country_code": "US"}
    govde = AES.new(_KEY, AES.MODE_CBC, iv=_IV).encrypt(pad(urllib.parse.urlencode(yuk, doseq=True).encode(), 16))
    kod, bas, _ = istek(URL_TOKENS, veri=govde, yonlendirme=False, basliklar={
        "app_name": "com.huami.midong", "appname": "com.huami.midong", "cv": f"151689_{VER}", "v": "2.0",
        "appplatform": "android_phone", "vb": "202509151347", "vn": VER, "user-agent": UA, "x-hm-ekv": "1",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8"})
    yer = {k.lower(): v for k, v in bas.items()}.get("location", "")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(yer).query)
    access = (q.get("access") or [None])[0]
    if kod != 303 or not access:
        hata = (q.get("error") or [None])[0]
        raise SystemExit(f"giriş reddedildi (HTTP {kod}{', ' + hata if hata else ''}). E-posta/şifre doğru mu? "
                         "Apple/Google ile giriliyorsa önce Zepp hesabına şifre tanımla.")
    login = {"code": access, "device_id": str(uuid.uuid4()), "device_model": "android_phone", "app_version": VER,
             "dn": "api-mifit.zepp.com,api-user.zepp.com,api-mifit.zepp.com,api-watch.zepp.com,"
                   "app-analytics.zepp.com,auth.zepp.com,api-analytics.zepp.com",
             "third_name": "huami", "source": f"com.huami.watch.hmwatchmanager:{VER}:151689",
             "app_name": "com.huami.midong", "country_code": "US", "grant_type": "access_token",
             "allow_registration": "false", "lang": "en"}
    kod, _, govde = istek(URL_LOGIN, veri=urllib.parse.urlencode(login).encode(), basliklar={
        "app_name": "com.huami.webapp", "appname": "com.huami.webapp", "origin": "https://user.zepp.com",
        "referer": "https://user.zepp.com/", "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "accept": "application/json"})
    try:
        yanit = json.loads(govde)
    except json.JSONDecodeError:
        raise SystemExit(f"login yanıtı JSON değil (HTTP {kod})")
    ti = yanit.get("token_info") or {}
    if kod != 200 or not ti.get("app_token") or not ti.get("user_id"):
        raise SystemExit(f"login başarısız (HTTP {kod}); yanıt anahtarları: {sorted(yanit)}")
    hostlar = sorted({str(v) for d in (yanit.get("domains") or yanit.get("domain") or [])
                      for v in (d.values() if isinstance(d, dict) else [d]) if "mifit" in str(v)})
    return {"user_id": str(ti["user_id"]), "app_token": ti["app_token"], "login_token": ti.get("login_token"),
            "alindi_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "hostlar": hostlar, "yanit_anahtarlari": sorted(yanit), "token_info_anahtarlari": sorted(ti)}


def token_yaz(t: dict) -> None:
    TOKEN.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    yeni = TOKEN.with_suffix(".yeni")
    fd = os.open(yeni, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(t, f)
    os.replace(yeni, TOKEN)


def k_giris(a) -> int:
    print("Zepp hesabına giriş (resmî olmayan API). Şifre saklanmaz, yalnız oturum tokenı saklanır.")
    eposta = input("Zepp e-posta: ").strip()
    sifre = getpass.getpass("Zepp şifre: ")
    t = giris_akisi(eposta, sifre)
    del sifre
    token_yaz(t)
    log("giriş tamam (elle)")
    durum_yaz(token_hatasi=None, giris_at=t["alindi_at"])
    # yalnız biçim: değer yok (sunucu adları sır değil)
    print(f"tamam. Kayıt: {TOKEN} (600). Yanıt anahtarları: {t['yanit_anahtarlari']}; "
          f"sunucular: {t['hostlar'] or '-'}. Deneme çekimi: cfv-zepp cek")
    return 0


# ---------- çekim ----------

def _token() -> dict:
    try:
        return json.loads(TOKEN.read_text())
    except FileNotFoundError:
        raise TokenHatasi("token yok")


def _veri(host: str, yol: str, params: dict, t: dict, istek=_istek) -> tuple[int, bytes]:
    p = {**params, "r": str(uuid.uuid4()), "appid": str(secrets.randbits(64))}
    url = f"https://{host}{yol}?{urllib.parse.urlencode(p)}"
    kod, _, govde = istek(url, basliklar={**APP_HEADERS, "apptoken": t["app_token"],
                                          "x-request-id": str(uuid.uuid4()), "timezone": "Europe/Istanbul"})
    return kod, govde


def _yetki_hatasi(kod: int, govde: bytes) -> bool:
    if kod in (401, 403):
        return True
    try:
        j = json.loads(govde)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    return isinstance(j, dict) and str(j.get("code")) in ("0101", "401", "-1001") and "token" in json.dumps(j).lower()


def cek(gun: int = 3, istek=_istek, simdi: dt.datetime | None = None, gecmis: dt.date | None = None) -> Path:
    """Yanıtları aynen klasöre yazar; klasör tamamlanınca raw'a taşır. Yeni raw klasörünü döner."""
    t = _token()
    simdi = simdi or dt.datetime.now(ayar.TZ)
    bas = (gecmis or simdi.date() - dt.timedelta(days=gun - 1)).isoformat()
    son = simdi.date().isoformat()
    ozet_p = {"query_type": "summary", "device_type": "android_phone", "userid": t["user_id"],
              "from_date": bas, "to_date": son}
    hostlar = [t["host"]] if t.get("host") else [*t.get("hostlar", []), *HOSTS]
    host, kod, ozet = None, None, b""
    for h in dict.fromkeys(hostlar):
        try:
            kod, ozet = _veri(h, "/v1/data/band_data.json", ozet_p, t, istek)
        except OSError:
            continue
        if _yetki_hatasi(kod, ozet):
            raise TokenHatasi(f"{h}: HTTP {kod}, token geçersiz")
        if kod == 200:
            host = h
            break
    if host is None:
        raise RuntimeError(f"hiçbir veri sunucusu 200 vermedi (son HTTP {kod})")
    if t.get("host") != host:
        token_yaz({**t, "host": host})
    dosyalar = {"summary.json": ozet}
    for ad, yol, p in (("detail.json", "/v1/data/band_data.json", {**ozet_p, "query_type": "detail"}),
                       ("workouts.json", "/v1/sport/run/history.json", {"userid": t["user_id"]})):
        k, g = _veri(host, yol, p, t, istek)
        dosyalar[ad] = g if k == 200 else b""
        if k != 200:
            log(f"{ad}: HTTP {k}")
    damga = f"gecmis-{son}" if gecmis else simdi.astimezone(ayar.TZ).strftime("%Y-%m-%dT%H%M")
    ad = damga
    n = 2
    while (raw() / ad).exists() or (tmp() / ad).exists():
        ad, n = f"{damga}-{n}", n + 1
    yapim = tmp() / ad
    yapim.mkdir(parents=True)
    for f, b in dosyalar.items():
        if b:
            (yapim / f).write_bytes(b)
    (yapim / "meta.json").write_text(json.dumps({"cekildi_at": simdi.isoformat(timespec="seconds"), "host": host,
                                                 "from_date": bas, "to_date": son,
                                                 "dosyalar": sorted(f for f, b in dosyalar.items() if b)}))
    if not borg_bekle():
        raise RuntimeError(f"borg 2 sa bitmedi; {yapim} hazırlıkta kaldı")
    (yapim / TAMAM).touch()
    raw().mkdir(parents=True, exist_ok=True)
    tasi_yerine_yazmadan(yapim, raw() / ad)
    return raw() / ad


def dun_uykusu_var(klasor: Path, bugun: dt.date) -> bool:
    """summary.json'da bugünün (uyanılan günün) ya da dünün kaydında sıfır olmayan uyku var mı (biçim toleranslı)."""
    import base64
    try:
        j = json.loads((klasor / "summary.json").read_text())
    except (OSError, json.JSONDecodeError):
        return False
    for r in j.get("data") or []:
        if r.get("date_time") not in (bugun.isoformat(), (bugun - dt.timedelta(days=1)).isoformat()):
            continue
        try:
            s = json.loads(base64.b64decode(r.get("summary") or ""))
        except (ValueError, json.JSONDecodeError):
            continue
        slp = s.get("slp") or {}
        if (slp.get("dp") or 0) + (slp.get("lt") or 0) > 0 or slp.get("stage"):
            return True
    return False


def k_cek(a) -> int:
    simdi = dt.datetime.now(ayar.TZ)
    bugun = simdi.date().isoformat()
    d = durum_oku()
    if a.sabah and simdi.hour < 14 and d.get("uyku_geldi_gun") == bugun:   # akşam turu her zaman çeker
        return 0
    try:
        k = cek(a.gun, gecmis=dt.date.fromisoformat(a.gecmis) if a.gecmis else None)
    except TokenHatasi as e:
        log(f"token: {e}")
        if d.get("token_uyari_gun") != bugun:      # günde en çok bir uyarı
            _bp(f"Zepp bulutu: {e}; Tuna sunucu terminalinde girsin: {GIRIS_KOMUTU}")
            durum_yaz(token_uyari_gun=bugun)
        durum_yaz(token_hatasi=str(e), son_hata_at=dt.datetime.now(ayar.TZ).isoformat(timespec="seconds"))
        return 2
    except Exception as e:  # ağ/sunucu: sonraki saat yeniden dener
        log(f"çekim başarısız: {type(e).__name__}: {e}")
        durum_yaz(son_hata=f"{type(e).__name__}: {e}"[:300])
        return 1
    uyku = dun_uykusu_var(k, dt.date.fromisoformat(bugun))
    log(f"çekildi {k.name} uyku={'var' if uyku else 'yok'}")
    durum_yaz(son_cekim=k.name, token_hatasi=None, son_hata=None, **({"uyku_geldi_gun": bugun} if uyku else {}))
    return 0


def _bp(mesaj: str) -> None:
    import subprocess
    try:
        subprocess.run(["bp", "msg", "server-cfv", f"[cfv-zepp] {mesaj}"], timeout=60, capture_output=True)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass


def k_durum(a) -> int:
    d = durum_oku()
    d["token_dosyasi"] = TOKEN.exists()
    print(json.dumps(d, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cfv-zepp", description="Zepp bulutu günlük çekim (resmî olmayan API)")
    alt = p.add_subparsers(dest="komut", required=True)
    alt.add_parser("giris").set_defaults(f=k_giris)
    s = alt.add_parser("cek")
    s.add_argument("--gun", type=int, default=3)
    s.add_argument("--sabah", action="store_true")
    s.add_argument("--gecmis", metavar="YYYY-MM-DD", help="bir kerelik: bu tarihten bugüne → raw/zepp-cloud/gecmis-<bugün>/")
    s.set_defaults(f=k_cek)
    alt.add_parser("durum").set_defaults(f=k_durum)
    a = p.parse_args(argv)
    os.umask(0o077)
    return a.f(a)


if __name__ == "__main__":
    sys.exit(main())
