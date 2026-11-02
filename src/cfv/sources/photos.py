"""iPhone Photos.sqlite → photo/video event'leri + kişiler + icloud-sync dosya eşlemesi.

Dosya eşleme (icloudpd adlandırması, photos ajanından):
  - ad = iCloud orijinal adı (ZADDITIONALASSETATTRIBUTES.ZORIGINALFILENAME), harf büyüklüğü farklı olabilir
  - çakışma: '<ad>-<iCloud orijinal boyutu>.<uzt>'  (≈ ZORIGINALFILESIZE)
  - düzenlenmiş: '<ad>-adjusted.<uzt>' (uzantı farklı olabilir), Live Photo videosu: '<ad>_HEVC.MOV'
  - '<dosya>.xmp' içinde xmp:CreateDate (tz'li) → çekim zamanı (±2 sn)
"""

from __future__ import annotations

import datetime as dt
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from cfv.ingest import Batch, open_ro, raw
from cfv.timeutil import from_coredata

IMPORTER = "cfv.sources.photos@1"
DEFAULT_DB = ("iphone", "photos-db-2026-11-01", "Photos.sqlite")
DEFAULT_LIBRARY = Path(os.environ.get("CFV_PHOTO_LIBRARY", Path.home() / "Pictures" / "icloud-sync"))

SUBTYPE_TAG = {1: "panorama", 2: "live", 10: "screenshot", 100: "video-streamed",
               101: "slomo", 102: "timelapse", 103: "screen-recording"}
_XMP_DATE = re.compile(r"<xmp:CreateDate>([^<]+)</xmp:CreateDate>")


@dataclass
class DiskFile:
    path: Path
    stem: str       # eşleme anahtarı: küçük harf, son eksiz kök ad
    ext: str        # küçük harf, noktasız
    role: str       # original | adjusted | live
    size_hint: int | None  # çakışma son ekindeki orijinal boyut
    taken: dt.datetime | None
    used: bool = False


def _xmp_time(p: Path) -> dt.datetime | None:
    try:
        m = _XMP_DATE.search(p.read_text(errors="ignore"))
    except OSError:
        return None
    if not m:
        return None
    s = m.group(1)
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            t = dt.datetime.strptime(s, fmt)
            return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    return None


def parse_name(name: str) -> tuple[str, str, str, int | None]:
    """'IMG_0787-5337221.JPG' → ('img_0787', 'jpg', 'original', 5337221)."""
    stem, _, ext = name.rpartition(".")
    role, size = "original", None
    if stem.endswith("-adjusted"):
        stem, role = stem[: -len("-adjusted")], "adjusted"
    elif stem.endswith("_HEVC") and ext.lower() == "mov":
        stem, role = stem[: -len("_HEVC")], "live"
    m = re.fullmatch(r"(.+)-(\d{4,})", stem)
    if m:
        stem, size = m.group(1), int(m.group(2))
    return stem.lower(), ext.lower(), role, size


def scan_library(root: Path) -> dict[str, list[DiskFile]]:
    index: dict[str, list[DiskFile]] = defaultdict(list)
    if not root.exists():
        return index
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]  # gizli klasörler (ör. kopya karantinası) atlanır
        for n in names:
            if n.endswith((".xmp", ".part")) or n.startswith("."):
                continue
            p = Path(dirpath) / n
            stem, ext, role, size = parse_name(n)
            xmp = p.with_name(n + ".xmp")
            index[stem].append(DiskFile(p, stem, ext, role, size, _xmp_time(xmp) if xmp.exists() else None))
    return index


