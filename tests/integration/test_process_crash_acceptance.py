from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "tools" / "s6b4c06_crash_acceptance.py"


def test_process_crash_and_cancellation_acceptance(tmp_path: Path) -> None:
    completed = subprocess.run(
        [sys.executable, str(HARNESS), "--run-dir", str(tmp_path / "s6b4c06-run")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    summary = json.loads(completed.stdout)
    assert summary["PROCESS_SIGKILL_PROOF"] == "PASS"
    assert summary["STORE_PRECOMMIT_ROLLBACK"] == "PASS"
    assert summary["REVERIFY_PRECOMMIT_ROLLBACK"] == "PASS"
    assert summary["REPLACE_PRECOMMIT_ROLLBACK"] == "PASS"
    assert summary["POSTCOMMIT_DURABILITY"] == "PASS"
    assert summary["POSTCOMMIT_IDEMPOTENT_REPLAY"] == "PASS"
    assert summary["EMBEDDING_INFLIGHT_PROCESS_KILL"] == "PASS"
    assert summary["ASYNC_CANCELLATION_SEMANTICS"] == "PASS"
    assert summary["SQLITE_QUICK_CHECK_AFTER_CRASHES"] == "PASS"
    assert summary["STALE_WRITER_LOCKS"] == 0
    assert summary["CURRENT_LINEAGE_FORKS"] == 0
    assert summary["PARTIAL_ADAPTER_ROWS"] == 0
    assert summary["PARTIAL_NATIVE_ROWS"] == 0
    assert summary["PARTIAL_VECTOR_ROWS"] == 0
    assert summary["ACTUAL_CODEX_OR_HERMES_PROCESS_KILLED"] == "NO"
