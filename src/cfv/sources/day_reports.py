"""Gün raporları (data/processed/days/**/YYYY-MM-DD.md, cfv.days üretir) → 'day_report' event'leri.

Raporlar aranabilir olsun diye DB'ye girer: "ne zaman şu işle uğraştım" sorusu en iyi
buradan yanıtlanır (rapor, o günün kaynaklarını zaten birleştirmiş halde).
"""

from __future__ import annotations

import datetime as dt
import re

from cfv.days import days_dir
from cfv.ingest import Batch
from cfv.timeutil import DEFAULT_TZ

IMPORTER = "cfv.sources.day_reports@1"


def ingest(conn) -> dict:
    root = days_dir()
    files = sorted(root.glob("*/????-??-??.md"))
    if not files:
        raise FileNotFoundError(root)
    batch = Batch(conn, "day-reports", root, IMPORTER)
    for f in files:
        d = dt.date.fromisoformat(f.stem)
        text = f.read_text()
        m = re.search(r"^# .*?—\s*(.+)$", text, re.M)
        fm = re.search(r"^projects:\s*\[(.*?)\]", text, re.M)
        batch.event(f.stem, "day_report", dt.datetime.combine(d, dt.time(0), DEFAULT_TZ),
                    time_quality="day", day=f.stem, title=m.group(1).strip() if m else f.stem,
                    body=text.split("\n---\n", 1)[-1],
                    payload={"path": str(f), "projects": [p.strip() for p in fm.group(1).split(",") if p.strip()] if fm else []})
    return {"reports": len(files)}
