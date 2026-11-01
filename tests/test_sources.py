"""Kaynak ayrıştırıcı testleri — tamamen SENTETİK veri."""

import datetime as dt
from pathlib import Path

import pytest

from cfv import db, search, tree
from cfv.ingest import Batch
from cfv.sources import calendar_ics, notes, photos
from cfv.sources.notes_proto import Line

FIX = Path(__file__).parent / "fixtures"


def B(text):  # kalın satır
    return Line(text, bold=True)


def C(text, done=False):
    return Line(text, style="checkbox", checked=done)


@pytest.mark.parametrize("text,idx", [
    ("pzt", 0), ("pazartesi", 0), ("salı", 1), ("sali", 1), ("çarş", 2), ("cars", 2),
    ("perş", 3), ("pers", 3), ("cuma", 4), ("cuma :-(", 4), ("cmt", 5), ("cumartesi", 5),
    ("pazar", 6), ("pzr", 6), ("perşe", 3), ("paz", 6), ("pzt-salı-çaşr", 0), ("OKUL", None), ("PROJE", None),
    ("market listesi", None), ("perşembe", 3),
])
def test_parse_day(text, idx):
    assert notes.parse_day(text) == idx


def test_parse_todo_note_weeks_days_and_year_wrap():
    lines = [B("Ödevler 2024"), Line(""),
             B("2"), B("salı"), C("ödev A", True), C("ödev B"),
             B("1 yeni sistem"), C("hafta işi"), B("cuma"), B("OKUL"), C("dosya hazırla", True),
             B("52"), B("pzt"), Line("düz satır", style="numbered")]
    weeks = notes.parse_todo_note(lines, 2024)
    assert [(w.year, w.week) for w in weeks] == [(2024, 2), (2024, 1), (2023, 52)]
    assert weeks[1].label == "yeni sistem"
    a, b = weeks[0].todos
    assert (a.text, a.done, a.day, a.date) == ("ödev A", True, 1, dt.date(2024, 1, 9))
    assert b.done is False
    week_level, okul = weeks[1].todos
    assert week_level.day is None and week_level.date == dt.date(2024, 1, 1)
    assert (okul.day, okul.section) == (4, "OKUL")
    (last,) = weeks[2].todos
    assert last.date == dt.date(2023, 12, 25) and last.done is None


def test_parse_todo_note_ignores_out_of_order_numbers():
    lines = [B("40"), C("x"), B("6937"), B("45"), C("y")]
    (w,) = notes.parse_todo_note(lines, 2025)
    assert w.week == 40
    assert [t.text for t in w.todos] == ["x", "y"]  # '45' bölüm başlığı sayılır
    assert w.todos[1].section == "45"


@pytest.mark.parametrize("name,expected", [
    ("IMG_0787.JPG", ("img_0787", "jpg", "original", None)),
    ("IMG_0787-5337221.JPG", ("img_0787", "jpg", "original", 5337221)),
    ("IMG_3469-adjusted.HEIC", ("img_3469", "heic", "adjusted", None)),
    ("IMG_1062_HEVC.MOV", ("img_1062", "mov", "live", None)),
    ("0135ddf1-485b-4eaf-9afc-cbe954e2b7a7.JPG",
     ("0135ddf1-485b-4eaf-9afc-cbe954e2b7a7", "jpg", "original", None)),
])
def test_photo_parse_name(name, expected):
    assert photos.parse_name(name) == expected


def test_fts_query_turkish():
    # ı/İ → i katlanır; büyük-küçük harf ve diğer aksanları FTS (unicode61) halleder
    assert search.fts_query("Işık İzmir") == '"Işik"* "izmir"*'
    assert search.fts_query('"robot kol" ders') == '"robot kol" "ders"*'


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CFV_DATA", str(tmp_path / "data"))
    return db.connect(db.init_db(tmp_path / "t.sqlite"))


def test_calendar_ics_recurrence_and_all_day(conn):
    stats = calendar_ics.ingest(conn, [FIX / "sample.ics"])
    assert stats["events"] == 5  # 1 + 3 tekrar + 1 tüm gün
    rows = conn.execute("SELECT local_date, title, time_quality FROM event ORDER BY start_utc").fetchall()
    assert [r[0] for r in rows if r[1] == "Robotik dersi"] == ["2025-03-03", "2025-03-10", "2025-03-17"]
    assert ("2025-03-15", "Yarışma günü", "day") in rows
    hits = search.search(conn, "dis hekim")
    assert hits and hits[0]["title"] == "Diş hekimi"


def test_reingest_replaces_events(conn):
    calendar_ics.ingest(conn, [FIX / "sample.ics"])
    calendar_ics.ingest(conn, [FIX / "sample.ics"])
    assert conn.execute("SELECT count(*) FROM event").fetchone()[0] == 5


def test_whatsapp_title_not_indexed_and_tree(conn, tmp_path):
    b = Batch(conn, "test", tmp_path / "x", "test")
    t = dt.datetime(2025, 9, 30, 10, 0, tzinfo=dt.timezone.utc)
    b.event("m1", "whatsapp", t, title="Örnek Grup", body="👍", payload={"sender": "ben"})
    b.event("m2", "whatsapp", t, title="Örnek Grup", body="robot geldi", payload={"sender": "ben"})
    b.event("t1", "todo", t, title="robot topla", payload={"done": True, "order": 1})
    assert {h["body"] if "body" in h else h["snippet"] for h in search.search(conn, "robot")} \
        == {"robot geldi", "robot topla"}
    (w,) = search.weeks(conn, "robot")
    assert w["week"] == "2025-W40" and w["hits"] == 2

    out = tmp_path / "timeline"
    tree.build(conn, out)
    wdir = out / "2025" / "09-Eylül" / "W40 (29-30)"
    text = (wdir / "README.md").read_text()
    assert "Salı 30 Eylül 2025" in text and "[x] robot topla" in text
    assert "robot geldi" in (wdir / "whatsapp.md").read_text()
    tree.build(conn, out)  # tekrar üretilebilir
    (out / ".cfv-generated").unlink()
    with pytest.raises(RuntimeError):
        tree.build(conn, out)  # yabancı klasörün üstüne yazmaz


