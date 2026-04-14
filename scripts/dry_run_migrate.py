"""
Step 12 dry-run — schema migration against a local sqlite DB.

Usage:
    python scripts/dry_run_migrate.py [--db PATH] [--reset]

Creates / migrates the Option C schema (§4) via backend.schema.init_db,
then prints the resulting table + index list so the operator can eyeball
that signals_v3 / evaluations / trade_outcomes / positions / signals /
eval_results all exist, and the bar_close_ms UNIQUE index is in place.
"""
import argparse
import os
import sqlite3
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from backend.schema import (
    init_db,
    migrate_add_bar_close_ms_to_signals_v3,
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.path.join(_ROOT, "dry_run.db"),
                        help="SQLite DB path")
    parser.add_argument("--reset", action="store_true",
                        help="Delete the DB file first, then migrate")
    args = parser.parse_args()

    if args.reset and os.path.exists(args.db):
        os.remove(args.db)
        print(f"[reset] removed {args.db}")

    with sqlite3.connect(args.db) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys = ON")
        init_db(conn)
        migrate_add_bar_close_ms_to_signals_v3(conn)

        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]
        indexes = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]

    print(f"[ok] schema applied to {args.db}")
    print(f"  tables  ({len(tables)}): {tables}")
    print(f"  indexes ({len(indexes)}): {indexes}")


if __name__ == "__main__":
    main()
