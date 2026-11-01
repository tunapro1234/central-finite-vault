"""Hiyerarşik (recursive) raporlar.

Kök = tüm veri. Bir düğümün ham verisi LLM sınırına (LIMIT) sığıyorsa düğüm YAPRAKTIR: rapor ham
veriden yazılır. Sığmıyorsa alt katmana iner ve rapor çocukların raporlarından yazılır:

    kök → yıl → ay → hafta (aya kırpılmış ISO hafta) → gün → 3 saat → 1 saat (her zaman yaprak)

Dosyalar: data/processed/reports/<yol>/index.md, ör. reports/2026/10/W43/22/09-12/index.md.
Her raporun frontmatter'ında `sig:` vardır (yaprak/bölünmüş + kayıt sayısı + toplam uzunluk).
Bir aralığa veri eklenince/değişince o aralığın imzası değişir; değişen düğüm ve onu içeren tüm
üst düğümler (gün → hafta → ay → yıl → kök) bayatlar ve aşağıdan yukarı yeniden üretilir.
Etkilenmeyen kardeşlerin raporları olduğu gibi kalır.

    cfv report jobs [--under 2026/10]   # bayat düğümler, çocuklar önce
    cfv report input 2026/10/W43/22     # bir düğümün LLM girdisi (ajan bunu okur)
    cfv report send                     # cfv-editor'a "işleri yap" mesajı
"""

from __future__ import annotations

import bisect
import datetime as dt
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from cfv import db, days

LIMIT = 60_000  # karakter (~20k token): bir girdinin üst sınırı
EDITOR = "cfv-editor"
REPORT_KINDS = ("day_report", "report")  # raporlar kendi girdilerine karışmasın
LEVELS = ["kök", "yıl", "ay", "hafta", "gün", "3 saat", "1 saat"]

# bir event'in döküm metninde kaplayacağı yaklaşık karakter (çok günlü aralıklar için tahmin)
EST = """CASE kind
  WHEN 'whatsapp' THEN min(length(coalesce(body, '')), 220) + 30
  WHEN 'email' THEN 200
  WHEN 'calendar' THEN length(coalesce(title, '')) + 70
  WHEN 'note' THEN min(length(coalesce(body, '')), 600) + length(coalesce(title, '')) + 10
  WHEN 'todo' THEN length(coalesce(title, '')) + 25
  WHEN 'photo' THEN 4 WHEN 'video' THEN 4
  ELSE 60 END"""
DAY_OVERHEAD = 300


def root_dir() -> Path:
    return db.data_dir() / "processed" / "reports"


@dataclass(frozen=True)
class Node:
    path: tuple[str, ...]
    start: dt.date
    end: dt.date  # hariç
    hours: tuple[int, int] | None = None

    @property
    def level(self) -> str:
        return LEVELS[len(self.path)]

    @property
    def name(self) -> str:
        return "/".join(self.path) or "kök"

    @property
    def file(self) -> Path:
        return root_dir().joinpath(*self.path, "index.md")

    @property
    def label(self) -> str:
        last = self.end - dt.timedelta(days=1)
        rng = self.start.isoformat() if self.start == last else f"{self.start} → {last}"
        return rng + (f" saat {self.hours[0]:02d}-{self.hours[1]:02d}" if self.hours else "")


