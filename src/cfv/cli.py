"""`cfv` komut satırı."""

from __future__ import annotations

import argparse
from pathlib import Path

from cfv import db


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="cfv", description="Central Finite Vault")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init-db", help="Zaman çizelgesi veritabanını oluştur/güncelle")
    p_init.add_argument("--db", type=Path, default=None, help="Varsayılan: data/processed/cfv.sqlite")

    args = parser.parse_args(argv)
    if args.cmd == "init-db":
        print(db.init_db(args.db))


if __name__ == "__main__":
    main()
