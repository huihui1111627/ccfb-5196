"""Standalone runner process (also invoked by the console in a subprocess).

Usage:
    python -m app.runner.main --batch 1 --headless
    python -m app.runner.main --docs 3,4 --no-verify
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from app.config import DATA_DIR, SCREENSHOT_DIR
from app.db import Database, now_iso
from app.runner import RunConfig, Runner, install_signal_handlers, reap_stale_runs
from app.runner.portal import MockPortal


def build_portal(settings: dict, run_dir=None) -> MockPortal:
    portal = MockPortal(
        base_url=settings["mock_base_url"],
        username=settings["mock_username"],
        password=settings["mock_password"],
        headless=settings.get("headless", "1") == "1",
        slow_mo_ms=int(settings.get("slow_mo_ms", "0") or 0),
        screenshot_dir=SCREENSHOT_DIR,
    )
    if run_dir:
        portal.set_run_dir(run_dir)
    return portal


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int)
    ap.add_argument("--docs", type=str, help="comma separated document ids")
    ap.add_argument("--headless", action="store_true", default=None)
    ap.add_argument("--headed", dest="headless", action="store_false")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--run-id", type=int)
    args = ap.parse_args()

    install_signal_handlers()
    db = Database()
    settings = db.get_settings()
    if args.headless is not None:
        settings["headless"] = "1" if args.headless else "0"
    config = RunConfig(
        batch_id=args.batch,
        document_ids=[int(x) for x in args.docs.split(",") if x.strip()] if args.docs else None,
        headless=settings.get("headless", "1") == "1",
        slow_mo_ms=int(settings.get("slow_mo_ms", "0") or 0),
        max_resubmit=int(settings.get("max_resubmit", "3")),
        verify_after_submit=(not args.no_verify) and settings.get("verify_after_submit", "1") == "1",
        settle_seconds_after_uncertain=float(
            settings.get("settle_seconds_after_uncertain", "2")),
        probe_attempts=int(settings.get("probe_attempts", "2")),
    )
    run_id = args.run_id
    if run_id is None:
        run_id = db.execute(
            "INSERT INTO runs(batch_id, started_at, status, mode, claimant, heartbeat_at, stats_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (config.batch_id, now_iso(), "running", "auto", "pending", now_iso(), "{}"),
        )
    reap_stale_runs(db)
    run_dir = SCREENSHOT_DIR / f"run_{run_id}"
    portal = build_portal(settings, run_dir)
    portal.start()
    runner = Runner(db, portal, config, settings, run_id=run_id)
    db.execute("UPDATE runs SET claimant=? WHERE id=?", (runner.claimant, run_id))
    try:
        stats = runner.run()
    finally:
        portal.close()
        db.set_setting("runner_active", "")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