class Data:
    """Tüm event'lerin (rapor hariç) gün → [(yerel saat|None, tahmini boy, gerçek boy)] özeti."""

    def __init__(self, conn):
        self.conn = conn
        self.by_day: dict[str, list[tuple[float | None, int, int]]] = defaultdict(list)
        q = ",".join("?" * len(REPORT_KINDS))
        for d, s, off, tq, est, ln in conn.execute(f"""
                SELECT local_date, start_utc, tz_offset_min, time_quality, {EST},
                       length(coalesce(title, '')) + length(coalesce(body, '')) + length(coalesce(payload, ''))
                FROM event WHERE kind NOT IN ({q}) AND local_date < ?""",
                (*REPORT_KINDS, dt.date.today().isoformat())):  # yalnız biten günler
            h = None if tq in ("day", "week") else days._local_hour((None, None, s, None, off))
            self.by_day[d].append((h, est, ln))
        self.days = sorted(self.by_day)
        self._fits: dict[Node, bool] = {}

    def days_in(self, n: Node) -> list[str]:
        a = bisect.bisect_left(self.days, n.start.isoformat())
        b = bisect.bisect_left(self.days, n.end.isoformat())
        return self.days[a:b]

    def items(self, n: Node):
        for d in self.days_in(n):
            for x in self.by_day[d]:
                if n.hours is None or (x[0] is not None and n.hours[0] <= x[0] < n.hours[1]):
                    yield x

    def root(self) -> Node:
        return Node((), dt.date.fromisoformat(self.days[0]), dt.date.fromisoformat(self.days[-1]) + dt.timedelta(1))

    def raw_text(self, n: Node) -> str:
        if len(n.path) >= 4:  # gün ve altı
            return days.dossier(self.conn, n.start, hours=n.hours)
        return "\n\n".join(days.dossier(self.conn, dt.date.fromisoformat(d), week_todos=False)
                           for d in self.days_in(n))

    def fits(self, n: Node) -> bool:
        if n not in self._fits:
            est = sum(x[1] for x in self.items(n)) + DAY_OVERHEAD * len(self.days_in(n))
            if len(n.path) == 6 or est < LIMIT * 0.5:
                ok = True
            elif est > LIMIT * 2:
                ok = False
            else:  # sınıra yakın: gerçek metni ölç
                ok = len(self.raw_text(n)) < LIMIT
            self._fits[n] = ok
        return self._fits[n]

    def children(self, n: Node) -> list[Node]:
        k, out = len(n.path), []
        if k == 0:
            for y in range(n.start.year, n.end.year + 1):
                out.append(Node((f"{y}",), dt.date(y, 1, 1), dt.date(y + 1, 1, 1)))
        elif k == 1:
            y = n.start.year
            for m in range(1, 13):
                out.append(Node((*n.path, f"{m:02d}"), dt.date(y, m, 1),
                                dt.date(y + (m == 12), m % 12 + 1, 1)))
        elif k == 2:
            weeks = defaultdict(list)
            d = n.start
            while d < n.end:
                weeks[d.isocalendar()[1]].append(d)
                d += dt.timedelta(1)
            for w, ds in sorted(weeks.items(), key=lambda kv: kv[1][0]):
                out.append(Node((*n.path, f"W{w:02d}"), ds[0], ds[-1] + dt.timedelta(1)))
        elif k == 3:
            d = n.start
            while d < n.end:
                out.append(Node((*n.path, f"{d.day:02d}"), d, d + dt.timedelta(1)))
                d += dt.timedelta(1)
        elif k in (4, 5):
            step = 3 if k == 4 else 1
            h0, h1 = n.hours or (0, 24)
            for h in range(h0, h1, step):
                out.append(Node((*n.path, f"{h:02d}-{h + step:02d}"), n.start, n.end, (h, h + step)))
        return [c for c in out if next(self.items(c), None) is not None]

    def split(self, n: Node) -> list[Node]:
        """Sığmıyorsa çocuklar, sığıyorsa [] (yaprak)."""
        return [] if self.fits(n) else self.children(n)

    def sig(self, n: Node) -> str:
        xs = list(self.items(n))
        return f"{'bölünmüş' if self.split(n) else 'yaprak'}:{len(xs)}:{sum(x[2] for x in xs)}"

    def node(self, name: str) -> Node:
        """'2026/10/W43/22/09-12' → Node."""
        n = self.root()
        for part in [p for p in name.strip("/").split("/") if p and p != "kök"]:
            match = [c for c in self.children(n) if c.path[-1] == part]
            if not match:
                raise KeyError(f"{name}: '{part}' yok (o aralıkta veri yok)")
            n = match[0]
        return n


def _sig_of(path: Path) -> str | None:
    try:
        m = re.search(r"^sig:\s*(.+?)\s*$", path.read_text(), re.M)
    except FileNotFoundError:
        return None
    return m.group(1) if m else None


