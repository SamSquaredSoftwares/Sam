"""Pytest wrapper for the SAMePOS monetization-core DB assertions.

Runs db/tests/run_db_tests.sh (shared with the licensing core), which builds a
disposable PostgreSQL cluster, applies both migrations and both standalone
schemas, runs the behavioural assertions — including the monetization suite in
db/tests/behavior_monetization.sql (per-row usage charging, idempotent batch
replay, once-off charge confirmation, permanent machine binding, invoicing) —
and checks migration/standalone equivalence, idempotency, and rollback.

Skips (rather than fails) when PostgreSQL server binaries are unavailable, so
the suite still runs on machines without a local Postgres.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HARNESS = REPO_ROOT / "db" / "tests" / "run_db_tests.sh"

SKIP_EXIT = 77  # the harness exits 77 when it cannot find initdb/pg_ctl


def test_monetization_db_end_to_end():
    assert HARNESS.is_file(), f"DB test harness missing: {HARNESS}"
    proc = subprocess.run(
        ["bash", str(HARNESS)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = proc.stdout + "\n" + proc.stderr
    if proc.returncode == SKIP_EXIT:
        pytest.skip("PostgreSQL server binaries not available:\n" + output)
    assert proc.returncode == 0, f"DB harness failed:\n{output}"
    assert "ALL MONETIZATION ASSERTIONS PASSED" in proc.stdout, (
        f"monetization assertions did not run/pass:\n{output}"
    )
    assert "DB TESTS: ALL PASSED" in proc.stdout, f"unexpected harness output:\n{output}"
