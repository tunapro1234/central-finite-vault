"""Google Takvim → data/raw/google-calendar/ sürekli (salt-okur) senkron.

Her çalıştırmada:
1. Takvim başına `gog calendar changed --since <son kontrol>` (ucuz). Değişiklik yoksa biter.
2. Değişiklik varsa ya da son tam çekimden 24 saat geçtiyse: tüm takvimler tam çekilir
   (tekrarlar açılmış halde, 2010 → bugün+400 gün), önceki anlık görüntüyle karşılaştırılır,
   fark `changes.jsonl`'a eklenir (silinen olayların tam içeriği dahil — arşiv kaybolmaz),
   `events.json` atomik olarak yenilenir.
3. Değişiklik olduysa (ops.) cfv.sqlite'a import + klasör ağacı yeniden üretilir.

gog yalnızca `--readonly --enable-commands=calendar` ile çağrılır: yazma isteği imkânsız.
Ağ yoksa / token düştüyse: log'a yazar, hiçbir şeye dokunmadan 0 ile çıkar (timer sessiz kalır).
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from cfv.db import data_dir

GOG = os.environ.get("CFV_GOG", "/home/tuna/.local/share/cfv-gog/gog")
ACCOUNT = os.environ.get("CFV_GCAL_ACCOUNT", "tunagul54@gmail.com")
FULL_EVERY = dt.timedelta(hours=24)
WINDOW_START = "2010-01-01"
WINDOW_AHEAD_DAYS = 400
MAX_REMOVED_RATIO = 0.2  # tam çekimde olayların %20'sinden fazlası "silindi" görünürse yazma

log = logging.getLogger("cfv.gcal")


class GogError(RuntimeError):
    pass


def out_dir() -> Path:
    return data_dir() / "raw" / "google-calendar"


def log_path() -> Path:
    return data_dir() / "logs" / "gcal-sync.log"


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def gog(*args: str, timeout: int = 180) -> dict:
    cmd = [GOG, "--readonly", "--enable-commands=calendar", "--no-input", "--color=never",
           "--account", ACCOUNT, "calendar", *args, "--json"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as e:
        raise GogError(f"{args[0]}: {type(e).__name__}: {e}") from None
    err = p.stderr.strip()
    if p.returncode != 0 or "error" in err.lower():
        raise GogError(f"{args[0]} rc={p.returncode}: {err[:400]}")
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError as e:
        raise GogError(f"{args[0]}: JSON okunamadı: {e}") from None


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def calendars() -> list[dict]:
    d = gog("calendars", "--max", "250")
    cals = d.get("calendars", d if isinstance(d, list) else [])
    return [{k: c.get(k) for k in ("id", "summary", "accessRole", "timeZone", "primary")} for c in cals]


def changed_since(cal_ids: list[str], since: dt.datetime) -> int:
    n = 0
    for cid in cal_ids:
        d = gog("changed", cid, "--since", _iso(since), "--max", "2500")
        n += len(d.get("events") or [])
    return n


def full_fetch(cal_ids: list[str], now: dt.datetime) -> list[dict]:
    to = (now + dt.timedelta(days=WINDOW_AHEAD_DAYS)).date().isoformat()
    events = []
    for cid in cal_ids:
        d = gog("events", cid, "--from", WINDOW_START, "--to", to, "--all-pages", "--max", "2500")
        if any(d.get("nextPageTokens") or []) or d.get("nextPageToken"):
            raise GogError(f"{cid}: sayfalama tamamlanmadı")
        for e in d.get("events") or []:
            e["CalendarID"] = cid
            events.append(e)
    return events


def _key(e: dict) -> str:
    return f"{e['CalendarID']}/{e['id']}"


def diff(old: list[dict], new: list[dict]) -> dict:
    o = {_key(e): e for e in old}
    n = {_key(e): e for e in new}
    return {
        "added": sorted(k for k in n if k not in o),
        "changed": sorted(k for k in n if k in o and n[k].get("etag") != o[k].get("etag")),
        "removed": [o[k] for k in sorted(o) if k not in n],
    }


def sync(*, force_full: bool = False, allow_mass_removal: bool = False) -> dict:
    """Bir senkron turu. Dönen sözlük: ne yapıldığı. GogError dışarı sızmaz → 'error'."""
    root = out_dir()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    state_p, events_p = root / "state.json", root / "events.json"
    lock = open(root / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return {"status": "busy"}

    state = _read_json(state_p, {})
    now = _now()
    try:
        cals = calendars()
        cal_ids = [c["id"] for c in cals]
        last_full = state.get("last_full") and dt.datetime.fromisoformat(state["last_full"])
        last_check = state.get("last_check") and dt.datetime.fromisoformat(state["last_check"])
        need_full = force_full or not events_p.exists() or not last_full or now - last_full >= FULL_EVERY
        reason = "zorla" if force_full else "ilk/periyodik" if need_full else None
        if not need_full:
            n = changed_since(cal_ids, last_check - dt.timedelta(minutes=10))
            if n:
                need_full, reason = True, f"{n} değişiklik"
        if not need_full:
            state.update(last_check=_iso(now), last_ok=_iso(now), last_error=None, failures=0)
            _write_json(state_p, state)
            return {"status": "unchanged"}

        new = full_fetch(cal_ids, now)
    except GogError as e:
        state.update(last_error=f"{_iso(now)} {e}", failures=state.get("failures", 0) + 1)
        _write_json(state_p, state)
        return {"status": "error", "error": str(e)}

    old = _read_json(events_p, {}).get("events", [])
    d = diff(old, new)
    if old and len(d["removed"]) > MAX_REMOVED_RATIO * len(old) and not allow_mass_removal:
        msg = f"{len(d['removed'])}/{len(old)} olay silinmiş görünüyor; yazılmadı (--allow-mass-removal)"
        state.update(last_error=f"{_iso(now)} {msg}", failures=state.get("failures", 0) + 1)
        _write_json(state_p, state)
        return {"status": "error", "error": msg}

    changed = bool(d["added"] or d["changed"] or d["removed"])
    if changed or not events_p.exists():
        _write_json(events_p, {"fetched_at": _iso(now), "account": ACCOUNT,
                               "window": [WINDOW_START, (now + dt.timedelta(days=WINDOW_AHEAD_DAYS)).date().isoformat()],
                               "calendars": cals, "events": new})
        with open(root / "changes.jsonl", "a") as f:
            f.write(json.dumps({"at": _iso(now), "reason": reason, "total": len(new), **d},
                               ensure_ascii=False) + "\n")
    state.update(last_check=_iso(now), last_full=_iso(now), last_ok=_iso(now), last_error=None,
                 failures=0, total=len(new))
    _write_json(state_p, state)
    return {"status": "changed" if changed else "unchanged", "reason": reason, "total": len(new),
            "added": len(d["added"]), "changed": len(d["changed"]), "removed": len(d["removed"])}


def setup_logging() -> None:
    p = log_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    h = logging.FileHandler(p)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(h)
    log.setLevel(logging.INFO)


def run(*, force_full=False, allow_mass_removal=False, ingest=True) -> int:
    """systemd timer'ın çağırdığı giriş. Her durumda 0 döner; ayrıntı log'da."""
    os.umask(0o077)
    setup_logging()
    try:
        r = sync(force_full=force_full, allow_mass_removal=allow_mass_removal)
    except Exception as e:  # beklenmeyen hata da timer'ı gürültüye boğmasın
        log.exception("beklenmeyen hata: %s", e)
        return 0
    if r["status"] == "error":
        log.warning("senkron yapılamadı: %s", r["error"])
        return 0
    if r["status"] == "busy":
        log.info("başka bir senkron sürüyor, atlandı")
        return 0
    if r["status"] == "unchanged":
        log.info("değişiklik yok%s", f" (tam çekim: {r['total']} olay)" if "total" in r else "")
        return 0
    log.info("güncellendi (%s): +%d ~%d -%d, toplam %d", r["reason"], r["added"], r["changed"],
             r["removed"], r["total"])
    if ingest:
        try:
            from cfv import db, tree
            from cfv.sources import calendar_gcal
            conn = db.connect(db.default_db_path())
            conn.executescript(db.schema_sql())
            with conn:
                stats = calendar_gcal.ingest(conn)
            from cfv import link
            link.run_all(conn)
            tree.build(conn, data_dir() / "processed" / "timeline")
            conn.close()
            log.info("cfv.sqlite + timeline güncellendi: %s", stats)
        except Exception as e:
            log.exception("import/ağaç hatası: %s", e)
    return 0
