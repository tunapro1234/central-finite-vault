"""cfv-sabah ucu (src/sabah/cfv_sabah.py): kimlik, sınırlar, tek biçim ham kayıt, tekrar, üzerine yazmama."""

import datetime as dt
import http.client
import importlib.util
import json
import threading
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "cfv_sabah", Path(__file__).resolve().parents[1] / "src" / "sabah" / "cfv_sabah.py")
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)

TOKEN = b"t" * 43
AN = dt.datetime(2026, 11, 16, 5, 12, 3, tzinfo=dt.timezone.utc)


@pytest.fixture
def uc(tmp_path):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cs.isleyici(TOKEN, cs.Sayac(sinir=3), tmp_path, saat=lambda: AN))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], tmp_path
    srv.shutdown()


def _post(port, govde: bytes, yol="/sabah/v1", token=TOKEN, yontem="POST"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Content-Type": "application/json"}
    if token is not None:
        h["Authorization"] = "Bearer " + token.decode()
    c.request(yontem, yol, body=govde, headers=h)
    r = c.getresponse()
    return r.status, json.loads(r.read() or b"{}")


def _govde(gid=None, **ek):
    return json.dumps({"v": 1, "gonderim_id": gid or str(uuid.uuid4()), "tetik": "app:Ornek", **ek},
                      ensure_ascii=False).encode()


def test_yazar_ve_tek_bicim(uc):
    port, kok = uc
    gid = str(uuid.uuid4())
    g = _govde(gid, ruya={"durum": "gordum", "metin": "örnek rüya"})
    assert _post(port, g) == (200, {"ok": True})
    f = kok / "raw/sabah/2026-11" / f"{gid}.json"
    kayit = json.loads(f.read_text())
    assert kayit == {"alindi_at": "2026-11-16T05:12:03Z", "govde": g.decode()}   # gövde bayt bayt aynen
    assert f.stat().st_mode & 0o777 == 0o600
    assert not list((kok / "work/sabah-tmp").iterdir())


def test_tekrar_uzerine_yazmaz(uc):
    port, kok = uc
    gid = str(uuid.uuid4())
    assert _post(port, _govde(gid, n=1))[0] == 200
    assert _post(port, _govde(gid, n=2)) == (200, {"ok": True, "tekrar": True})
    assert '"n": 1' in json.loads((kok / "raw/sabah/2026-11" / f"{gid}.json").read_text())["govde"]


@pytest.mark.parametrize("token", [None, b"yanlis", b"t" * 42])
def test_token(uc, token):
    port, kok = uc
    assert _post(port, _govde(), token=token)[0] == 401
    assert not (kok / "raw").exists()


@pytest.mark.parametrize("govde", [b"{bozuk", b'{"gonderim_id": "x"}', b"[]",
                                   json.dumps({"v": 1, "gonderim_id": "../../etc/x"}).encode(),
                                   json.dumps({"v": 1, "gonderim_id": str(uuid.uuid4()).upper()}).encode(),
                                   b"\xff\xfe"])
def test_bozuk_400(uc, govde):
    port, kok = uc
    assert _post(port, govde)[0] == 400
    assert not (kok / "raw").exists()


def test_yol_ve_yontem_404(uc):
    port, _ = uc
    assert _post(port, _govde(), yol="/sabah/v2")[0] == 404
    assert _post(port, b"", yontem="GET")[0] == 404


def test_buyuk_413(uc):
    port, _ = uc
    assert _post(port, b"{" + b" " * cs.AZAMI_GOVDE + b"}")[0] == 413


def test_gunluk_sinir_429(uc):
    port, _ = uc
    assert [_post(port, _govde())[0] for _ in range(4)] == [200, 200, 200, 429]


def test_sayac_gun_degisince_sifirlanir():
    s = cs.Sayac(sinir=1)
    assert s.al("2026-11-16") and not s.al("2026-11-16") and s.al("2026-11-17")


def test_kaydet_kalip_disi_id_reddeder(tmp_path):
    with pytest.raises(ValueError):
        cs.kaydet(b"{}", "../x", AN, tmp_path)
