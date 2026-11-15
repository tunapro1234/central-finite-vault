"""Hafif tur (cfv-akis-hafif.timer, 05:00–23:45 her 15 dk): sabah / zepp-cloud / tarti ham verisi değişince yalnız
o kaynakları içe al. PLAN.md "Ek (2026-11-10): hafif sabah turu".

    python -m akis.hafif [--dry-run]

Ucuz başlar: raw/sabah, raw/zepp-cloud (yalnız .tamam'lı klasörler, gecmis-* dahil), raw/tarti altını yürür, parmak izi
(dosya sayısı + toplam boyut + en yeni mtime + yol/boyut/mtime özeti) son işlenenle aynıysa hemen çıkar (log yok,
sahne kopyası yok). Değiştiyse: gece turu koşuyorsa çık → .akis.lock (en çok 2 dk dene; alınamazsa sonraki tik)
→ borg penceresindeyse çık → sahne kopya → `cfv ingest <SOURCES'ta olanlar>` → azalma denetimi (DUR: canlıya
yazmaz, aynı parmak izi tekrar denenmez) → canlıya yayın → gün kapanışı (revizyon). Tree/foto/büyüme yok (gece turu).
Durum: data/logs/akis-hafif.json · satırlar gunluk.log'a "[hafif]" önekiyle · uyarı bp msg server-cfv (aynı metin
24 saatte bir). Gece turu kendi `ingest all`'undan önce aldığı parmak izini buraya yazar → ertesi tik tekrar işlemez.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

from cfv import db

from akis import gun, gunluk, sayim, tazelik

NAMES = ("sabah", "zepp-cloud", "tarti")
LOCK_TRY_S = 120
ALERT_EVERY = dt.timedelta(hours=24)


def state_path() -> Path:
    return gunluk.logs() / "akis-hafif.json"


def _load() -> dict:
    try:
        return json.loads(state_path().read_text())
    except (OSError, ValueError):
        return {}


def _save(st: dict) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".yeni")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1))
    tmp.replace(p)


def _files(name: str, root: Path):
    """Parmak izine giren dosyalar: gizli (.) adlar yarım yazım/işaret sayılır, girmez; zepp-cloud'da yalnız
    .tamam'lı klasörler (yarım çekim work/'te ya da .tamam'sız → okuyucu da okumaz)."""
    if name == "zepp-cloud":
        dirs = [d for d in root.iterdir() if d.is_dir() and (d / ".tamam").exists()]
    else:
        dirs = [root]
    for d in dirs:
        for dp, dns, fns in os.walk(d):
            dns[:] = [x for x in dns if not x.startswith(".")]
            for fn in fns:
                if not fn.startswith("."):
                    yield Path(dp) / fn


