"""Hafif tur (akis.hafif): parmak izi, değişmeyince iş yok, değişince yalnız o kaynaklar, DUR tekrar denenmez (sentetik)."""

import json
import os
import sqlite3

import pytest

from cfv import db

from akis import gunluk, hafif


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("CFV_DATA", str(tmp_path / "data"))
    d = tmp_path / "data"
    (d / "processed").mkdir(parents=True)
    (d / "logs").mkdir()
    monkeypatch.setattr(hafif.tazelik, "alert", lambda msg, **kw: True)
    monkeypatch.setattr(hafif, "_nightly_busy", lambda: False)
    monkeypatch.setattr(hafif, "_in_borg", lambda now: False)
    return d


@pytest.fixture
def sent(data, monkeypatch):
    out = []
    monkeypatch.setattr(hafif.tazelik, "alert", lambda msg, **kw: out.append(msg) or True)
    return out


def _live(d):
    conn = db.connect(d / "processed" / "cfv.sqlite")
    conn.executescript(db.schema_sql())
    conn.commit()
    conn.close()


def _args(**kw):
    return type("A", (), {"dry_run": False, "force": False, **kw})()


def test_fingerprint(data):
    fp = hafif.fingerprint()
    assert fp == {"sabah": None, "zepp-cloud": None, "tarti": None}
    zc = data / "raw" / "zepp-cloud"
    (zc / "2026-11-15T0700").mkdir(parents=True)
    (zc / "2026-11-15T0700" / "summary.json").write_text("{}")         # .tamam yok → sayılmaz
    (zc / "gecmis-2026-11-14").mkdir()
    (zc / "gecmis-2026-11-14" / "summary.json").write_text("{}")
    (zc / "gecmis-2026-11-14" / ".tamam").write_text("")
    s = data / "raw" / "sabah" / "2026-11"
    s.mkdir(parents=True)
    (s / ".abc.json.tmp").write_text("{}")                               # gizli/yarım: girmez
    fp = hafif.fingerprint()
    assert fp["zepp-cloud"]["dosya"] == 1 and fp["sabah"]["dosya"] == 0 and fp["tarti"] is None
    (zc / "2026-11-15T0700" / ".tamam").write_text("")                   # çekim tamamlandı → iz değişir
    fp2 = hafif.fingerprint()
    assert fp2["zepp-cloud"]["dosya"] == 2 and fp2["zepp-cloud"]["ozet"] != fp["zepp-cloud"]["ozet"]
    assert fp2["sabah"] == fp["sabah"]


def test_tur_ingests_only_on_change(data, sent):
    _live(data)
    live = data / "processed" / "cfv.sqlite"
    assert hafif.tur(_args()) == 0                                       # hiç klasör yok → iz ilk kez kaydedilmemiş
    st = hafif._load()
    assert st["islenen"] == {"sabah": None, "zepp-cloud": None, "tarti": None}
    assert not (data / "processed" / ".hafif").exists()                  # ingest edilecek ad yok: sahne kopyası yok
    assert not (data / "logs" / "gunluk.log").exists()                  # boş başlangıç log kirletmez
    (data / "raw" / "sabah" / "2026-11").mkdir(parents=True)
    assert hafif.tur(_args()) == 0
    st = hafif._load()
    assert st["islenen"]["sabah"] is not None and st["son_tur"]["ingest"] == ["sabah"]
    with sqlite3.connect(live) as c:                                     # yayın yapıldı: sabah kaynağı canlıda
        assert c.execute("SELECT count(*) FROM source WHERE kind='sabah'").fetchone()[0] == 1
    assert not (data / "processed" / ".hafif" / "cfv-hafif.sqlite").exists()
    log = (data / "logs" / "gunluk.log").read_text()
    assert "[hafif] değişen: sabah → ingest sabah" in log and "[sabah]" in log
    m1, n = live.stat().st_mtime_ns, len(log)
    assert hafif.tur(_args()) == 0                                       # aynı iz: sessiz, dokunmaz
    assert live.stat().st_mtime_ns == m1 and len((data / "logs" / "gunluk.log").read_text()) == n
    assert sent == []


def test_tur_stop_not_retried(data, sent, monkeypatch):
    _live(data)
    live = data / "processed" / "cfv.sqlite"
    (data / "raw" / "sabah" / "2026-11").mkdir(parents=True)
    monkeypatch.setattr(gunluk, "drops", lambda b, a, s: ["sabah: 10 → 0"])
    assert hafif.tur(_args()) == 2
    with sqlite3.connect(live) as c:                                     # canlıya yazılmadı
        assert c.execute("SELECT count(*) FROM source WHERE kind='sabah'").fetchone()[0] == 0
    assert len(sent) == 1 and "DUR" in sent[0]
    assert (data / "processed" / ".hafif" / "cfv-hafif.sqlite").exists()  # incelemek için kalır
    assert hafif.tur(_args()) == 0 and len(sent) == 1              # aynı ham veri: tekrar denenmez
    (data / "raw" / "sabah" / "2026-11" / "x.json").write_text("{}")    # yeni veri: yeniden dener, eski kopya kenara
    assert hafif.tur(_args()) == 2
    assert len(sent) == 1                                           # aynı uyarı metni 24 sa içinde tekrar gitmez
    kept = list((data / "processed" / ".hafif").glob("cfv-hafif.dur-*.sqlite"))
    assert len(kept) == 1


def test_tarti_only_no_reader(data):
    _live(data)
    (data / "raw" / "tarti").mkdir(parents=True)
    (data / "raw" / "tarti" / "olcum.json").write_text("{}")
    assert hafif.tur(_args()) == 0
    st = hafif._load()
    assert st["islenen"]["tarti"]["dosya"] == 1 and "son_tur" not in st
    assert "okuyucu yok, ATLANDI: tarti" in (data / "logs" / "gunluk.log").read_text()


def test_nightly_marks_fingerprint(data):
    (data / "raw" / "sabah").mkdir(parents=True)
    fp = hafif.fingerprint()
    hafif.mark_done(fp, "gece")
    assert hafif.tur(_args()) == 0 and "son_tur" not in hafif._load()
    assert json.loads((data / "logs" / "akis-hafif.json").read_text())["islenen_tur"] == "gece"
    assert os.environ["CFV_DATA"] == str(data)
