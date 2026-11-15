"""Sabah kestirmesi (raw/sabah/<YYYY-MM>/<gonderim_id>.json) → uyku, tartı, günlük adım/nabız, egzersiz (PLAN cfv/iphone §9.6).

Her dosya tek `{"alindi_at", "govde"}` nesnesi; `govde` kestirmenin gönderdiği JSON metni (v=1). Kestirme her koşuda son
36 saati (tartıda 7 günü) yolladığı için aynı örnek birçok dosyada gelir: örnekler içerikleriyle tekilleştirilir.
`ruya` alanı burada okunmaz (cfv-ruya'nın, `dream` kaynağı).

Haftalık iPhone yedeği (health.py) ve Zepp uygulama yedeği (zepp.py) daha eksiksizdir, onlar kazanır. Bu yüzden
SOURCES'ta health'ten SONRA koşar ve onların kapsadığı şeyi üretmez:
  - sleep: o gece için health ya da zepp'te uyku olayı varsa üretilmez,
  - health_day: o gün health_day ya da zepp_day varsa üretilmez,
  - measurement: health'te aynı external_id (`measurement/<kaynak>/<UTC YYYY-MM-DDTHH:MM>`) varsa üretilmez,
  - workout: health/zepp'te ±10 dk içinde başlayan egzersiz varsa üretilmez.
Kaynak her koşuda baştan kurulur (Batch), yani yedek gelince örtüşen sabah olayları kendiliğinden düşer.
Bozuk dosya/örnek atlanır ve sayılır; hiçbir ham dosyaya yazılmaz.
"""

from __future__ import annotations

import bisect
import datetime as dt
import json
import re
from pathlib import Path

from cfv.ingest import Batch, raw
from cfv.timeutil import iso, local_date

IMPORTER = "cfv.sources.sabah@1"
NIGHT_GAP = dt.timedelta(hours=4)
WORKOUT_MATCH = dt.timedelta(minutes=10)
# Health Uyku Analizi değeri → health.py/zepp.py anahtarları (Sağlık'ın 'core'u = 'light'). Kestirmeler değeri
# telefonun diliyle verebiliyor; Türkçe/İngilizce adlar da kabul edilir (küçük harfe çevrilip bakılır).
SLEEP_VALUE = {"in_bed": "in_bed", "asleep": "asleep", "core": "light", "deep": "deep", "rem": "rem",
               "awake": "awake", "in bed": "in_bed", "asleep (unspecified)": "asleep", "asleep unspecified": "asleep",
               "yatakta": "in_bed", "uykuda": "asleep", "uyku": "asleep", "çekirdek": "light", "cekirdek": "light",
               "temel": "light", "hafif": "light", "derin": "deep", "uyanık": "awake", "uyanik": "awake"}
ASLEEP = ("asleep", "light", "deep", "rem")
RANGE = {"kilo_kg": (20, 300), "yag_yuzde": (2, 70), "yagsiz_kutle_kg": (15, 200), "bmi": (10, 70)}
FIELD = {"kilo_kg": "weight_kg", "yag_yuzde": "fat_pct", "yagsiz_kutle_kg": "lean_kg", "bmi": "bmi"}


def label(kaynak) -> str:
    """Kestirmenin verdiği Sağlık kaynak ADI → health.py'nin paket tabanlı etiketi."""
    k = str(kaynak or "").strip().lower()
    if "zepp" in k or "mi fit" in k or "amazfit" in k:
        return "zepp"
    if "xiaomi" in k or "mi home" in k:
        return "mihome"
    if "iphone" in k or k in ("health", "sağlık", "saglik"):
        return "iphone"
    return k or "bilinmiyor"


def _t(s) -> dt.datetime | None:
    if not isinstance(s, str):
        return None
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else None


def _off(t: dt.datetime) -> int:
    return round(t.utcoffset().total_seconds() / 60)


