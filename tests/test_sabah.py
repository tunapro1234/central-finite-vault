"""sources/sabah.py: kestirme gönderimleri → uyku/tartı/gün/egzersiz; tekrar ayıklama ve haftalık yedeğe öncelik."""

import datetime as dt
import json
import sqlite3
import uuid

import pytest

from cfv import db
from cfv.ingest import Batch
from cfv.sources import sabah

TR = dt.timezone(dt.timedelta(hours=3))


def t(day, h, m=0):
    return dt.datetime(2026, 11, day, h, m, tzinfo=TR).isoformat()


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CFV_DATA", str(tmp_path / "data"))
    c = db.connect(db.init_db(tmp_path / "t.sqlite"))
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def post(root, govde: dict, alindi="2026-11-16T05:12:03Z", ham: str | None = None):
    gid = str(uuid.uuid4())
    d = root / alindi[:7]
    d.mkdir(parents=True, exist_ok=True)
    metin = ham if ham is not None else json.dumps({"v": 1, "gonderim_id": gid, **govde}, ensure_ascii=False)
    (d / f"{gid}.json").write_text(json.dumps({"alindi_at": alindi, "govde": metin}))


UYKU = [{"baslangic": t(15, 23, 30), "bitis": t(16, 1), "deger": "core", "kaynak": "Zepp"},
        {"baslangic": t(16, 1), "bitis": t(16, 2), "deger": "deep", "kaynak": "Zepp"},
        {"baslangic": t(16, 2), "bitis": t(16, 2, 10), "deger": "awake", "kaynak": "Zepp"},
        {"baslangic": t(16, 2, 10), "bitis": t(16, 7), "deger": "rem", "kaynak": "Zepp"}]
TARTI = [{"zaman": t(16, 7, 30), "kilo_kg": 72.4, "yag_yuzde": 18.1, "yagsiz_kutle_kg": 59.3, "bmi": 22.1,
          "kaynak": "Xiaomi Home"}]


def events(conn, kind):
    return conn.execute("SELECT e.* FROM event e JOIN source s ON s.id=e.source_id WHERE s.kind='sabah' AND e.kind=?",
                        (kind,)).fetchall()


def test_tekrarlar_ayiklanir_ve_olaylar(conn, tmp_path):
    root = tmp_path / "data/raw/sabah"
    for alindi in ("2026-11-16T05:12:03Z", "2026-11-16T08:15:00Z"):       # aynı örnekler iki gönderimde
        post(root, {"uyku": UYKU, "tarti": TARTI, "adim": [{"gun": "2026-11-15", "toplam": 8412}],
                    "nabiz": [{"gun": "2026-11-15", "dinlenik": 58, "min": 49, "ort": 71, "max": 148}],
                    "egzersiz": [{"baslangic": t(15, 18), "bitis": t(15, 18, 45), "tur": "koşu",
                                  "ort_nabiz": 150, "maks_nabiz": 175, "kaynak": "Zepp"}],
                    "ruya": {"durum": "gordum", "metin": "x"}}, alindi=alindi)
    s = sabah.ingest(conn)
    assert (s["posts"], s["sleeps"], s["measurements"], s["days"], s["workouts"]) == (2, 1, 1, 1, 1)
    (sl,) = events(conn, "sleep")
    p = json.loads(sl["payload"])
    assert sl["local_date"] == "2026-11-16" and sl["external_id"] == "sleep/2026-11-16"
    assert (p["light_min"], p["deep_min"], p["rem_min"], p["awake_min"]) == (90, 60, 290, 10)
    assert p["asleep_min"] == 440 and p["source"] == "zepp"
    (m,) = events(conn, "measurement")
    assert m["external_id"] == "measurement/mihome/2026-11-16T04:30"            # health.py ile aynı biçim
    assert json.loads(m["payload"])["weight_kg"] == 72.4
    (d,) = events(conn, "health_day")
    assert json.loads(d["payload"])["steps"] == 8412 and d["title"] == "8.412 adım"
    assert not events(conn, "dream")                                            # rüya cfv-ruya'nın


def test_haftalik_yedek_kazanir(conn, tmp_path):
    root = tmp_path / "data/raw/sabah"
    post(root, {"uyku": UYKU, "tarti": TARTI, "adim": [{"gun": "2026-11-15", "toplam": 8412}],
                "egzersiz": [{"baslangic": t(15, 18), "bitis": t(15, 18, 45), "kaynak": "Zepp"}]})
    h = Batch(conn, "health", tmp_path / "health", "test")
    start = dt.datetime(2026, 11, 15, 23, 30, tzinfo=TR)
    h.event("sleep/2026-11-16", "sleep", start, end=start + dt.timedelta(hours=7), tz_offset_min=180,
            day="2026-11-16")
    h.event("measurement/mihome/2026-11-16T04:30", "measurement", start, tz_offset_min=180)
    h.event("day/2026-11-15", "health_day", start, tz_offset_min=180, day="2026-11-15")
    h.event("workout/1", "workout", dt.datetime(2026, 11, 15, 18, 5, tzinfo=TR), tz_offset_min=180)
    s = sabah.ingest(conn)
    assert (s["sleeps"], s["measurements"], s["days"], s["workouts"], s["covered"]) == (0, 0, 0, 0, 4)


def test_bozuk_dosya_ve_ornek_sayilir(conn, tmp_path):
    root = tmp_path / "data/raw/sabah"
    post(root, {}, ham="{bozuk")
    post(root, {"v": 2}, ham=json.dumps({"v": 2}))
    post(root, {"uyku": [{"baslangic": "dün", "bitis": t(16, 7), "deger": "rem"},
                         {"baslangic": t(16, 7), "bitis": t(16, 6), "deger": "rem"},
                         {"baslangic": t(16, 1), "bitis": t(16, 2), "deger": "uydurma"}],
                "tarti": [{"zaman": t(16, 7), "kilo_kg": 900}]})
    s = sabah.ingest(conn)
    assert (s["posts"], s["bad_files"], s["bad_samples"], s["measurements"]) == (1, 2, 3, 0)


def test_klasor_yoksa_atlanir(conn):
    with pytest.raises(FileNotFoundError):
        sabah.ingest(conn)


def test_etiket():
    assert [sabah.label(x) for x in ("Zepp", "Xiaomi Home", "Mi Home", "Tuna'nın iPhone'u", None)] == \
        ["zepp", "mihome", "mihome", "iphone", "bilinmiyor"]


def test_yerel_bicim(conn, tmp_path):
    root = tmp_path / "data/raw/sabah"
    post(root, {"uyku": [{"baslangic": t(15, 23), "bitis": t(16, 6), "deger": "Çekirdek", "kaynak": "Zepp"}],
                "tarti": [{"zaman": t(16, 7), "kilo_kg": "72,4", "yag_yuzde": "18,1 %", "kaynak": "Xiaomi Home"}]})
    s = sabah.ingest(conn)
    assert (s["sleeps"], s["measurements"], s["bad_samples"]) == (1, 1, 0)
    (m,) = events(conn, "measurement")
    assert json.loads(m["payload"])["fat_pct"] == 18.1
