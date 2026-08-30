from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "s6b4c04_disposable_shared_store.py"


def _run(*args: object, env: dict[str, str] | None = None, timeout: int = 20) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), *(str(arg) for arg in args)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    return json.loads(completed.stdout)


def _actor_env(home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["HOME"] = str(home)
    return env


def test_disposable_process_clients_share_one_store_without_lost_update(tmp_path: Path) -> None:
    run_dir = tmp_path / "s6b4c04-run"
    codex_home = tmp_path / "codex-home"
    hermes_home = tmp_path / "hermes-home"
    codex_home.mkdir()
    hermes_home.mkdir()

    prepared = _run("prepare", "--run-dir", run_dir)
    assert prepared["prepared"] is True
    assert prepared["expected_revision"] == 1

    actors: list[tuple[str, subprocess.Popen[str]]] = []
    for actor, home in (("codex-disposable", codex_home), ("hermes-disposable", hermes_home)):
        actors.append(
            (
                actor,
                subprocess.Popen(
                    [
                        sys.executable,
                        str(SCRIPT),
                        "actor",
                        "--run-dir",
                        str(run_dir),
                        "--actor",
                        actor,
                    ],
                    cwd=ROOT,
                    env=_actor_env(home),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ),
            )
        )

    actor_payloads: dict[str, dict[str, object]] = {}
    for actor, process in actors:
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stderr or stdout
        actor_payloads[actor] = json.loads(stdout)

    verified = _run("verify", "--run-dir", run_dir)

    assert verified["process_boundary_proof"] == "PASS"
    assert verified["shared_pre_read_parent"] is True
    assert verified["successful_writers"] == 1
    assert verified["version_conflict_writers"] == 1
    assert verified["current_record_count"] == 1
    assert verified["current_lineage_forks"] == 0
    assert verified["partial_loser_rows"] == 0
    assert verified["lost_update_prevention"] == "PASS"

    assert {payload["actor"] for payload in actor_payloads.values()} == {
        "codex-disposable",
        "hermes-disposable",
    }
    assert len({payload["pid"] for payload in actor_payloads.values()}) == 2
    assert len({payload["effective_home"] for payload in actor_payloads.values()}) == 2
    assert len({payload["gateway_instance_id"] for payload in actor_payloads.values()}) == 2
    assert {payload["host_id"] for payload in actor_payloads.values()} == {
        "codex-disposable",
        "hermes-disposable",
    }
