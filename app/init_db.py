"""Initialise the broker DB (and optionally the mock DB) without the web UI."""

from __future__ import annotations

import sys

from app.db import Database
from app.services.state import _system_run

if __name__ == "__main__":
    db = Database()
    _system_run(db)
    print(f"Broker DB ready at {db.path}")
    if "--with-mock" in sys.argv:
        from mock_system.app import init_db
    print("Done")
