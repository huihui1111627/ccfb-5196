"""Shared fixtures: temp data dirs, isolated DBs, mock server, sample files."""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("samples")
    import subprocess
    subprocess.run([sys.executable, str(ROOT / "scripts" / "make_test_data.py"),
                    "--out", str(out)], cwd=str(ROOT), check=True,
                   env={**os.environ, "PYTHONPATH": str(ROOT)})
    return out


@pytest.fixture()
def broker_env(tmp_path, monkeypatch):
    """Point app.config paths at a throw-away directory."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("BROKER_DATA_DIR", str(data))
    monkeypatch.setenv("BROKER_DB", str(data / "broker.db"))
    monkeypatch.setenv("BROKER_SHOTS", str(data / "screenshots"))
    monkeypatch.setenv("BROKER_EXPORTS", str(data / "exports"))
    # Reload modules that capture paths at import time.
    import importlib
    import app.config
    import app.db
    importlib.reload(app.config)
    importlib.reload(app.db)
    return data


@pytest.fixture()
def db(broker_env):
    from app.db import Database
    database = Database()
    return database


@pytest.fixture()
def mock_server(tmp_path):
    """Run the mock customs portal in-process on a free port."""
    from mock_system.app import create_app
    db_path = tmp_path / "mock.db"
    port = _free_port()
    flask_app = create_app(db_path=str(db_path), rejection_rate=0.0,
                           field_delay_ms=5, session_seconds=1200, testing=True,
                           debug_enabled=True)
    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", port, flask_app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.4)
    yield {"url": f"http://127.0.0.1:{port}", "app": flask_app,
           "db_path": db_path, "port": port}
    server.shutdown()
    thread.join(timeout=3)


@pytest.fixture()
def mock_client(mock_server):
    client = mock_server["app"].test_client()
    client.post("/login", data={"username": "admin", "password": "admin123"})
    return client
