"""Zepp bulutu çekimleri (raw/zepp-cloud/<YYYY-MM-DDTHHMM>/) → uyku, günlük adım, egzersiz (PLAN cfv/iphone §4A).

Her klasör cfv-zepp'in bir çekimi (`gecmis-<tarih>/` bir kerelik geçmiş çekimi): summary.json (günlük özet; her günün `summary` alanı base64 JSON: `slp` uyku,
`stp` adım), workouts.json (sport/run/history), detail.json (dakikalık seri; burada okunmaz, ham olarak durur).
Biçim resmî olmayan API'den gelir ve ilk gerçek çekime kadar denetlenmedi: tanınmayan kayıt atlanır ve sayılır.

Aynı gün birçok çekimde gelir: klasör adına göre SON çekimin değeri kullanılır (gün içinde artar, en son en tam).
zepp (uygulama yedeği) ve health (Sağlık) daha eksiksizdir: SOURCES'ta onlardan SONRA koşar ve onların kapsadığı
geceyi/günü/egzersizi üretmez; sabah kestirmesi de bu kaynağın kapsadığını atlar. Olay anahtarları ve payload
alanları zepp.py ile aynıdır, yalnız `via: "zepp-cloud"` eklenir.
"""

from __future__ import annotations

import base64
import bisect
import datetime as dt
import json
from pathlib import Path

from cfv.ingest import Batch, raw
from cfv.sources.zepp import SPORT
from cfv.timeutil import from_unix, local_date

IMPORTER = "cfv.sources.zepp_cloud@1"
# bulut evre kodları (topluluk notları): 4 hafif, 5 derin; 7 uyanık, 8 REM yeni cihazlarda. Bilinmeyen → m<kod>.
STAGE = {4: "light", 5: "deep", 7: "awake", 8: "rem"}
WORKOUT_MATCH = dt.timedelta(minutes=10)
TR_OFF = 180


def _num(v) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _off(r: dict) -> int:
    """Kaydın saat dilimi (saniye ya da dakika gelebilir); yoksa İstanbul."""
    tz = _num(r.get("tz") if isinstance(r, dict) else None)
    if tz is None:
        return TR_OFF
    m = tz / 60 if abs(tz) > 900 else tz
    return round(m) if -720 <= m <= 840 else TR_OFF


def read_pulls(root: Path) -> tuple[list[Path], int]:
    """Tamamlanmış (.tamam'lı) çekim klasörleri, eskiden yeniye."""
    # gecmis-<tarih> (bir kerelik geçmiş çekimi) her günlük çekimden önce gelir: aynı günde günlük çekim kazanır
    dirs = sorted((d for d in root.iterdir() if d.is_dir() and (d / ".tamam").exists()),
                  key=lambda d: (not d.name.startswith("gecmis-"), d.name))
    skipped = sum(1 for d in root.iterdir() if d.is_dir() and not (d / ".tamam").exists())
    return dirs, skipped


def _json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def coverage(conn) -> tuple[set[str], set[str], list[dt.datetime]]:
    rows = conn.execute("""SELECT e.kind, e.local_date, e.start_utc FROM event e
                           JOIN source s ON s.id = e.source_id WHERE s.kind IN ('health', 'zepp')
                           AND e.kind IN ('sleep', 'health_day', 'zepp_day', 'workout')""").fetchall()
    nights = {d for k, d, _ in rows if k == "sleep"}
    days = {d for k, d, _ in rows if k in ("health_day", "zepp_day")}
    workouts = sorted(dt.datetime.fromisoformat(s.replace("Z", "+00:00")) for k, _, s in rows if k == "workout")
    return nights, days, workouts


