"""Google Takvim / Gmail senkronu (gog v0.11) + src/gsync betikleri — sentetik veriyle."""

import datetime as dt
import importlib.machinery
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

GSYNC = Path(__file__).resolve().parents[1] / "src" / "gsync"


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


gog_ro = _load("gog_ro", GSYNC / "gog-ro")
check = _load("gsync_check", GSYNC / "check.py")


@pytest.mark.parametrize("args", [
    ["--enable-commands=calendar", "--no-input", "--account", "me@example.com", "calendar", "calendars", "--json"],
    ["--account", "me@example.com", "calendar", "events", "c1", "--from", "2010-01-01", "--all-pages", "--json"],
    ["--enable-commands=gmail", "--account", "me@example.com", "gmail", "labels", "list", "--json"],
    ["--account", "me@example.com", "gmail", "messages", "search", "in:anywhere after:1", "--all", "--max", "500"],
    ["--account", "me@example.com", "gmail", "get", "abc", "--format", "metadata", "--json"],
])
def test_gog_ro_allows_reads(args):
    assert gog_ro.check(args)[0] is None


@pytest.mark.parametrize("args", [
    ["gmail", "send", "--to", "x@example.com"],
    ["send", "--to", "x@example.com"],
    ["calendar", "create", "primary"],
    ["calendar", "delete", "primary", "e1"],
    ["gmail", "thread", "modify", "t1"],
    ["gmail", "labels", "create", "x"],
    ["gmail", "batch", "delete", "a"],
    ["auth", "list"],
    ["--account", "calendar", "gmail", "send"],          # değer bayrağı konumsal sayılmaz
    ["-y", "calendar", "events"],
    ["--enable-commands=gmail", "calendar", "events"],   # kısıt komutla uyuşmalı
    [],
])
def test_gog_ro_denies_everything_else(args):
    assert gog_ro.check(args)[0] is not None


def test_gog_ro_exec_rejects_with_exit_2(tmp_path):
    p = subprocess.run([sys.executable, str(GSYNC / "gog-ro"), "gmail", "send"],
                       capture_output=True, text=True, env={"GOG_BIN": str(tmp_path / "yok")})
    assert p.returncode == 2 and "reddedildi" in p.stderr


FAKE_GOG = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
assert "--readonly" not in args, args
assert "--enable-commands=calendar" in args and "--no-input" in args, args
os.makedirs(os.path.dirname(os.environ["FAKE_LOG"]), exist_ok=True)
open(os.environ["FAKE_LOG"], "a").write(json.dumps(args) + "\n")
db = json.load(open(os.environ["FAKE_DB"]))
i = args.index("calendar")
cmd = args[i + 1]
if cmd == "calendars":
    print(json.dumps({"calendars": db["calendars"], "nextPageToken": ""}))
elif cmd == "events":
    print(json.dumps({"events": db["events"].get(args[i + 2], []), "nextPageToken": ""}))
else:
    sys.exit(f"beklenmeyen: {cmd}")
