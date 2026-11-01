"""`cfv` komut satırı."""

from __future__ import annotations

import argparse
import importlib
import json
import time
from pathlib import Path

from cfv import db

# ad → modül (cfv.sources.<modül>.ingest(conn))
SOURCES = {
    "photos": "photos",
    "notes": "notes",
    "calendar-ios": "calendar_ios",
    "calendar-ics": "calendar_ics",
    "whatsapp": "whatsapp",
    "zepp": "zepp",
}


def cmd_ingest(args):
    names = list(SOURCES) if args.source == ["all"] else args.source
    conn = db.connect(args.db)
    conn.executescript(db.schema_sql())
    for name in names:
        mod = importlib.import_module(f"cfv.sources.{SOURCES[name]}")
        t = time.time()
        try:
            with conn:
                stats = mod.ingest(conn)
        except FileNotFoundError as e:
            print(f"[{name}] ATLANDI — ham veri yok: {e}")
            continue
        print(f"[{name}] {time.time() - t:.1f} sn  {json.dumps(stats, ensure_ascii=False)}")


def cmd_tree(args):
    from cfv import tree
    stats = tree.build(db.connect(args.db), args.out)
    print(json.dumps(stats, ensure_ascii=False))


def cmd_search(args):
    from cfv import search
    conn = db.connect(args.db)
    if args.by_week:
        for w in search.weeks(conn, args.query, limit=args.limit):
            print(f"{w['week']}  {w['hits']:4d} eşleşme  {w['kinds']}")
            for h in w["samples"]:
                print(f"      {h['local_date']} [{h['kind']}] {h['snippet']}")
    else:
        for h in search.search(conn, args.query, limit=args.limit, kinds=args.kind):
            title = h["title"] or ""
            extra = "" if h["snippet"] in ("", title) else f" — {h['snippet']}"
            print(f"{h['local_date']} [{h['kind']}] {title}{extra}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="cfv", description="Central Finite Vault")
    parser.add_argument("--db", type=Path, default=None, help="Varsayılan: data/processed/cfv.sqlite")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="Zaman çizelgesi veritabanını oluştur/güncelle")

    p = sub.add_parser("ingest", help="Kaynak(lar)ı içe aktar")
    p.add_argument("source", nargs="+", choices=[*SOURCES, "all"])

    p = sub.add_parser("tree", help="Tarih bazlı klasör ağacını üret (sene/ay/hafta)")
    p.add_argument("--out", type=Path, default=None, help="Varsayılan: data/processed/timeline")

    p = sub.add_parser("search", help="Tam metin arama")
    p.add_argument("query")
    p.add_argument("--kind", action="append", help="note, todo, whatsapp, photo, calendar ...")
    p.add_argument("--by-week", action="store_true", help="Sonuçları haftalara grupla")
    p.add_argument("--limit", type=int, default=20)

    args = parser.parse_args(argv)
    if args.cmd == "init-db":
        print(db.init_db(args.db))
    elif args.cmd == "ingest":
        cmd_ingest(args)
    elif args.cmd == "tree":
        cmd_tree(args)
    elif args.cmd == "search":
        cmd_search(args)


if __name__ == "__main__":
    main()
