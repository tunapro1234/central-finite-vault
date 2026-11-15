"""sources/zepp_cloud.py: bulut çekimleri → uyku/gün/egzersiz; son çekim kazanır, yedekler ve kestirme ile çakışma."""

import base64
import datetime as dt
import json
import sqlite3

import pytest

from cfv import db
from cfv.ingest import Batch
from cfv.sources import sabah, zepp_cloud

TR = dt.timezone(dt.timedelta(hours=3))


def ts(day, h, m=0):
    return int(dt.datetime(2026, 11, day, h, m, tzinfo=TR).timestamp())


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CFV_DATA", str(tmp_path / "data"))
    c = db.connect(db.init_db(tmp_path / "t.sqlite"))
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def gun(tarih, steps, slp=None):
    s = {"v": 6, "stp": {"ttl": steps, "dis": 6100, "cal": 310, "wk": 75, "rn": 12}, "goal": 8000}
    if slp:
        s["slp"] = slp
    return {"date_time": tarih, "summary": base64.b64encode(json.dumps(s).encode()).decode()}


SLP = {"st": ts(15, 23, 30), "ed": ts(16, 7), "dp": 60, "lt": 300, "wk": 10, "rhr": 57,
       "stage": [{"start": 1410, "stop": 1499, "mode": 4}, {"start": 1500, "stop": 1559, "mode": 5},
                 {"start": 1560, "stop": 1569, "mode": 7}, {"start": 1570, "stop": 1859, "mode": 8}]}


def cekim(root, ad, gunler, workouts=None, tamam=True):
    d = root / ad
    d.mkdir(parents=True)
    (d / "summary.json").write_text(json.dumps({"code": 1, "data": gunler}))
    if workouts is not None:
        (d / "workouts.json").write_text(json.dumps({"code": 1, "data": {"summary": workouts}}))
    if tamam:
        (d / ".tamam").touch()


def events(conn, kind, src="zepp-cloud"):
    return conn.execute("SELECT e.* FROM event e JOIN source s ON s.id=e.source_id WHERE s.kind=? AND e.kind=?",
                        (src, kind)).fetchall()


WO = [{"trackid": str(ts(15, 18)), "type": 1, "run_time": "2700", "end_time": str(ts(15, 18, 45))}]


def test_son_cekim_kazanir_ve_olaylar(conn, tmp_path):
    root = tmp_path / "data/raw/zepp-cloud"
    cekim(root, "2026-11-16T0630", [gun("2026-11-15", 5000), gun("2026-11-16", 120, SLP)], WO)
    cekim(root, "2026-11-16T2100", [gun("2026-11-15", 8412), gun("2026-11-16", 9100, SLP)], WO)
    cekim(root, "2026-11-16T2200", [gun("2026-11-16", 1)], tamam=False)          # yarım çekim okunmaz
    s = zepp_cloud.ingest(conn)
    assert (s["pulls"], s["partial_dirs"], s["sleeps"], s["days"], s["workouts"]) == (2, 1, 1, 2, 1)
    (sl,) = events(conn, "sleep")
    p = json.loads(sl["payload"])
    assert sl["external_id"] == "sleep/2026-11-16" and sl["local_date"] == "2026-11-16"
    assert (p["light_min"], p["deep_min"], p["awake_min"], p["rem_min"], p["asleep_min"]) == (90, 60, 10, 290, 440)
    assert p["via"] == "zepp-cloud"
    d = {r["local_date"]: json.loads(r["payload"]) for r in events(conn, "zepp_day")}
    assert d["2026-11-15"]["steps"] == 8412 and d["2026-11-16"]["steps"] == 9100
    assert d["2026-11-16"]["rest_hr"] == 57 and d["2026-11-15"]["step_goal"] == 8000
    (w,) = events(conn, "workout")
    assert w["title"] == "koşu 45 dk"