def ingest(conn, root: Path | None = None) -> dict:
    root = root or raw("zepp-cloud")
    if not root.is_dir():
        raise FileNotFoundError(root)
    pulls, partial = read_pulls(root)
    batch = Batch(conn, "zepp-cloud", root, IMPORTER)
    nights_cov, days_cov, workouts_cov = coverage(conn)
    stats = {"pulls": len(pulls), "partial_dirs": partial, "bad_files": 0, "bad_records": 0, "sleeps": 0,
             "days": 0, "workouts": 0, "covered": 0, "last_pull": pulls[-1].name if pulls else None}

    days: dict[str, dict] = {}          # date_time → son çekimin özeti
    workouts: dict[str, dict] = {}      # trackid → son çekimin kaydı
    for d in pulls:
        if (d / "summary.json").exists():
            s = _json(d / "summary.json")
            if not isinstance(s, dict) or not isinstance(s.get("data"), list):
                stats["bad_files"] += 1
            else:
                for r in s["data"]:
                    try:
                        day = dt.date.fromisoformat(r["date_time"]).isoformat()
                        summ = json.loads(base64.b64decode(r["summary"]))
                        if not isinstance(summ, dict):
                            raise ValueError
                    except (KeyError, TypeError, ValueError):
                        stats["bad_records"] += 1
                        continue
                    days[day] = {"s": summ, "off": _off(r) if "tz" in r else _off(summ)}
        if (d / "workouts.json").exists():
            w = _json(d / "workouts.json")
            lst = ((w.get("data") or {}).get("summary") if isinstance(w, dict) else None)
            if not isinstance(lst, list):
                stats["bad_files"] += 1
            else:
                for r in lst:
                    if isinstance(r, dict) and _num(r.get("trackid")):
                        workouts[str(r["trackid"]).split(".")[0]] = r
                    else:
                        stats["bad_records"] += 1

    # --- uyku: gün kaydının `slp`'si o sabah biten gece; gece anahtarı = bitişin yerel günü ---
    for day, rec in sorted(days.items()):
        slp, off = rec["s"].get("slp") or {}, rec["off"]
        a, b = from_unix(_num(slp.get("st"))), from_unix(_num(slp.get("ed")))
        if a is None or b is None or b <= a:
            continue                              # o gece uyku yok
        mins: dict[str, int] = {}
        for st in slp.get("stage") or []:
            try:
                k = STAGE.get(int(st["mode"]), f"m{int(st['mode'])}")
                mins[k] = mins.get(k, 0) + int(st["stop"]) - int(st["start"]) + 1
            except (KeyError, TypeError, ValueError):
                stats["bad_records"] += 1
        if not mins:                              # evre listesi yoksa özet dakikaları
            for key, k in (("lt", "light"), ("dp", "deep"), ("wk", "awake")):
                if (v := _num(slp.get(key))) and v > 0:
                    mins[k] = round(v)
        total = round((b - a).total_seconds() / 60)
        if total < 20:
            continue
        night = local_date(b, off)
        if night in nights_cov:
            stats["covered"] += 1
            continue
        asleep = sum(mins.get(k, 0) for k in ("light", "deep", "rem"))
        h, m = divmod(asleep or total, 60)
        batch.event(f"sleep/{night}", "sleep", a, end=b, tz_offset_min=off, day=night,
                    title=f"uyku {h}s {m:02d}dk",
                    payload={"total_min": total, "asleep_min": asleep or None, "envelope": True,
                             **{f"{k}_min": v for k, v in sorted(mins.items())}, "via": "zepp-cloud"})
        stats["sleeps"] += 1

    # --- günlük aktivite (zepp_day; zepp.py ile aynı alanlar, bulutta olmayanlar None) ---
    for day, rec in sorted(days.items()):
        stp, s = rec["s"].get("stp") or {}, rec["s"]
        steps = round(_num(stp.get("ttl")) or 0)
        if not steps:
            continue
        if day in days_cov:
            stats["covered"] += 1
            continue
        off = rec["off"]
        start = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(), dt.timezone(dt.timedelta(minutes=off)))
        g = lambda v: round(v) if (v := _num(v)) is not None else None  # noqa: E731
        payload = {"steps": steps, "distance_m": g(stp.get("dis")), "calories": g(stp.get("cal")),
                   "walk_min": g(stp.get("wk")), "run_min": g(stp.get("rn")), "step_goal": g(s.get("goal")),
                   "rest_hr": g((s.get("slp") or {}).get("rhr")) or None, "max_hr": None,
                   "pai_daily": None, "pai_total": None, "via": "zepp-cloud"}
        batch.event(f"day/{day}", "zepp_day", start, end=start + dt.timedelta(days=1), tz_offset_min=off,
                    time_quality="day", day=day, title=f"{steps:,} adım".replace(",", "."), payload=payload)
        stats["days"] += 1

    # --- egzersiz: trackid = başlangıç (unix sn) ---
    for tid, r in sorted(workouts.items()):
        start = from_unix(_num(tid))
        dur = _num(r.get("run_time"))
        end = from_unix(_num(r.get("end_time"))) or (start + dt.timedelta(seconds=dur) if dur else None)
        if start is None or end is None or end <= start:
            stats["bad_records"] += 1
            continue
        i = bisect.bisect_left(workouts_cov, start - WORKOUT_MATCH)
        if i < len(workouts_cov) and workouts_cov[i] <= start + WORKOUT_MATCH:
            stats["covered"] += 1
            continue
        typ = int(_num(r.get("type")) or 0)
        mins = round((dur or (end - start).total_seconds()) / 60)
        batch.event(f"workout/{tid}", "workout", start, end=end, tz_offset_min=TR_OFF,
                    title=f"{SPORT.get(typ, f'egzersiz {typ}')} {mins} dk",
                    payload={"activity_type": typ, "duration_min": mins, "tz": None, "gps_points": None,
                             "via": "zepp-cloud"})
        stats["workouts"] += 1
    return stats