def _num(v) -> float | None:
    """Sayı ya da kestirmenin yerel biçimli metni ("72,4", "18,1 %", "72.4 kg")."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        v = v.replace("\u00a0", " ").strip()
        if "," in v:                       # Türkçe: "1.234,5" → 1234.5
            v = v.replace(".", "").replace(",", ".")
        if m := re.match(r"^(-?\d+(?:\.\d+)?)", v):
            return float(m[1])
    return None


def read_posts(root: Path) -> tuple[list[tuple[dt.datetime, dict]], int]:
    posts, bad = [], 0
    for f in sorted(root.glob("*/*.json")):
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
            govde = json.loads(rec["govde"])
            alindi = _t(rec["alindi_at"])
            if not isinstance(govde, dict) or govde.get("v") != 1 or alindi is None:
                raise ValueError
        except (OSError, ValueError, KeyError, TypeError):
            bad += 1
            continue
        posts.append((alindi, govde))
    posts.sort(key=lambda p: p[0])
    return posts, bad


def coverage(conn) -> tuple[set[str], set[str], set[str], list[dt.datetime]]:
    """health/zepp'in kapsadığı geceler, günler, ölçüm external_id'leri ve egzersiz başlangıçları."""
    rows = conn.execute("""SELECT e.kind, e.local_date, e.start_utc, e.external_id FROM event e
                           JOIN source s ON s.id = e.source_id WHERE s.kind IN ('health', 'zepp')
                           AND e.kind IN ('sleep', 'health_day', 'zepp_day', 'measurement', 'workout')""").fetchall()
    nights = {d for k, d, _, _ in rows if k == "sleep"}
    days = {d for k, d, _, _ in rows if k in ("health_day", "zepp_day")}
    meas = {x for k, _, _, x in rows if k == "measurement"}
    workouts = sorted(dt.datetime.fromisoformat(s.replace("Z", "+00:00")) for k, _, s, _ in rows if k == "workout")
    return nights, days, meas, workouts


def _hm(minutes: int) -> str:
    return f"{minutes // 60} sa {minutes % 60:02d} dk"


def ingest(conn, root: Path | None = None) -> dict:
    root = root or raw("sabah")
    if not root.is_dir():
        raise FileNotFoundError(root)
    posts, bad = read_posts(root)
    batch = Batch(conn, "sabah", root, IMPORTER)
    nights_cov, days_cov, meas_cov, workouts_cov = coverage(conn)
    stats = {"posts": len(posts), "bad_files": bad, "bad_samples": 0, "sleeps": 0, "measurements": 0, "days": 0,
             "workouts": 0, "covered": 0, "last_post": iso(posts[-1][0]) if posts else None}

    sleep: set[tuple] = set()
    weigh: dict[tuple[str, str], dict] = {}
    daily: dict[str, dict] = {}       # gün → en son gönderimin değerleri (gün içinde artar, son gelen en tam)
    workouts: dict[tuple, dict] = {}
    for _, g in posts:
        for u in g.get("uyku") or []:
            u = u if isinstance(u, dict) else {}
            a, b = _t(u.get("baslangic")), _t(u.get("bitis"))
            v = SLEEP_VALUE.get(str(u.get("deger") or "").strip().lower())
            if a is None or b is None or b <= a or v is None:
                stats["bad_samples"] += 1
                continue
            sleep.add((a, b, v, label(u.get("kaynak"))))
        for m in g.get("tarti") or []:
            t = _t(m.get("zaman")) if isinstance(m, dict) else None
            if t is None:
                stats["bad_samples"] += 1
                continue
            src = label(m.get("kaynak"))
            rec = weigh.setdefault((src, iso(t)[:16]), {"t": t, "source": src})
            for k, name in FIELD.items():
                v = _num(m.get(k))
                if v is not None and RANGE[k][0] <= v <= RANGE[k][1]:
                    rec[name] = round(v, 2)
        for a in g.get("adim") or []:
            if isinstance(a, dict) and isinstance(a.get("gun"), str) and _num(a.get("toplam")) is not None:
                daily.setdefault(a["gun"], {})["steps"] = round(_num(a["toplam"]))
        for n in g.get("nabiz") or []:
            if isinstance(n, dict) and isinstance(n.get("gun"), str):
                d = daily.setdefault(n["gun"], {})
                for k, name in (("dinlenik", "rest_hr"), ("min", "min_hr"), ("ort", "avg_hr"), ("max", "max_hr")):
                    if (v := _num(n.get(k))) is not None and 25 <= v <= 250:
                        d[name] = round(v)
        for w in g.get("egzersiz") or []:
            a, b = (_t(w.get("baslangic")), _t(w.get("bitis"))) if isinstance(w, dict) else (None, None)
            if a is None or b is None or b <= a:
                stats["bad_samples"] += 1
                continue
            workouts[(a, label(w.get("kaynak")))] = {"a": a, "b": b, "tur": w.get("tur"), "src": label(w.get("kaynak")),
                                                    "avg_hr": _num(w.get("ort_nabiz")),
                                                    "max_hr": _num(w.get("maks_nabiz"))}

    # --- uyku: kaynak başına seanslar (4 saatten uzun boşluk yeni seans), gece = bitişin yerel günü ---
    by_src: dict[str, list] = {}
    for row in sorted(sleep):
        by_src.setdefault(row[3], []).append(row)
    nights: dict[str, list[dict]] = {}
    for src, rows in by_src.items():
        sessions: list[list] = []
        for row in rows:
            if sessions and row[0] - max(x[1] for x in sessions[-1]) <= NIGHT_GAP:
                sessions[-1].append(row)
            else:
                sessions.append([row])
        for ss in sessions:
            mins: dict[str, int] = {}
            for a, b, v, _ in ss:
                mins[v] = mins.get(v, 0) + round((b - a).total_seconds() / 60)
            core = [x for x in ss if x[2] in ASLEEP] or ss
            start, end = min(x[0] for x in core), max(x[1] for x in core)
            off = _off(ss[-1][1])
            nights.setdefault(local_date(end, off), []).append(
                {"src": src, "start": start, "end": end, "off": off, "mins": mins})
    for day, cands in sorted(nights.items()):
        if day in nights_cov:
            stats["covered"] += 1
            continue
        staged = lambda c: any(k in c["mins"] for k in ("light", "deep", "rem"))  # noqa: E731
        n = max(cands, key=lambda c: (c["src"] == "zepp", staged(c), c["end"] - c["start"]))
        total = round((n["end"] - n["start"]).total_seconds() / 60)
        if total < 20:
            continue
        asleep = sum(n["mins"].get(k, 0) for k in ASLEEP)
        batch.event(f"sleep/{day}", "sleep", n["start"], end=n["end"], tz_offset_min=n["off"], day=day,
                    title=f"uyku {_hm(asleep or total)}",
                    payload={"total_min": total, "asleep_min": asleep or None, "source": n["src"], "via": "sabah",
                             **{f"{k}_min": v for k, v in sorted(n["mins"].items())}})
        stats["sleeps"] += 1

    # --- tartı ---
    for (src, minute), m in sorted(weigh.items(), key=lambda kv: kv[1]["t"]):
        if "weight_kg" not in m and "fat_pct" not in m:
            continue
        xid = f"measurement/{src}/{minute}"
        if xid in meas_cov:
            stats["covered"] += 1
            continue
        title = f"{m['weight_kg']:.1f} kg".replace(".", ",") if m.get("weight_kg") else f"yağ %{m['fat_pct']:.1f}"
        batch.event(xid, "measurement", m["t"], tz_offset_min=_off(m["t"]), title=title,
                    payload={**{k: m.get(k) for k in ("weight_kg", "fat_pct", "lean_kg", "bmi")}, "source": src,
                             "via": "sabah"})
        stats["measurements"] += 1

    # --- günlük adım/nabız (dakikalık seri haftalık Sağlık'tan gelir) ---
    for day, d in sorted(daily.items()):
        try:
            start = dt.datetime.combine(dt.date.fromisoformat(day), dt.time(), dt.timezone(dt.timedelta(hours=3)))
        except ValueError:
            stats["bad_samples"] += 1
            continue
        if day in days_cov:
            stats["covered"] += 1
            continue
        payload = {k: d.get(k) for k in ("steps", "rest_hr", "min_hr", "avg_hr", "max_hr")}
        if not any(payload.values()):
            continue
        steps = payload["steps"]
        batch.event(f"day/{day}", "health_day", start, end=start + dt.timedelta(days=1), tz_offset_min=180,
                    time_quality="day", day=day, title=f"{steps:,} adım".replace(",", ".") if steps else "sağlık günü",
                    payload={**payload, "source": "sabah", "via": "sabah"})
        stats["days"] += 1

    # --- egzersiz ---
    for (a, _), w in sorted(workouts.items()):
        i = bisect.bisect_left(workouts_cov, a - WORKOUT_MATCH)
        if i < len(workouts_cov) and workouts_cov[i] <= a + WORKOUT_MATCH:
            stats["covered"] += 1
            continue
        mins = round((w["b"] - a).total_seconds() / 60)
        tur = w["tur"] if isinstance(w["tur"], str) and w["tur"] else "egzersiz"
        batch.event(f"workout/{w['src']}/{iso(a)[:16]}", "workout", a, end=w["b"], tz_offset_min=_off(a),
                    title=f"{tur} {mins} dk",
                    payload={"activity": tur, "duration_min": mins, "avg_hr": w["avg_hr"] and round(w["avg_hr"]),
                             "max_hr": w["max_hr"] and round(w["max_hr"]), "source": w["src"], "via": "sabah"})
        stats["workouts"] += 1
    return stats
