"""Sentetik: ad/son ek, önceki görüntü seçimi, borg bekleme ve yerine-yazmadan yayın."""

import datetime as dt
import os

import pytest

from cfv_iphone import ayar, goruntu


def test_ad_coz():
    assert goruntu.ad_coz("backup-enc-2026-11-15") == ("backup-enc", "2026-11-15", 1)
    assert goruntu.ad_coz("photos-db-2026-11-15-3") == ("photos-db", "2026-11-15", 3)
    assert goruntu.ad_coz("backup") is None


def test_ek_sec_tum_urunlerde_ortak(tmp_path):
    raw, haz = tmp_path / "raw", tmp_path / "haz"
    raw.mkdir(), haz.mkdir()
    assert goruntu.ek_sec("2026-11-15", [raw, haz]) == ""
    (raw / "extract-2026-11-15").mkdir()
    assert goruntu.ek_sec("2026-11-15", [raw, haz]) == "-2"
    (haz / "photos-db-2026-11-15-2").mkdir()      # başka ürün, hazırlıkta
    assert goruntu.ek_sec("2026-11-15", [raw, haz]) == "-3"


def test_onceki_tamam_ister_ve_sayisal_siralar(tmp_path):
    for ad, tamam in [("backup-enc-2026-11-08", True), ("backup-enc-2026-11-15-2", True),
                      ("backup-enc-2026-11-15-10", True), ("backup-enc-2026-11-22", False),
                      ("backup-enc-x", True)]:
        (tmp_path / ad).mkdir()
        if tamam:
            (tmp_path / ad / goruntu.TAMAM).touch()
    assert goruntu.onceki("backup-enc", tmp_path).name == "backup-enc-2026-11-15-10"
    assert goruntu.onceki("extract", tmp_path) is None


def test_kopyala_link_dest_hardlink(tmp_path):
    prev, src, dst = tmp_path / "prev", tmp_path / "src", tmp_path / "dst"
    for d in (prev, src):
        (d / "ab").mkdir(parents=True)
    (src / "ab" / "ayni").write_bytes(b"x" * 100)
    (src / "ab" / "yeni").write_bytes(b"y")
    os.link(src / "ab" / "ayni", prev / "ab" / "ayni")   # önceki görüntü: aynı içerik+zaman
    goruntu.kopyala(src, dst, prev)
    assert (dst / "ab" / "ayni").stat().st_ino == (prev / "ab" / "ayni").stat().st_ino
    assert (dst / "ab" / "yeni").stat().st_ino != (src / "ab" / "yeni").stat().st_ino   # çalışma kopyasıyla paylaşmaz
    with pytest.raises(FileExistsError):
        goruntu.kopyala(src, dst, prev)


def test_borg_bekle():
    durum = iter([True, True, False])
    uyku = []
    assert goruntu.borg_bekle(azami_sn=900, aralik_sn=300, kontrol=lambda: next(durum), uyu=uyku.append)
    assert uyku == [300, 300]
    uyku.clear()
    assert not goruntu.borg_bekle(azami_sn=600, aralik_sn=300, kontrol=lambda: True, uyu=uyku.append)
    assert sum(uyku) == 600


def test_yayinla_borg_bitmezse_dokunmaz(tmp_path):
    raw, d = tmp_path / "raw", tmp_path / "haz" / "extract-2026-11-15"
    raw.mkdir()
    d.mkdir(parents=True)
    (d / "f").write_text("1")
    assert goruntu.yayinla([d], raw, bekle=lambda: False) == []
    assert d.exists() and not (d / goruntu.TAMAM).exists() and not list(raw.iterdir())


def test_yayinla_tamam_ve_rename(tmp_path):
    raw, d = tmp_path / "raw", tmp_path / "haz" / "extract-2026-11-15"
    raw.mkdir()
    d.mkdir(parents=True)
    (d / "f").write_text("1")
    out = goruntu.yayinla([d], raw, bekle=lambda: True)
    assert out == [raw / "extract-2026-11-15"]
    assert (raw / "extract-2026-11-15" / goruntu.TAMAM).read_bytes() == b""
    assert not d.exists()


def test_yayinla_var_olanin_uzerine_yazmaz(tmp_path):
    raw, d = tmp_path / "raw", tmp_path / "haz" / "extract-2026-11-15"
    (raw / "extract-2026-11-15").mkdir(parents=True)          # boş dizin bile olsa
    d.mkdir(parents=True)
    with pytest.raises(FileExistsError):
        goruntu.yayinla([d], raw, bekle=lambda: True)
    with pytest.raises(OSError):
        goruntu.tasi_yerine_yazmadan(d, raw / "extract-2026-11-15")
    assert d.exists()


def test_bekleyenler_yapim_ve_bos_dizini_almaz(tmp_path):
    for ad in ("extract-2026-11-15", "backup-enc-2026-11-15", "yapim", "durduruldu", "photos-db-2026-11-15"):
        (tmp_path / ad).mkdir()
    for ad in ("extract-2026-11-15", "backup-enc-2026-11-15", "yapim"):
        (tmp_path / ad / "f").touch()
    assert [p.name for p in goruntu.bekleyenler(tmp_path)] == ["backup-enc-2026-11-15", "extract-2026-11-15"]


def test_hafta_basi():
    assert ayar.hafta_basi(dt.date(2026, 11, 15)) == dt.date(2026, 11, 15)   # Pazar
    assert ayar.hafta_basi(dt.date(2026, 11, 17)) == dt.date(2026, 11, 15)   # Salı → önceki Pazar
    assert ayar.hafta_basi(dt.date(2026, 11, 21)) == dt.date(2026, 11, 15)   # Cumartesi


def test_ikinci_kopya(tmp_path):
    import json

    from cfv_iphone.cli import ikinci_kopya
    f = tmp_path / "data.json"
    f.write_text(json.dumps({"ts": dt.datetime(2026, 11, 16, 3, 41, tzinfo=ayar.TZ).timestamp(), "exit": 0}))
    assert ikinci_kopya(None, f) == "yayın yok"
    assert ikinci_kopya("2026-11-15T21:10:00+03:00", f).startswith("tamam")
    assert ikinci_kopya("2026-11-16T05:00:00+03:00", f).startswith("bekliyor")
    f.write_text(json.dumps({"ts": dt.datetime(2026, 11, 16, 3, 41, tzinfo=ayar.TZ).timestamp(), "exit": 2}))
    assert ikinci_kopya("2026-11-15T21:10:00+03:00", f).startswith("bekliyor")