'''


@pytest.fixture
def gcal(tmp_path, monkeypatch):
    from cfv import gcal_sync
    data = tmp_path / "data"
    (data / "config").mkdir(parents=True)
    (data / "config" / "gcal-account").write_text("me@example.com\n")
    fake = tmp_path / "gog"
    fake.write_text(FAKE_GOG)
    fake.chmod(0o755)
    db = tmp_path / "db.json"
    monkeypatch.setenv("CFV_DATA", str(data))
    monkeypatch.delenv("CFV_GCAL_ACCOUNT", raising=False)
    monkeypatch.setenv("FAKE_DB", str(db))
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "calls.jsonl"))
    monkeypatch.setattr(gcal_sync, "GOG", str(fake))

    def put(events_by_cal):
        db.write_text(json.dumps({"calendars": [{"id": c, "summary": c} for c in events_by_cal],
                                  "events": events_by_cal}))
    return gcal_sync, data / "raw" / "google-calendar", put


def _ev(i, etag="1"):
    return {"id": f"e{i}", "etag": etag, "summary": f"olay {i}", "start": {"date": "2025-01-01"}}


def test_gcal_full_fetch_every_run(gcal, tmp_path):
    gs, root, put = gcal
    put({"c1": [_ev(i) for i in range(10)], "c2": [_ev(100)]})
    assert gs.sync()["status"] == "changed"
    snap = json.loads((root / "events.json").read_text())
    assert snap["account"] == "me@example.com" and len(snap["events"]) == 11 and len(snap["calendars"]) == 2
    st = json.loads((root / "state.json").read_text())
    assert st["total"] == 11 and st["calendars"] == 2 and st["failures"] == 0

    assert gs.sync()["status"] == "unchanged"           # aynı veri: events.json yeniden yazılmaz
    put({"c1": [_ev(0, "2")] + [_ev(i) for i in range(1, 10)] + [_ev(11)], "c2": []})
    r = gs.sync()
    assert (r["added"], r["changed"], r["removed"]) == (1, 1, 1)
    lines = [json.loads(x) for x in (root / "changes.jsonl").read_text().splitlines()]
    assert len(lines) == 2 and lines[1]["removed"][0]["id"] == "e100"   # silinenin tam içeriği
    calls = [json.loads(x) for x in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert not any("changed" in c for c in calls)        # gog v0.11'de yok


def test_gcal_mass_removal_not_written(gcal):
    gs, root, put = gcal
    put({"c1": [_ev(i) for i in range(10)]})
    gs.sync()
    before = (root / "events.json").read_text()
    put({"c1": [_ev(0)]})
    r = gs.sync()
    assert r["status"] == "error" and "silinmiş" in r["error"]
    assert (root / "events.json").read_text() == before
    assert json.loads((root / "state.json").read_text())["failures"] == 1


def test_gcal_missing_account_is_error(gcal, tmp_path):
    gs, root, put = gcal
    (tmp_path / "data" / "config" / "gcal-account").unlink()
    put({"c1": [_ev(1)]})
    r = gs.sync()
    assert r["status"] == "error" and "hesab" in r["error"]
    assert not (root / "events.json").exists()


def test_gmail_fetch_reads_get_metadata(monkeypatch):
    from cfv import gmail_sync
    seen = []

    def fake(account, *args, timeout=300):
        seen.append(args)
        return {"headers": {"from": "x"}, "message": {
            "id": "m1", "threadId": "t1", "internalDate": "1735725600000", "labelIds": ["INBOX", "Label_1"],
            "snippet": "merhaba", "payload": {"headers": [
                {"name": "From", "value": "Ayşe <a@example.com>"}, {"name": "To", "value": "me@example.com"},
                {"name": "Subject", "value": "Konu"}, {"name": "Cc"}]}}}  # v0.11: boş değerde "value" yok
    monkeypatch.setattr(gmail_sync, "gog", fake)
    m = gmail_sync.fetch("me@example.com", "m1", {"Label_1": "Proje"})
    assert seen == [("get", "m1", "--format", "metadata", "--json")]
    assert m == {"id": "m1", "thread": "t1", "time": "2025-01-01T10:00:00Z", "labels": ["INBOX", "Proje"],
                 "from": "Ayşe <a@example.com>", "to": "me@example.com", "cc": None,
                 "subject": "Konu", "snippet": "merhaba"}


def test_gmail_gog_command_has_no_readonly(monkeypatch):
    from cfv import gmail_sync
    got = {}

    def run(cmd, **kw):
        got["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout='{"labels": []}', stderr="")
    monkeypatch.setattr(gmail_sync.subprocess, "run", run)
    gmail_sync.gog("me@example.com", "labels", "list", "--json")
    assert "--readonly" not in got["cmd"] and "--enable-commands=gmail" in got["cmd"]
    assert gog_ro.check(got["cmd"][1:])[0] is None       # sarmalayıcı izin veriyor


def _data(tmp_path, *, events=100, cals=3, last_ok="2026-01-10T06:00:00Z", total=50, failures=0):
    d = tmp_path / "data"
    g = d / "raw" / "google-calendar"
    g.mkdir(parents=True, exist_ok=True)
    (g / "state.json").write_text(json.dumps({"last_ok": last_ok, "failures": failures}))
    (g / "events.json").write_text(json.dumps({"fetched_at": last_ok, "calendars": [{}] * cals,
                                               "events": [{}] * events}))
    (d / "config").mkdir(exist_ok=True)
    (d / "config" / "gmail-accounts").write_text("me@example.com\n")
    a = d / "raw" / "gmail" / "me@example.com"
    a.mkdir(parents=True, exist_ok=True)
    (a / "state.json").write_text(json.dumps({"last_fetch": last_ok, "failures": 0, "total": total}))
    return d


def test_check_ok_then_drop(tmp_path, monkeypatch):
    now = dt.datetime(2026, 1, 10, 9, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(check, "DATA", _data(tmp_path))
    r = check.check(now, {})
    assert r["ok"] and r["gcal"]["events"] == 100 and r["gmail"]["me@example.com"]["total"] == 50

    monkeypatch.setattr(check, "DATA", _data(tmp_path, events=90, cals=2, total=49))
    r2 = check.check(now, r)
    assert not r2["ok"] and r2["streak"] == 1
    joined = " ".join(r2["problems"])
    assert "olay sayısı 100 → 90" in joined and "takvim sayısı 3 → 2" in joined and "mesaj sayısı 50 → 49" in joined
    assert "me@example.com" not in joined                  # mesajda hesap adı yok


def test_check_stale_and_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(check, "DATA", _data(tmp_path, last_ok="2026-01-08T00:00:00Z", failures=2))
    r = check.check(dt.datetime(2026, 1, 10, 9, tzinfo=dt.timezone.utc), {"streak": 2})
    assert not r["ok"] and r["streak"] == 3
    assert any(">26 sa" in p for p in r["problems"]) and any("2 hata" in p for p in r["problems"])


def test_snapshot_only_adds(tmp_path):
    f = tmp_path / "events.json"
    f.write_text("v1")
    run = lambda: subprocess.run([str(GSYNC / "snapshot.sh"), str(f), "events"], check=True)
    run()
    run()                                                   # değişmedi → yeni kopya yok
    hist = tmp_path / "history"
    assert len(list(hist.iterdir())) == 1
    first = next(hist.iterdir())
    f.write_text("v2")
    # aynı saniyede ikinci kopya adı çakışır → üzerine yazmaz; eski kopya bozulmaz
    old = first.read_bytes()
    run()
    assert first.read_bytes() == old
    assert subprocess.run(["zstd", "-dc", str(first)], capture_output=True, text=True).stdout == "v1"


meta_watch = _load("gsync_meta_watch", GSYNC / "meta_watch.py")


def _gmail(tmp_path, msgs):
    d = tmp_path / "data"
    (d / "config").mkdir(parents=True, exist_ok=True)
    (d / "config" / "gmail-accounts").write_text("me@example.com\n")
    a = d / "raw" / "gmail" / "me@example.com"
    a.mkdir(parents=True, exist_ok=True)
    (a / "messages.jsonl").write_text("".join(json.dumps(m) + "\n" for m in msgs))
    return d


def _m(i, time, frm="Instagram <security@mail.instagram.com>", subj="Your Meta information download is ready"):
    return {"id": f"m{i}", "time": time, "from": frm, "subject": subj, "snippet": "GİZLİ-İÇERİK https://x"}


@pytest.mark.parametrize("m,ok", [
    (_m(1, "t"), True),
    (_m(1, "t", subj="Bilgilerin indirilmeye hazır"), True),
    (_m(1, "t", frm="Meta <notification@facebookmail.com>", subj="Your information is ready to download"), True),
    (_m(1, "t", subj="Your Meta download request is in progress"), False),
    (_m(1, "t", frm="kotu@example.com"), False),                     # gönderen Meta değil
])
def test_meta_watch_matcher(m, ok):
    assert meta_watch.is_ready_mail(m) is ok


def test_meta_watch_notifies_once(tmp_path, monkeypatch):
    now = dt.datetime(2026, 1, 10, 6, tzinfo=dt.timezone.utc)
    calls = []
    monkeypatch.setattr(meta_watch.subprocess, "run",
                        lambda cmd, **kw: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    monkeypatch.setattr(meta_watch, "DATA", _gmail(tmp_path, [_m(1, "2026-01-01T10:00:00Z")]))  # eski
    r = meta_watch.run(now)                                   # ilk çalıştırma: bildirim yok
    assert r["first"] and calls == []

    monkeypatch.setattr(meta_watch, "DATA", _gmail(tmp_path, [
        _m(1, "2026-01-01T10:00:00Z"),                        # pencere dışı (>4 gün)
        _m(2, "2026-01-09T10:25:00Z"),                        # yeni hazır maili
        _m(3, "2026-01-09T10:15:00Z", subj="Your Meta download request is in progress"),
    ]))
    r = meta_watch.run(now)
    assert r["sent"] == ["m2"]
    assert [c[0] for c in calls] == ["laptop-notify", "bp"]
    assert calls[0][1:] == ["Instagram verisi hazır", "me hesabına Meta indirme maili geldi, bağlantı birkaç gün geçerli",
                            "normal"]
    assert calls[1][1:3] == ["msg", "server-cfv"]
    assert all("GİZLİ" not in x and "https" not in x and "download is ready" not in x for c in calls for x in c)

    calls.clear()
    assert meta_watch.run(now)["sent"] == [] and calls == []  # aynı mail ikinci kez bildirilmez
    seen = json.loads((tmp_path / "data" / "logs" / "meta-watch.json").read_text())["seen"]
    assert seen == ["m2"]


def test_meta_watch_retries_if_nothing_sent(tmp_path, monkeypatch):
    now = dt.datetime(2026, 1, 10, 6, tzinfo=dt.timezone.utc)
    d = _gmail(tmp_path, [])
    (d / "logs").mkdir()
    (d / "logs" / "meta-watch.json").write_text('{"seen": []}')
    (d / "raw" / "gmail" / "me@example.com" / "messages.jsonl").write_text(json.dumps(_m(2, "2026-01-09T10:25:00Z")) + "\n")
    monkeypatch.setattr(meta_watch, "DATA", d)
    monkeypatch.setattr(meta_watch.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1))
    assert meta_watch.run(now)["sent"] == []                  # hiçbir bildirim gitmedi → yarın yeniden dener
    assert json.loads((d / "logs" / "meta-watch.json").read_text())["seen"] == []
