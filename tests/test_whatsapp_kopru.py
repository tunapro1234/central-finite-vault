"""WhatsApp köprüsü okuyucusu: artımlı toplama (raw yalnız eklenir) + iPhone tekilleştirme + süzgeçler (sentetik)."""

import datetime as dt
import json
import os

import pytest

from cfv import db
from cfv.ingest import Batch
from cfv.sources import whatsapp_kopru as wk

UTC = dt.timezone.utc


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("CFV_DATA", str(tmp_path / "data"))
    return tmp_path / "data"


def rec(i, ts, *, chat="905550000001@lid", ctype="dm", name="Ayşe", from_me=False, text="selam", sender=None,
        **kw):
    r = {"ts": ts, "chatJid": chat, "chatType": ctype, "chatName": name, "senderJid": chat,
         "senderName": sender or ("Tuna" if from_me else name), "fromMe": from_me, "text": text, "id": i}
    r.update(kw)
    return json.dumps(r, ensure_ascii=False)


def test_collect_incremental_append_only(data, tmp_path):
    store = tmp_path / "wa" / "messages.jsonl"
    store.parent.mkdir()
    dream = rec("D1", "2026-11-12T05:00:00Z", chat="1203@g.us", ctype="group", name="🌙 Rüya", from_me=True)
    store.write_text(rec("A1", "2026-11-12T08:00:00Z") + "\n" + dream + "\n" + rec("A2", "2026-11-12T09:00:00Z")[:30])
    day1 = dt.date(2026, 11, 12)
    assert wk.collect(store, today=day1, dry_run=True)["yeni"] == 1
    assert not wk.raw_dir().exists()                                   # deneme hiçbir şey yazmaz
    s = wk.collect(store, today=day1)
    assert s["yeni"] == 1 and s["atlanan_ruya"] == 1                   # yarım son satır sonraki tura
    raw1 = wk.raw_dir() / "2026-11-12.jsonl"
    first = raw1.read_text()
    assert "Rüya" not in first and json.loads(first)["id"] == "A1"
    # yarım satır tamamlandı + yeni satır; ertesi gün yeni dosyaya eklenir, eski dosya aynen kalır
    store.write_text(store.read_text()[: -30] + rec("A2", "2026-11-12T09:00:00Z") + "\n"
                     + rec("A3", "2026-11-13T07:00:00Z") + "\n")
    s = wk.collect(store, today=dt.date(2026, 11, 13))
    assert raw1.read_text() == first
    ids = [json.loads(x)["id"] for x in (wk.raw_dir() / "2026-11-13.jsonl").read_text().splitlines()]
    assert ids == ["A2", "A3"]
    # okuma durumu kaybolsa / köprü dosyası yenilense (inode değişir) de çift kopya olmaz
    wk.state_path().unlink()
    tmp = store.with_suffix(".yeni")
    tmp.write_text(store.read_text())
    os.replace(tmp, store)
    s = wk.collect(store, today=dt.date(2026, 11, 13))
    assert s["yeni"] == 0 and s["zaten_var"] == 3


def _iphone(conn, data, ids):
    b = Batch(conn, "whatsapp", data / "raw/iphone/extract-2026-11-01/x/ChatStorage.sqlite", "t",
              snapshot="2026-11-01")
    for i in ids:
        b.event(f"905550000001@s.whatsapp.net/{i}", "whatsapp", dt.datetime(2026, 10, 31, 9, tzinfo=UTC))


def test_ingest_filters_and_marks(data):
    (data / "processed").mkdir(parents=True)
    conn = db.connect(data / "processed" / "cfv.sqlite")
    conn.executescript(db.schema_sql())
    with conn:
        _iphone(conn, data, ["I1"])
    lines = [
        rec("I1", "2026-10-31T09:00:00Z"),                                   # iPhone'da var
        rec("OLD", "2026-10-30T09:00:00Z"),                                  # yedekten eski, iPhone'da yok
        rec("N1", "2026-11-05T09:00:00Z", text="nasılsın"),                 # yeni, gelen
        rec("N1", "2026-11-05T09:00:00Z", text="nasılsın"),                 # çift
        rec("G1", "2026-11-05T10:00:00Z", chat="1203@g.us", ctype="group", name="Ekip", from_me=True,
            sender="server-main", text="[server-main] rapor hazır"),       # agent (köprü giden kaydı)
        rec("E1", "2026-11-05T11:00:00Z", chat="1203@g.us", ctype="group", name="Ekip", from_me=True,
            text="[server-main] tamam"),                                     # agent (telefon yankısı)
        rec("H1", "2026-11-05T12:00:00Z", from_me=True, sender="server-sms-isbank", text=wk.HIDDEN),
        rec("D1", "2026-11-06T05:00:00Z", chat="9@g.us", ctype="group", name="🌙 Rüya", from_me=True),
        rec("M1", "2026-11-06T06:00:00Z", text="", file="/srv/whatsapp/media/M1-audio.ogg"),
        rec("T1", "2026-11-06T07:00:00Z", from_me=True, text="[not] kendi notum"),  # Tuna; önek agent adı değil
    ]
    wk.raw_dir().mkdir(parents=True)
    (wk.raw_dir() / "2026-11-13.jsonl").write_text("\n".join(lines) + "\n")
    with conn:
        s = wk.ingest(conn)
    assert (s["iphone_da_var"], s["iphone_yedeginden_eski"], s["cift"], s["icerik_yok"], s["ruya"]) == (1, 1, 1, 1, 1)
    assert s["mesaj"] == 5 and s["agent"] == 2 and s["iphone_kesim"] == "2026-11-01"
    rows = {r[0].rpartition("/")[2]: (r[1], json.loads(r[2])) for r in conn.execute(
        "SELECT e.external_id, e.body, e.payload FROM event e JOIN source s ON s.id=e.source_id "
        "WHERE s.kind='whatsapp-kopru'")}
    assert set(rows) == {"N1", "G1", "E1", "M1", "T1"}
    assert rows["G1"][1]["agent"] == "server-main" and rows["E1"][1]["agent"] == "server-main"
    assert "agent" not in rows["N1"][1] and rows["T1"][1]["sender"] == "ben" and "agent" not in rows["T1"][1]
    assert rows["M1"][0] == "[ses]" and rows["M1"][1]["type"] == "ses"
    snap = conn.execute("SELECT snapshot FROM source WHERE kind='whatsapp-kopru'").fetchone()[0]
    assert snap == "2026-11-06T07:00:00Z"
    # yeni iPhone yedeği N1'i getirdi → köprüden düşer (çift sayılmaz)
    with conn:
        Batch(conn, "whatsapp", data / "raw/iphone/extract-2026-11-08/x/ChatStorage.sqlite", "t",
              snapshot="2026-11-08").event("905550000001@s.whatsapp.net/N1", "whatsapp",
                                           dt.datetime(2026, 11, 5, 9, tzinfo=UTC))
        s = wk.ingest(conn)
    assert s["iphone_da_var"] == 2 and s["iphone_kesim"] == "2026-11-08" and s["mesaj"] == 0  # 11-08 öncesi: telefon esas


def test_drops_handover():
    from akis.gunluk import drops
    before = {"whatsapp": 313000, "whatsapp-kopru": 9000}
    assert drops(before, {"whatsapp": 321900, "whatsapp-kopru": 200}, set()) == []   # 8900 geçti
    out = drops(before, {"whatsapp": 313000, "whatsapp-kopru": 200}, set())
    assert out and out[0].startswith("whatsapp-kopru")
