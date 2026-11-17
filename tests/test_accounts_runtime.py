"""Exercise the real Uvicorn entry point with port collision and persisted recovery."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from web.settings import Settings
from web.store import Store


def test_real_server_selects_next_port_and_recovers_after_restart(tmp_path):
    settings = Settings(state_dir=tmp_path / "state", workers=1)
    store = Store(settings)
    user = store.create_user("alice", "test-password-123", must_change=False)
    job, _ = store.enqueue(user["id"], "saved manuscript", "project", "", "page")
    first = store.claim()
    # Stop this queued job on recovery, preventing any real generation.
    store.stop(user["id"], job["id"])
    runtime = tmp_path / "run"
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        start = occupied.getsockname()[1]
        program = (
            "from pathlib import Path; from web.runtime import run; "
            "from web.settings import Settings; "
            f"run(port={start},port_end={min(start + 30, 65535)},"
            f"runtime_dir=Path({str(runtime)!r}),"
            f"settings=Settings(state_dir=Path({str(settings.state_dir)!r})))"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", program],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PROVIDER": "openai_compat", "OPENAI_COMPAT_API_KEY": ""},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if (runtime / "port").exists():
                    break
                assert process.poll() is None, process.stderr.read().decode()
                time.sleep(0.1)
            assert (runtime / "port").exists()
            port = int((runtime / "port").read_text())
            assert port > start
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=5
            ) as response:
                assert json.load(response)["accounts"] is True
            assert store.owned_job(user["id"], job["id"])["status"] == "cancelled"
            assert not store.update_job(job["id"], first["lease"], status="done")
        finally:
            process.send_signal(signal.SIGTERM)
            _, errors = process.communicate(timeout=20)
        assert process.returncode in {0, -signal.SIGTERM}, errors.decode()
        assert "Application shutdown complete" in errors.decode()
        assert not (runtime / "port").exists()
        assert not (runtime / "pid").exists()


def test_real_worker_observes_cancel_before_any_model_call(tmp_path):
    from web.queue import Scheduler

    store = Store(Settings(state_dir=tmp_path / "state"))
    user = store.create_user("alice", "test-password-123", must_change=False)
    job, _ = store.enqueue(user["id"], "must never reach provider", "cancelled", "", "page")
    claim = store.claim()
    store.stop(user["id"], job["id"])
    scheduler = Scheduler(store)
    try:
        assert scheduler._spawn(claim)
        process, _, _ = scheduler.children[job["id"]]
        assert process.wait(timeout=20) == 0
        scheduler.tick()
        assert not scheduler.children
        result = store.snapshot(store.owned_job(user["id"], job["id"]))
        assert result["status"] == "cancelled"
        assert (
            not store.settings.workspace(user["id"])
            .joinpath("projects", "cancelled", "state.json")
            .exists()
        )
    finally:
        scheduler.stop()