def _score(f: DiskFile, taken: dt.datetime, orig_size: int | None) -> float:
    """Küçük = iyi. Zaman farkı (sn) ana ölçüt; boyut son eki ve klasör ayı ipucu."""
    s = 0.0
    if f.taken is not None:
        s += min(abs((f.taken - taken).total_seconds()), 10**7)
    else:
        # klasör YYYY/MM ile çekim ayı uzaklığı (ay sınırında tz kayması olabilir)
        try:
            y, m = int(f.path.parent.parent.name), int(f.path.parent.name)
            s += abs((y * 12 + m) - (taken.year * 12 + taken.month)) * 86400 * 20 + 3600
        except ValueError:
            s += 10**6
    if f.size_hint is not None and orig_size:
        s += 0 if abs(f.size_hint - orig_size) <= 1024 else 10**6
    return s


def ingest(conn, db_path: Path | None = None, library: Path | None = None) -> dict:
    db_path = db_path or raw(*DEFAULT_DB)
    library = library or DEFAULT_LIBRARY
    src = open_ro(db_path)
    batch = Batch(conn, "iphone-photos", db_path, IMPORTER, snapshot=db_path.parent.name[-10:])

    # --- kişiler (birleştirilmiş kişileri hedefe çöz) ---
    persons = {r["Z_PK"]: r for r in src.execute(
        "SELECT Z_PK, ZFULLNAME, ZDISPLAYNAME, ZPERSONUUID, ZMERGETARGETPERSON FROM ZPERSON")}

    def resolve(pk):
        seen = set()
        while pk in persons and persons[pk]["ZMERGETARGETPERSON"] and pk not in seen:
            seen.add(pk)
            pk = persons[pk]["ZMERGETARGETPERSON"]
        return pk

    faces: dict[int, set[int]] = defaultdict(set)
    for r in src.execute("SELECT ZASSETFORFACE a, ZPERSONFORFACE p FROM ZDETECTEDFACE "
                         "WHERE ZASSETFORFACE IS NOT NULL AND ZPERSONFORFACE IS NOT NULL"):
        pk = resolve(r["p"])
        if pk in persons and (persons[pk]["ZFULLNAME"] or "").strip():
            faces[r["a"]].add(pk)

    albums: dict[int, list[str]] = defaultdict(list)
    for r in src.execute(
            "SELECT j.Z_3ASSETS a, g.ZTITLE t FROM Z_30ASSETS j JOIN ZGENERICALBUM g ON g.Z_PK=j.Z_30ALBUMS "
            "WHERE g.ZKIND=2 AND g.ZTRASHEDSTATE=0 AND g.ZTITLE IS NOT NULL"):
        albums[r["a"]].append(r["t"])

    moments = {r["Z_PK"]: (r["ZTITLE"], r["ZSUBTITLE"]) for r in src.execute(
        "SELECT Z_PK, ZTITLE, ZSUBTITLE FROM ZMOMENT")}

    disk = scan_library(library)
    stats = defaultdict(int)
    by_stem: dict[str, list[tuple[int, dt.datetime, str, int | None]]] = defaultdict(list)

    rows = src.execute("""
        SELECT a.Z_PK, a.ZUUID, a.ZKIND, a.ZKINDSUBTYPE, a.ZDATECREATED, a.ZLATITUDE, a.ZLONGITUDE,
               a.ZFAVORITE, a.ZHIDDEN, a.ZWIDTH, a.ZHEIGHT, a.ZDURATION, a.ZMOMENT,
               a.ZUNIFORMTYPEIDENTIFIER uti,
               x.ZORIGINALFILENAME fname, x.ZORIGINALFILESIZE fsize, x.ZTIMEZONEOFFSET tzoff,
               x.ZTIMEZONENAME tzname, d.ZLONGDESCRIPTION descr
        FROM ZASSET a LEFT JOIN ZADDITIONALASSETATTRIBUTES x ON x.ZASSET = a.Z_PK
        LEFT JOIN ZASSETDESCRIPTION d ON d.Z_PK = x.ZASSETDESCRIPTION
        WHERE a.ZTRASHEDSTATE = 0
        ORDER BY a.ZDATECREATED""").fetchall()

    for r in rows:
        taken = from_coredata(r["ZDATECREATED"])
        if taken is None:
            stats["no_date"] += 1
            continue
        off = int(r["tzoff"] // 60) if r["tzoff"] is not None else None
        lat, lon = r["ZLATITUDE"], r["ZLONGITUDE"]
        if lat is None or lat < -90 or (lat == 0 and lon == 0):
            lat = lon = None
        names = sorted({persons[pk]["ZFULLNAME"].strip() for pk in faces.get(r["Z_PK"], ())})
        mt, ms = moments.get(r["ZMOMENT"], (None, None))
        alb = albums.get(r["Z_PK"], [])
        body_parts = []
        if names:
            body_parts.append("Kişiler: " + ", ".join(names))
        if mt or ms:
            body_parts.append("Yer: " + " · ".join(x for x in (mt, ms) if x))
        if alb:
            body_parts.append("Albüm: " + ", ".join(alb))
        if isinstance(r["descr"], str) and r["descr"].strip():
            body_parts.append(r["descr"].strip())
        kind = "video" if r["ZKIND"] == 1 else "photo"
        eid = batch.event(
            r["ZUUID"], kind, taken, tz_offset_min=off, title=r["fname"],
            body="\n".join(body_parts) or None, lat=lat, lon=lon,
            payload={"moment": mt, "moment_sub": ms, "albums": alb or None, "people": names or None,
                     "uti": r["uti"], "orig_size": r["fsize"], "tz": r["tzname"],
                     "duration_s": r["ZDURATION"] or None})
        batch.tag(eid, SUBTYPE_TAG.get(r["ZKINDSUBTYPE"]), "favorite" if r["ZFAVORITE"] else None,
                  "hidden" if r["ZHIDDEN"] else None, *[f"album:{a}" for a in alb])
        for pk in faces.get(r["Z_PK"], ()):
            p = persons[pk]
            pid = batch.person(p["ZPERSONUUID"], p["ZFULLNAME"].strip())
            batch.link_person(eid, pid, "in_photo")

        # --- disk eşleme ---
        if not r["fname"]:
            stats["no_filename"] += 1
            continue
        stem, ext, _, _ = parse_name(r["fname"])
        by_stem[stem].append((eid, taken, r["fname"], r["ZWIDTH"]))
        cands = disk.get(stem, [])
        matched = False
        for role in ("original", "adjusted", "live"):
            pool = [f for f in cands if f.role == role and not f.used
                    and (role != "original" or f.ext == ext)]
            if not pool:
                continue
            best = min(pool, key=lambda f: _score(f, taken, r["fsize"]))
            # zaman bilgisi varken çok uzak eşleşmeyi reddet (aynı adlı başka asset olabilir)
            if best.taken is not None and abs((best.taken - taken).total_seconds()) > 36 * 3600:
                continue
            best.used = True
            batch.media(eid, best.path, role, original_filename=r["fname"],
                        width=r["ZWIDTH"], height=r["ZHEIGHT"],
                        duration_s=r["ZDURATION"] or None)
            matched = matched or role == "original"
            stats[f"file_{role}"] += 1
        stats["matched" if matched else "unmatched"] += 1

    # --- ikinci geçiş: aynı adlı, aynı anda çekilmiş artık dosyalar = aynı asset'in diğer
    # sürümleri (farklı uzantı ya da boyut son ekiyle inmiş türev) → role 'alt'
    for stem, files in disk.items():
        for f in files:
            if f.used or f.taken is None or stem not in by_stem:
                continue
            eid, taken, fname, _ = min(by_stem[stem], key=lambda x: abs((x[1] - f.taken).total_seconds()))
            if abs((taken - f.taken).total_seconds()) <= 5:
                f.used = True
                batch.media(eid, f.path, "alt", original_filename=fname)
                stats["file_alt"] += 1

    stats["events"] = batch.count
    stats["people"] = len({pk for s in faces.values() for pk in s})
    stats["disk_files"] = sum(len(v) for v in disk.values())
    stats["disk_unused"] = sum(1 for v in disk.values() for f in v if not f.used)
    return dict(stats)
