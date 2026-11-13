"""Concurrency + crash recovery guarantees."""
from __future__ import annotations

import threading
import time

from declarant.common import connect
from declarant.worker import engine


def test_claim_is_atomic(declarant, imported):
    decl_id = imported["report"]["invoices"][0]["declaration_id"]
    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        results.append(engine.claim(decl_id))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1, results
    conn = connect()
    try:
        assert conn.execute("SELECT status FROM declarations WHERE id=?",
                            (decl_id,)).fetchone()["status"] == "recording"
    finally:
        conn.close()


def test_recover_stale_releases_recording_declaration(declarant, imported):
    decl_id = imported["report"]["invoices"][0]["declaration_id"]
    conn = connect()
    try:
        conn.execute("UPDATE declarations SET status='recording', "
                     "updated_at='2020-01-01 00:00:00' WHERE id=?", (decl_id,))
        conn.execute("UPDATE jobs SET status='running', updated_at='2020-01-01 00:00:00' "
                     "WHERE declaration_id=?", (decl_id,))
        conn.commit()
    finally:
        conn.close()
    released = engine.recover_stale(minutes=5)
    assert released == 1
    conn = connect()
    try:
        assert conn.execute("SELECT status FROM declarations WHERE id=?",
                            (decl_id,)).fetchone()["status"] == "validated"
        assert conn.execute("SELECT status FROM jobs WHERE declaration_id=?",
                            (decl_id,)).fetchone()["status"] == "pending"
    finally:
        conn.close()


def test_recover_stale_leaves_active_worker_alone(declarant, imported):
    decl_id = imported["report"]["invoices"][0]["declaration_id"]
    assert engine.claim(decl_id)
    # claim() just refreshed timestamps, so nothing should be released
    assert engine.recover_stale(minutes=5) == 0
    conn = connect()
    try:
        assert conn.execute("SELECT status FROM declarations WHERE id=?",
                            (decl_id,)).fetchone()["status"] == "recording"
    finally:
        conn.close()