def test_turkish_search_end_to_end(conn, tmp_path):
    b = Batch(conn, "test", tmp_path / "y", "test")
    b.event("n1", "note", dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc), title="IŞIK sensörü", body="ölçüm")
    for q in ("ışık", "IŞIK", "isik", "Işık sens", "olcum"):
        assert len(search.search(conn, q)) == 1, q


def test_calendar_gcal_snapshot_and_diff(conn, tmp_path):
    import json
    from cfv import gcal_sync
    from cfv.sources import calendar_gcal
    ev = [
        {"CalendarID": "me@example.com", "id": "a1", "etag": "1", "summary": "Robot montaj",
         "start": {"dateTime": "2025-03-03T18:00:00+03:00"}, "end": {"dateTime": "2025-03-03T20:00:00+03:00"},
         "location": "Atölye", "attendees": [{"email": "me@example.com", "self": True}, {"email": "x@example.com"}]},
        {"CalendarID": "me@example.com", "id": "a2", "etag": "1", "summary": "Kamp",
         "start": {"date": "2025-03-15"}, "end": {"date": "2025-03-17"}},
        {"CalendarID": "me@example.com", "id": "a3", "status": "cancelled", "summary": "iptal",
         "start": {"date": "2025-03-16"}},
        {"CalendarID": "tr.turkish#holiday@group.v.calendar.google.com", "id": "h", "summary": "Bayram",
         "start": {"date": "2025-03-30"}, "end": {"date": "2025-03-31"}},
    ]
    p = tmp_path / "events.json"
    p.write_text(json.dumps({"fetched_at": "2025-03-01T00:00:00Z", "events": ev,
                             "calendars": [{"id": "me@example.com", "summary": "Ben"}]}))
    assert calendar_gcal.ingest(conn, p) == {"events": 2, "skipped": 2}
    rows = conn.execute("SELECT local_date, title, time_quality, tz_offset_min, start_utc FROM event "
                        "ORDER BY start_utc").fetchall()
    assert rows[0] == ("2025-03-03", "Robot montaj", "exact", 180, "2025-03-03T15:00:00Z")
    assert rows[1][:3] == ("2025-03-15", "Kamp", "day")
    assert search.search(conn, "atolye")[0]["title"] == "Robot montaj"

    new = [dict(ev[0], etag="2"), ev[3], {**ev[1], "id": "a4"}]
    d = gcal_sync.diff(ev, new)
    assert d["added"] == ["me@example.com/a4"]
    assert d["changed"] == ["me@example.com/a1"]
    assert [e["id"] for e in d["removed"]] == ["a2", "a3"]


def test_gmail_reader(conn, tmp_path):
    import json
    from cfv.sources import gmail
    acc = tmp_path / "me@example.com"
    acc.mkdir()
    msgs = [
        {"id": "g1", "thread": "t1", "time": "2025-04-01T09:00:00Z", "labels": ["INBOX", "CATEGORY_UPDATES"],
         "from": "Ayşe Örnek <ayse@example.com>", "to": "me@example.com", "cc": None,
         "subject": "Robot toplantısı", "snippet": "yarın atölyede"},
        {"id": "g2", "thread": "t1", "time": "2025-04-01T10:00:00Z", "labels": ["SENT"],
         "from": "Ben <me@example.com>", "to": "ayse@example.com", "cc": "Can <can@example.com>",
         "subject": "Re: Robot toplantısı", "snippet": "tamam"},
        {"id": "g3", "thread": "t2", "time": "2025-04-02T10:00:00Z", "labels": ["SPAM"],
         "from": "x@example.com", "to": "me@example.com", "subject": "kazandınız", "snippet": "..."},
    ]
    (acc / "messages.jsonl").write_text("".join(json.dumps(m) + "\n" for m in msgs))
    assert gmail.ingest(conn, tmp_path) == {"emails": 2, "skipped": 1}
    rows = conn.execute("SELECT external_id, kind, title, body, json_extract(payload,'$.direction') "
                        "FROM event ORDER BY start_utc").fetchall()
    assert [r[:3] for r in rows] == [("g1", "email", "Robot toplantısı"), ("g2", "email", "Re: Robot toplantısı")]
    assert rows[0][3] == "Kimden: Ayşe Örnek <ayse@example.com>\nKime: me@example.com\nyarın atölyede"
    assert [r[4] for r in rows] == ["in", "out"]
    links = conn.execute("SELECT e.external_id, pa.alias, p.display_name, ep.role FROM event_person ep "
                         "JOIN event e ON e.id=ep.event_id JOIN person p ON p.id=ep.person_id "
                         "JOIN person_alias pa ON pa.person_id=p.id ORDER BY 1, 2").fetchall()
    assert links == [("g1", "ayse@example.com", "Ayşe Örnek", "sender"),
                     ("g2", "ayse@example.com", "Ayşe Örnek", "recipient"),
                     ("g2", "can@example.com", "Can", "recipient")]
    assert search.search(conn, "atolyede")[0]["title"] == "Robot toplantısı"
