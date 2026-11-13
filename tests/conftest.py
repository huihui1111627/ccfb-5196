"""Shared pytest fixtures: an isolated data dir + in-process mock customs server."""
from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("DECLARANT_DATA_DIR", str(d))
    # reset cached module-level paths if any code copied values at import time
    from declarant import common
    monkeypatch.setattr(common, "_catalog_cache", None)
    return d


@pytest.fixture()
def mock_server(data_dir, monkeypatch):
    from werkzeug.serving import make_server

    port = free_port()
    monkeypatch.setenv("DECLARANT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("MOCK_REJECT_RATE", "0")
    from mock_system import app as mock_app
    mock_app.REJECT_RATE = 0.0
    mock_app.REJECT_RATE_REVISION = 0.0
    # the mock caches schema state per-process; each test gets a fresh data dir
    mock_app._schema_ready = False
    mock_app.init_mock_db()
    server = make_server("127.0.0.1", port, mock_app.app, threaded=True)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            import urllib.request
            urllib.request.urlopen(base + "/health", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    yield {"base_url": base, "port": port, "app": mock_app.app}
    server.shutdown()
    server.server_close()


@pytest.fixture()
def declarant(mock_server, data_dir, monkeypatch):
    from declarant.common import connect, init_db, seed_settings
    init_db()
    conn = connect()
    seed_settings(conn)
    from declarant.common import set_setting
    set_setting(conn, "mock_base_url", mock_server["base_url"])
    conn.commit()
    conn.close()
    return mock_server


@pytest.fixture()
def samples_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture()
def imported(declarant, samples_dir):
    """Import the multi-invoice Excel sample and return the parsed report."""
    from declarant.importer.service import import_file
    return import_file(samples_dir / "invoice_CI-202610-0002.pdf", "sample.pdf")


@pytest.fixture()
def mock_db_rows(declarant):
    from declarant.common import connect_mock

    def _rows():
        conn = connect_mock()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT id, client_ref, status, decl_no FROM declarations ORDER BY id").fetchall()]
        finally:
            conn.close()
    return _rows
