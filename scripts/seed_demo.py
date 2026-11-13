"""Seed the console with demo data on first start (idempotent)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from declarant.common import connect, init_db
from declarant.importer.service import import_file


def main() -> None:
    init_db()
    conn = connect()
    try:
        if conn.execute("SELECT COUNT(*) n FROM declarations").fetchone()["n"] > 0:
            return
    finally:
        conn.close()
    samples = Path(__file__).resolve().parent.parent / "samples"
    for f in sorted(samples.glob("*.xlsx")) + sorted(samples.glob("*.pdf")):
        res = import_file(f, f.name)
        print(f"seeded {f.name}: imported={res.get('imported')} duplicate={res.get('duplicate')}")


if __name__ == "__main__":
    main()