def fingerprint(data: Path | None = None) -> dict:
    """{ad: {'dosya', 'bayt', 'son_mtime', 'ozet'} | None (klasör yok)} — yalnız stat, içerik okunmaz."""
    raw = (data or db.data_dir()) / "raw"
    out: dict = {}
    for name in NAMES:
        root = raw / name
        if not root.is_dir():
            out[name] = None
            continue
        h, n, size, newest = hashlib.sha1(), 0, 0, 0
        for f in sorted(_files(name, root)):
            st = f.stat()
            h.update(f"{f.relative_to(root)}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
            n, size, newest = n + 1, size + st.st_size, max(newest, st.st_mtime_ns)
        out[name] = {"dosya": n, "bayt": size,
                     "son_mtime": dt.datetime.fromtimestamp(newest / 1e9).isoformat(timespec="seconds") if n else None,
                     "ozet": h.hexdigest()}
    return out


def mark_done(fp: dict, by: str) -> None:
    """Bu parmak izi işlendi (gece turu `ingest all` sonrası da çağırır)."""
    st = _load()
    st["islenen"] = fp
    st["islendi_at"] = dt.datetime.now().isoformat(timespec="seconds")
    st["islenen_tur"] = by
    _save(st)


def _alert(st: dict, msg: str) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    sent = st.setdefault("uyarilar", {})
    prev = sent.get(msg)
    if prev and now - dt.datetime.fromisoformat(prev) < ALERT_EVERY:
        return
    tazelik.alert(msg, title="cfv hafif tur")
    sent[msg] = now.isoformat(timespec="seconds")
    for k in [k for k, v in sent.items() if now - dt.datetime.fromisoformat(v) > 7 * ALERT_EVERY]:
        del sent[k]


def _lock():
    p = gunluk.proc() / ".akis.lock"
    p.parent.mkdir(parents=True, exist_ok=True)
    f = open(p, "a")
    t0 = time.time()
    while True:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return f
        except BlockingIOError:
            if time.time() - t0 >= LOCK_TRY_S:
                f.close()
                return None
            time.sleep(5)


def _in_borg(now: dt.datetime) -> bool:
    (h0, m0), (h1, m1) = gunluk.BORG_WINDOW
    return (h0, m0) <= (now.hour, now.minute) < (h1, m1)


def _nightly_busy() -> bool:
    st = gunluk.unit_states(["cfv-gunluk.service"]).get("cfv-gunluk.service", {})
    return st.get("state") in ("active", "activating", "deactivating", "reloading")


def tur(args) -> int:
    fp = fingerprint()
    st = _load()
    if fp == st.get("islenen") and not args.force:
        return 0                                                  # değişiklik yok: sessiz çık
    if fp == st.get("dur", {}).get("parmak_izi") and not args.force:
        return 0                                                  # bu ham veri DUR'du; değişene dek tekrar deneme
    changed = [n for n in NAMES if fp.get(n) != (st.get("islenen") or {}).get(n)]
    if _nightly_busy():
        return 0                                                  # gece turu `ingest all` ile zaten alacak
    if _in_borg(dt.datetime.now()):
        return 0
    lk = _lock()
    if lk is None:
        gunluk.log(f"[hafif] değişen: {', '.join(changed)} — kilit {LOCK_TRY_S} sn alınamadı, sonraki tik")
        return 0
    with lk:
        return _run(args, fp, changed)


def _run(args, fp: dict, changed: list[str]) -> int:
    from cfv.cli import SOURCES
    t0 = time.time()
    st = _load()
    live = db.default_db_path()
    names = [n for n in NAMES if n in SOURCES and fp.get(n) is not None]
    no_reader = [n for n in NAMES if n not in SOURCES and fp.get(n) is not None]
    if not names and not no_reader:                               # hiçbir klasör yok (ilk kurulum): sessiz kaydet
        if not args.dry_run:
            mark_done(fp, "hafif")
        return 0
    gunluk.log(f"[hafif] değişen: {', '.join(changed)} → ingest {' '.join(names) or '-'}"
               + (f" (okuyucu yok, ATLANDI: {', '.join(no_reader)})" if no_reader else "")
               + (" (DENEME)" if args.dry_run else ""))
    summary: dict = {"basla": dt.datetime.now().isoformat(timespec="seconds"), "degisen": changed,
                     "ingest": names, "dry_run": args.dry_run}
    if not names:                                                 # yalnız okuyucusu olmayan (tarti) değişti
        if not args.dry_run:
            mark_done(fp, "hafif")
        return 0

    before = sayim.counts(live)
    alias_before = sayim.alias_count(live)
    stage_dir = gunluk.proc() / ".hafif"
    stage_dir.mkdir(exist_ok=True)
    stage = stage_dir / "cfv-hafif.sqlite"
    if stage.exists():                                            # önceki DUR'un kopyası: üzerine yazma, kenara al
        keep = stage.with_name(f"cfv-hafif.dur-{int(stage.stat().st_mtime)}.sqlite")
        stage.rename(keep)
        gunluk.log(f"[hafif] önceki sahne kopyası kenara alındı: {keep.name}")
    gunluk.rm_sqlite(stage)
    gunluk.sqlite_copy(live, stage)
    rc, out, s = gunluk.run([sys.executable, "-m", "cfv.cli", "--db", str(stage), "ingest", *names], timeout=1800)
    for line in out.splitlines():
        gunluk.log(f"[hafif]   {line[:400]}")
    skipped = set(re.findall(r"^\[([\w-]+)\] ATLANDI", out, re.M))
    if rc != 0:
        problems = [f"ingest rc={rc}: {out.splitlines()[-1][:300] if out else ''}"]
        after = {}
    else:
        after = sayim.counts(stage)
        problems = gunluk.drops(before, after, {n for n in skipped if before.get(gunluk.KIND_OF.get(n, n), 0) > 0})
        alias_after = sayim.alias_count(stage)
        if alias_after < alias_before:
            problems.append(f"person_alias {alias_before} → {alias_after} ({alias_after - alias_before:+d})")
    diff = {k: after.get(k, 0) - before.get(k, 0) for k in sorted(set(before) | set(after))}
    moved = {k: d for k, d in diff.items() if d}
    gunluk.log(f"[hafif] önce→sonra: {', '.join(f'{k} {d:+d}' for k, d in moved.items()) or 'değişiklik yok'}")
    summary["fark"] = moved
    if problems:
        msg = "cfv hafif tur DUR (canlı DB'ye yazılmadı): " + "; ".join(problems) + \
              f". Sahne kopyası: data/processed/.hafif/{stage.name}"
        gunluk.log(f"[hafif] {msg}")
        if args.dry_run:
            gunluk.rm_sqlite(stage)
            return 2
        st["dur"] = {"parmak_izi": fp, "at": dt.datetime.now().isoformat(timespec="seconds"), "sorunlar": problems}
        _alert(st, msg)
        _save(st)
        return 2
    if args.dry_run:
        gunluk.rm_sqlite(stage)
        gunluk.log(f"[hafif] DENEME bitti: denetim geçti, canlıya dokunulmadı ({time.time() - t0:.0f} sn)")
        return 0
    with sqlite3.connect(stage) as c:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    gunluk.sqlite_copy(stage, live)
    if sayim.counts(live) != after:
        raise RuntimeError("yayın sonrası canlı DB sayıları sahneyle aynı değil")
    gunluk.rm_sqlite(stage)
    g = gun.close_days(live)
    gunluk.log(f"[hafif] canlı güncellendi · gün: kapanan={len(g['kapanan'])} revize={len(g['revize'])} "
               f"{','.join(g['revize'][-10:])} · {time.time() - t0:.0f} sn")
    st = _load()
    st.pop("dur", None)
    st["islenen"], st["islenen_tur"] = fp, "hafif"
    st["islendi_at"] = dt.datetime.now().isoformat(timespec="seconds")
    st["son_tur"] = {**summary, "revize": g["revize"][-20:], "kapanan": len(g["kapanan"]),
                     "sure_sn": round(time.time() - t0)}
    _save(st)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="akis.hafif")
    ap.add_argument("--dry-run", action="store_true", help="sahnede ingest + denetim; canlıya/duruma yazmaz")
    ap.add_argument("--force", action="store_true", help="parmak izi aynı olsa da işle")
    ap.add_argument("--goster", action="store_true", help="parmak izini ve durumu yazdır, hiçbir şey yapma")
    a = ap.parse_args(argv)
    if a.goster:
        print(json.dumps({"simdi": fingerprint(), "durum": _load()}, ensure_ascii=False, indent=1))
        return 0
    try:
        return tur(a)
    except Exception as e:
        gunluk.log(f"[hafif] HATA: {type(e).__name__}: {e}")
        if not a.dry_run:
            st = _load()
            _alert(st, f"cfv hafif tur HATA: {type(e).__name__}: {str(e)[:300]}")
            _save(st)
        raise


if __name__ == "__main__":
    sys.exit(main())
