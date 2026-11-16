"""cfv-zepp: sentetik — giriş akışı (sahte sunucu, gerçek AES), çekim klasörü, token hatası, uyku tespiti."""

import base64
import datetime as dt
import json
import urllib.parse

import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from cfv_iphone import ayar, zepp

SAHTE_TOKEN = "app-token-sentetik"


@pytest.fixture(autouse=True)
def ortam(tmp_path, monkeypatch):
    monkeypatch.setenv("CFV_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(zepp, "TOKEN", tmp_path / "cfg/token.json")
    monkeypatch.setattr(zepp, "borg_bekle", lambda: True)


def test_giris_akisi():
    gorulen = {}

    def istek(url, *, veri=None, basliklar=None, yonlendirme=True, zaman=30):
        if url == zepp.URL_TOKENS:
            assert yonlendirme is False
            duz = unpad(AES.new(zepp._KEY, AES.MODE_CBC, iv=zepp._IV).decrypt(veri), 16).decode()
            gorulen["yuk"] = urllib.parse.parse_qs(duz)
            return 303, {"Location": "https://x.example.com/s.html?access=AC&refresh=RF&country_code=US"}, b""
        assert url == zepp.URL_LOGIN
        gorulen["login"] = urllib.parse.parse_qs(veri.decode())
        return 200, {}, json.dumps({"token_info": {"login_token": "L", "app_token": SAHTE_TOKEN, "user_id": 42},
                                    "domains": [{"host": "api-mifit-de2.huami.com"}]}).encode()

    t = zepp.giris_akisi("kisi@example.com", "sentetik-sifre", istek)
    assert gorulen["yuk"]["emailOrPhone"] == ["kisi@example.com"] and gorulen["yuk"]["token"] == ["access", "refresh"]
    assert gorulen["login"]["code"] == ["AC"]
    assert t["user_id"] == "42" and t["app_token"] == SAHTE_TOKEN and t["hostlar"] == ["api-mifit-de2.huami.com"]
    assert "sentetik-sifre" not in json.dumps(t)


def test_giris_reddi():
    with pytest.raises(SystemExit, match="HTTP 401"):
        zepp.giris_akisi("a@example.com", "x", lambda *a, **k: (401, {}, b""))


def ozet(gun, slp=None):
    s = {"stp": {"ttl": 100}, **({"slp": slp} if slp else {})}
    return {"date_time": gun, "summary": base64.b64encode(json.dumps(s).encode()).decode()}


def test_cek_klasor_ve_host(tmp_path):
    zepp.token_yaz({"user_id": "42", "app_token": SAHTE_TOKEN, "hostlar": []})
    cagrilar = []

    def istek(url, *, veri=None, basliklar=None, yonlendirme=True, zaman=30):
        cagrilar.append(url)
        assert basliklar["apptoken"] == SAHTE_TOKEN
        host = urllib.parse.urlparse(url).hostname
        if host == zepp.HOSTS[0]:
            raise OSError("bağlanamadı")
        if "band_data" in url:
            return 200, {}, json.dumps({"code": 1, "data": [ozet("2026-11-16", {"dp": 60, "lt": 300})]}).encode()
        return 500, {}, b"x"

    simdi = dt.datetime(2026, 11, 16, 7, 30, tzinfo=ayar.TZ)
    k = zepp.cek(3, istek, simdi)
    assert k == tmp_path / "data/raw/zepp-cloud/2026-11-16T0730"
    assert sorted(p.name for p in k.iterdir()) == [".tamam", "detail.json", "meta.json", "summary.json"]
    assert json.loads((k / "meta.json").read_text())["from_date"] == "2026-11-14"
    assert json.loads(zepp.TOKEN.read_text())["host"] == zepp.HOSTS[1]
    assert zepp.TOKEN.stat().st_mode & 0o777 == 0o600
    assert zepp.dun_uykusu_var(k, simdi.date())
    k2 = zepp.cek(3, istek, simdi)                                  # aynı dakika: üzerine yazmaz, ek alır
    assert k2.name == "2026-11-16T0730-2"
    assert all(zepp.HOSTS[0] not in u for u in cagrilar[len(cagrilar) // 2:])   # öğrenilen host kullanılır


def test_token_hatasi_ve_yok(tmp_path):
    with pytest.raises(zepp.TokenHatasi):
        zepp.cek(3, lambda *a, **k: (200, {}, b"{}"))
    zepp.token_yaz({"user_id": "42", "app_token": SAHTE_TOKEN, "host": "h.example.com"})
    with pytest.raises(zepp.TokenHatasi):
        zepp.cek(3, lambda *a, **k: (401, {}, b""))
    assert not (tmp_path / "data/raw/zepp-cloud").exists()


def test_uyku_yoksa_false(tmp_path):
    (tmp_path / "s.json").write_text(json.dumps({"data": [ozet("2026-11-16"), ozet("2026-11-10", {"dp": 5})]}))
    assert not zepp.dun_uykusu_var(tmp_path / "x", dt.date(2026, 11, 16))
    (tmp_path / "k").mkdir()
    (tmp_path / "s.json").rename(tmp_path / "k/summary.json")
    assert not zepp.dun_uykusu_var(tmp_path / "k", dt.date(2026, 11, 16))


def test_gecmis_klasoru(tmp_path):
    zepp.token_yaz({"user_id": "42", "app_token": SAHTE_TOKEN, "host": "h.example.com"})
    gorulen = []

    def istek(url, **k):
        gorulen.append(urllib.parse.parse_qs(urllib.parse.urlparse(url).query))
        return 200, {}, b'{"code": 1, "data": []}'

    k = zepp.cek(3, istek, dt.datetime(2026, 11, 16, 9, 0, tzinfo=ayar.TZ), gecmis=dt.date(2019, 1, 1))
    assert k.name == "gecmis-2026-11-16" and gorulen[0]["from_date"] == ["2019-01-01"]


def test_token_yok_uyarisi_tam_komut_gunde_bir(monkeypatch):
    gonderilen = []
    monkeypatch.setattr(zepp, "_bp", gonderilen.append)
    a = zepp.main.__globals__["argparse"].Namespace(gun=3, sabah=False, gecmis=None)
    assert zepp.k_cek(a) == 2 and zepp.k_cek(a) == 2
    (m,) = gonderilen                                               # günde en çok bir uyarı
    assert m == f"Zepp bulutu: token yok; Tuna sunucu terminalinde girsin: {zepp.GIRIS_KOMUTU}"
    assert zepp.GIRIS_KOMUTU == "cd /srv/cfv/prod/src/iphone && uv run cfv-zepp giris"
