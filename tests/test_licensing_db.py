"""Pytest wrapper for the SAMePOS licensing-core DB test harness.

Runs db/tests/run_db_tests.sh, which builds a disposable PostgreSQL cluster,
applies the migration and the standalone schema, runs the behavioural
assertions, checks migration/standalone equivalence, idempotency, and rollback.

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


def test_licensing_db_end_to_end():
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
    assert proc.returncode == 0, f"licensing DB harness failed:\n{output}"
    assert "DB TESTS: ALL PASSED" in proc.stdout, f"unexpected harness output:\n{output}"
