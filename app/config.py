"""Runtime configuration for the declaration-entry console."""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("BROKER_DATA_DIR", BASE_DIR / "data"))
DB_PATH = Path(os.environ.get("BROKER_DB", DATA_DIR / "broker.db"))
SCREENSHOT_DIR = Path(os.environ.get("BROKER_SHOTS", DATA_DIR / "screenshots"))
EXPORT_DIR = Path(os.environ.get("BROKER_EXPORTS", DATA_DIR / "exports"))

DEFAULT_SETTINGS = {
    "mock_base_url": os.environ.get("MOCK_BASE_URL", "http://127.0.0.1:5055"),
    "mock_username": os.environ.get("MOCK_USERNAME", "admin"),
    "mock_password": os.environ.get("MOCK_PASSWORD", "admin123"),
    "session_minutes": "20",
    "headless": "1",
    "slow_mo_ms": "0",
    "verify_after_submit": "1",
    "max_resubmit": "3",
    "reconcile_strict": "1",
    "settle_seconds_after_uncertain": "2",
    "probe_attempts": "2",
    "auto_start_runner": "0",
    "screenshot_all_steps": "1",
}


def ensure_dirs() -> None:
    for d in (DATA_DIR, SCREENSHOT_DIR, EXPORT_DIR):
        Path(d).mkdir(parents=True, exist_ok=True)