def test_evresiz_uyku_ozet_dakikalari(conn, tmp_path):
    root = tmp_path / "data/raw/zepp-cloud"
    cekim(root, "2026-11-16T0730", [gun("2026-11-16", 300, {k: v for k, v in SLP.items() if k != "stage"})])
    zepp_cloud.ingest(conn)
    (sl,) = events(conn, "sleep")
    p = json.loads(sl["payload"])
    assert (p["light_min"], p["deep_min"], p["awake_min"], p["asleep_min"]) == (300, 60, 10, 360)


def test_yedekler_kazanir(conn, tmp_path):
    root = tmp_path / "data/raw/zepp-cloud"
    cekim(root, "2026-11-16T2100", [gun("2026-11-15", 8412), gun("2026-11-16", 9100, SLP)], WO)
    z = Batch(conn, "zepp", tmp_path / "zepp", "test")
    start = dt.datetime(2026, 11, 15, 23, 30, tzinfo=TR)
    z.event("sleep/1", "sleep", start, end=start + dt.timedelta(hours=7), tz_offset_min=180, day="2026-11-16")
    z.event("day/2026-11-15", "zepp_day", start, tz_offset_min=180, day="2026-11-15")
    z.event("workout/9", "workout", dt.datetime(2026, 11, 15, 18, 3, tzinfo=TR), tz_offset_min=180)
    s = zepp_cloud.ingest(conn)
    assert (s["sleeps"], s["days"], s["workouts"], s["covered"]) == (0, 1, 0, 3)


def test_kestirme_buluta_yol_verir(conn, tmp_path):
    cekim(tmp_path / "data/raw/zepp-cloud", "2026-11-16T0830", [gun("2026-11-16", 300, SLP)])
    zepp_cloud.ingest(conn)
    r = tmp_path / "data/raw/sabah/2026-11"
    r.mkdir(parents=True)
    govde = {"v": 1, "gonderim_id": "00000000-0000-0000-0000-000000000001",
             "uyku": [{"baslangic": "2026-11-15T23:30:00+03:00", "bitis": "2026-11-16T07:00:00+03:00",
                       "deger": "core", "kaynak": "Zepp"}]}
    (r / "a.json").write_text(json.dumps({"alindi_at": "2026-11-16T05:00:00Z", "govde": json.dumps(govde)}))
    s = sabah.ingest(conn)
    assert (s["sleeps"], s["covered"]) == (0, 1)


def test_bozuk_kayit_sayilir(conn, tmp_path):
    root = tmp_path / "data/raw/zepp-cloud"
    cekim(root, "2026-11-16T0630", [{"date_time": "2026-11-16", "summary": "!!"}, {"x": 1}],
          [{"trackid": "abc"}, {"trackid": str(ts(15, 18)), "run_time": "0"}])
    (root / "2026-11-16T0730").mkdir()
    (root / "2026-11-16T0730/summary.json").write_text("{bozuk")
    (root / "2026-11-16T0730/.tamam").touch()
    s = zepp_cloud.ingest(conn)
    assert (s["bad_records"], s["bad_files"], s["sleeps"], s["days"], s["workouts"]) == (4, 1, 0, 0, 0)


def test_klasor_yoksa_atlanir(conn):
    with pytest.raises(FileNotFoundError):
        zepp_cloud.ingest(conn)


def test_gecmis_cekimi_gunluge_yenilir(conn, tmp_path):
    root = tmp_path / "data/raw/zepp-cloud"
    cekim(root, "2026-11-16T2100", [gun("2026-11-16", 9100)])
    cekim(root, "gecmis-2026-11-16", [gun("2026-11-10", 7000), gun("2026-11-16", 400)])
    zepp_cloud.ingest(conn)
    d = {r["local_date"]: json.loads(r["payload"])["steps"] for r in events(conn, "zepp_day")}
    assert d == {"2026-11-10": 7000, "2026-11-16": 9100}
