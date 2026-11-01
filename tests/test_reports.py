"""Hiyerarşik raporlar — SENTETİK veri. Sınır küçültülerek bölünme ve yukarı doğru bayatlama denenir."""

import datetime as dt

import pytest

from cfv import db, reports
from cfv.ingest import Batch

UTC = dt.timezone.utc


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("CFV_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(reports, "LIMIT", 4000)
    c = db.connect(db.init_db(tmp_path / "t.sqlite"))
    b = Batch(c, "test", tmp_path / "x", "test")
    b.event("a", "note", dt.datetime(2020, 5, 1, 9, tzinfo=UTC), tz_offset_min=180, title="eski not", body="az veri")
    # 2025-03-10: her saatte uzun mesajlar → gün sığmaz, 3 saate iner
    for h in range(6, 24):
        for i in range(8):
            b.event(f"m{h}-{i}", "whatsapp", dt.datetime(2025, 3, 10, h - 3, i, tzinfo=UTC), tz_offset_min=180,
                    title="Örnek Grup", body="x" * 200, payload={"chat": "Örnek Grup", "sender": "Ayşe"})
    b.event("n", "note", dt.datetime(2025, 3, 20, 9, tzinfo=UTC), tz_offset_min=180, title="sakin gün", body="kısa")
    c.commit()
    return c


def _write(n, sig):
    n.file.parent.mkdir(parents=True, exist_ok=True)
    n.file.write_text(f"---\nlevel: {n.level}\nnode: {n.name}\nrange: {n.label}\nsig: {sig}\n---\n# x — y\n")


def test_split_down_only_where_needed(conn):
    todo = {n.name: leaf for n, leaf in reports.jobs(conn)}
    assert todo["2020"] is True                       # az veri: tüm yıl tek yaprak
    assert todo["2025"] is False and todo["kök"] is False
    assert "2025/03/W12/20" not in todo               # sakin gün kendi haftasında yaprak
    assert todo["2025/03/W11/10"] is False            # yoğun gün bölündü
    assert any(k.startswith("2025/03/W11/10/") for k in todo)
    names = list(todo)
    assert names.index("2025/03/W11/10") > max(i for i, k in enumerate(names) if k.startswith("2025/03/W11/10/"))
    assert names[-1] == "kök"


def test_new_data_restales_only_ancestors(conn, tmp_path):
    data = reports.Data(conn)
    for n, kids, sig in reports.walk(data):
        _write(n, sig)
    assert reports.jobs(conn) == []
    b = Batch(conn, "test2", tmp_path / "y", "test")
    b.event("new", "note", dt.datetime(2020, 6, 1, 9, tzinfo=UTC), tz_offset_min=180, title="yeni", body="eklendi")
    conn.commit()
    assert [n.name for n, _ in reports.jobs(conn)] == ["2020", "kök"]
    text = reports.input_text(conn, "kök")
    assert "=== ALT RAPORLAR (2 yıl) ===" in text and "sig: bölünmüş:" in text