def walk(data: Data, n: Node | None = None):
    """Çocuklar önce (post-order): (düğüm, çocuklar, imza)."""
    n = n or data.root()
    kids = data.split(n)
    for c in kids:
        yield from walk(data, c)
    yield n, kids, data.sig(n)


def jobs(conn, under: str | None = None) -> list[tuple[Node, bool]]:
    data = Data(conn)
    start = data.node(under) if under else None
    return [(n, not kids) for n, kids, sig in walk(data, start) if _sig_of(n.file) != sig]


def _previous(n: Node) -> Path | None:
    """Aynı katmandaki, bu düğümden önce biten en son rapor (süreklilik için)."""
    depth = len(n.path)
    if depth == 0:
        return None
    best = None
    for f in root_dir().glob("/".join(["*"] * depth) + "/index.md"):
        m = re.search(r"^range:\s*(\S+)\s*(?:→\s*(\S+))?", f.read_text(), re.M)
        if not m:
            continue
        end = dt.date.fromisoformat(m.group(2) or m.group(1))
        if end < n.start and (best is None or end > best[0]):
            best = (end, f)
    return best[1] if best else None


def input_text(conn, name: str) -> str:
    data = Data(conn)
    n = data.node(name)
    kids = data.split(n)
    head = [f"# İŞ: {n.level} raporu — {n.name} ({n.label})",
            f"Yaz: {n.file}",
            f"Frontmatter'a aynen koy → level: {n.level} | node: {n.name} | range: {n.label} | sig: {data.sig(n)}",
            "Biçim: src/cfv/prompts/report.md · Kişisel bağlam: data/config/day-context.md", ""]
    prev = _previous(n)
    if prev:
        head += [f"=== ÖNCEKİ {n.level.upper()} RAPORU ({prev.parent.relative_to(root_dir())}) — süreklilik için ===",
                 prev.read_text().strip(), ""]
    if not kids:
        body = data.raw_text(n)
        title = "HAM VERİ"
    else:
        parts = []
        if len(n.path) == 4:  # bölünmüş gün: saatsiz kayıtlar (takvim, ödev, sağlık) iskelette
            parts.append(days.dossier(conn, n.start, skeleton=True))
        for c in kids:
            parts.append(f"--- {c.level}: {c.name} ({c.label}) ---\n"
                         + (c.file.read_text().strip() if c.file.exists() else "(RAPOR YOK — önce bu yazılmalı)"))
        body = "\n\n".join(parts)
        title = ("GÜNÜN İSKELETİ + " if len(n.path) == 4 else "") + f"ALT RAPORLAR ({len(kids)} {kids[0].level})"
    if len(body) > LIMIT:
        body = body[:LIMIT] + f"\n\n… (sınır: {len(body) - LIMIT} karakter kesildi)"
    return "\n".join(head) + f"=== {title} ===\n{body}\n"


def day_map() -> dict[dt.date, Path]:
    """Her gün için onu kapsayan en derin (saat parçası olmayan) rapor."""
    best: dict[dt.date, tuple[int, Path]] = {}
    for f in root_dir().rglob("index.md"):
        m = re.search(r"^range:\s*(\S+)\s*(?:→\s*(\S+))?\s*$", f.read_text(), re.M)
        if not m:
            continue
        a = dt.date.fromisoformat(m.group(1))
        b = dt.date.fromisoformat(m.group(2) or m.group(1))
        depth = len(f.relative_to(root_dir()).parts)
        d = a
        while d <= b:
            if d not in best or depth > best[d][0]:
                best[d] = (depth, f)
            d += dt.timedelta(1)
    return {d: f for d, (_, f) in best.items()}


def send(under: str | None = None) -> str:
    msg = ("İş var: `uv run cfv report jobs" + (f" --under {under}" if under else "")
           + "` listesini sırayla yap (src/cfv/prompts/editor.md'yi oku).")
    p = subprocess.run(["bp", "msg", EDITOR, msg], capture_output=True, text=True)
    out = (p.stdout or p.stderr).strip()
    return out.splitlines()[-1] if out else msg
