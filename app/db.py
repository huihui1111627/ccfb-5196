"""SQLite schema and access helpers for the broker console.

The central safety property lives in the `submissions` table: every attempt to
submit one *source document* (one 票) is claimed by a unique row keyed on
`doc_id`, and the row carries a `state` that is only advanced with an explicit,
committed transaction.  A submission is never sent unless this process holds the
claim and the state is `submitting`.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.config import DB_PATH, ensure_dirs

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    source_filename TEXT NOT NULL,
    source_kind TEXT NOT NULL,            -- excel | pdf
    source_sha256 TEXT NOT NULL,
    source_path TEXT NOT NULL,
    doc_count INTEGER NOT NULL DEFAULT 0,
    imported_at TEXT NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER REFERENCES batches(id),
    external_ref TEXT NOT NULL,            -- invoice no / contract no; one 票
    source_kind TEXT NOT NULL,
    source_file TEXT NOT NULL,
    page_ref TEXT,                         -- e.g. page range or sheet row
    sequence_no INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    -- pending | validating | ready | needs_fix | queued | submitting
    -- | submitted | rejected | revision | verified | verified_mismatch | failed | cancelled
    header_json TEXT NOT NULL,
    items_json TEXT NOT NULL,
    validation_json TEXT,
    validation_blocking INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT NOT NULL,              -- hash of canonical header+items
    compare_json TEXT,
    rejection_json TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    revision_of INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(batch_id, external_ref)
);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_documents_ref ON documents(external_ref);

CREATE TABLE IF NOT EXISTS document_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    item_no INTEGER NOT NULL,
    hs_code TEXT NOT NULL,
    goods_name TEXT NOT NULL,
    spec_model TEXT,
    quantity TEXT NOT NULL,
    unit TEXT NOT NULL,
    unit_price TEXT NOT NULL,
    total_price TEXT NOT NULL,
    currency TEXT,
    origin_country TEXT,
    destination_country TEXT,
    elements_json TEXT NOT NULL,
    UNIQUE(document_id, item_no)
);

CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL UNIQUE REFERENCES documents(id),  -- one fence per 票
    state TEXT NOT NULL DEFAULT 'none',
    -- none (never claimed) | submitting (claimant is inside submit) | uncertain
    -- | accepted | rejected
    claimant TEXT,                         -- process/run token holding the claim
    attempt_seq INTEGER NOT NULL DEFAULT 0,
    payload_sha TEXT NOT NULL,
    drafted_at TEXT,
    submitted_at TEXT,
    settled_at TEXT,
    response_kind TEXT,                    -- accepted | rejected | unknown
    declaration_no TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS submission_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,                    -- draft/submit/probe
    claimant TEXT,
    outcome TEXT NOT NULL,                 -- started | accepted | rejected | uncertain | skipped
    declaration_no TEXT,
    detail TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(doc_id, seq, kind)
);
CREATE INDEX IF NOT EXISTS idx_sattempts_doc ON submission_attempts(doc_id);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',   -- running | finished | aborted | crashed_recovered
    mode TEXT NOT NULL DEFAULT 'auto',
    claimant TEXT NOT NULL,
    heartbeat_at TEXT,
    stats_json TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS run_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    doc_id INTEGER,
    item_no INTEGER,
    step TEXT NOT NULL,
    status TEXT NOT NULL,                  -- ok | info | warning | error
    message TEXT,
    screenshot_path TEXT,
    url TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_steps_run ON run_steps(run_id);
CREATE INDEX IF NOT EXISTS idx_steps_doc ON run_steps(doc_id);

CREATE TABLE IF NOT EXISTS rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id INTEGER NOT NULL UNIQUE REFERENCES documents(id),
    reason_code TEXT,
    reason_text TEXT,
    category TEXT,
    hint TEXT,
    source TEXT NOT NULL DEFAULT 'mock',   -- mock | operator
    status TEXT NOT NULL DEFAULT 'open',   -- open | handling | resolved | waived
    handler TEXT,
    resolution_note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_stats (
    day TEXT PRIMARY KEY,
    submitted INTEGER NOT NULL DEFAULT 0,
    accepted INTEGER NOT NULL DEFAULT 0,
    rejected INTEGER NOT NULL DEFAULT 0,
    mismatched INTEGER NOT NULL DEFAULT 0,
    items_entered INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_local = threading.local()


def _connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")   # durability matters for the fence
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class Database:
    """Small wrapper.  One connection per thread; explicit transactions."""

    def __init__(self, path: str | Path | None = None):
        if path is None:
            from app import config as _config
            path = _config.DB_PATH
        self.path = str(path)
        ensure_dirs()
        self.init_schema()

    def conn(self) -> sqlite3.Connection:
        key = f"conn_{self.path}"
        conn = getattr(_local, key, None)
        if conn is None or getattr(conn, "_closed", False):
            conn = _connect(self.path)
            setattr(_local, key, conn)
            return conn
        try:
            conn.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            conn = _connect(self.path)
            setattr(_local, key, conn)
        return conn

    def init_schema(self) -> None:
        conn = _connect(self.path)
        try:
            conn.executescript(SCHEMA)
        finally:
            conn.close()

    @contextmanager
    def tx(self, immediate: bool = True):
        """Exclusive transaction.  Use `immediate=True` for writes that must
        serialise against other processes (the submission fence in particular)."""
        conn = self.conn()
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return list(self.conn().execute(sql, params))

    def one(self, sql: str, params: tuple | dict = ()):
        return self.conn().execute(sql, params).fetchone()

    def scalar(self, sql: str, params: tuple | dict = ()):
        row = self.one(sql, params)
        return row[0] if row else None

    def execute(self, sql: str, params: tuple | dict = ()) -> int:
        cur = self.conn().execute(sql, params)
        return cur.lastrowid

    # -- settings ---------------------------------------------------------
    def get_settings(self) -> dict:
        from app.config import DEFAULT_SETTINGS

        out = dict(DEFAULT_SETTINGS)
        for row in self.query("SELECT key, value FROM settings"):
            out[row["key"]] = row["value"]
        return out

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO settings(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, str(value), now_iso()),
        )


def json_dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def json_loads(text):
    if not text:
        return None
    if isinstance(text, (dict, list)):
        return text
    return json.loads(text)
