"""A single leased job per process: no shared user paths or log handlers."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import threading
import time
from pathlib import Path

from web.settings import Settings
from web.store import Store


class JobLog(logging.Handler):
    def __init__(self, store: Store, job_id: str, lease: str):
        super().__init__(logging.INFO)
        self.store, self.job_id, self.lease = store, job_id, lease

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.store.append_log(self.job_id, self.lease, record.getMessage())
        except Exception:
            # Never log recursively from a logging handler.
            self.handleError(record)


def execute(store: Store, job_id: str, lease: str, stopping: threading.Event) -> None:
    job = store.worker_job(job_id, lease)
    if job is None:
        return
    os.umask(0o027)
    request = json.loads(job["request_json"])
    workspace = store.settings.workspace(job["user_id"])
    workspace.mkdir(parents=True, exist_ok=True)
    os.environ["INKSTONE_DATA_DIR"] = str(workspace / "data")
    os.environ["INKSTONE_EXPERIMENTS_DIR"] = str(workspace / "experiments")
    os.environ["INKSTONE_ERROR_LOG"] = f"{job_id}-provider-errors.jsonl"
    os.chdir(workspace)

    from core.pipelines.run_until_complete import PausedRun, run_until_complete
    from core.pipelines.timing import estimate_remaining
    from web import server as legacy

    # These globals are local to this child process, never HTTP request globals.
    legacy.OUTPUT_DIR = workspace / "projects"
    output_dir = legacy.OUTPUT_DIR / request["slug"]
    output_dir.mkdir(parents=True, exist_ok=True)
    base_elapsed = legacy._load_timing_elapsed(request["slug"])
    started = time.monotonic()
    root = logging.getLogger()
    log = JobLog(store, job_id, lease)
    root.setLevel(logging.INFO)
    root.addHandler(log)
    store.update_job(
        job_id,
        lease,
        result={
            "session_started_at": time.time(),
            "base_elapsed": base_elapsed,
            "error": None,
            "stage": "resume" if (output_dir / "state.json").exists() else "init",
        },
    )

    def cancelled() -> bool:
        current = store.worker_job(job_id, lease)
        return stopping.is_set() or current is None or bool(current["cancel_requested"])

    def progress(stage: str, percent: float | None) -> None:
        elapsed = base_elapsed + time.monotonic() - started
        result = {
            "stage": stage,
            "elapsed_seconds": elapsed,
            "remaining_seconds": estimate_remaining(elapsed, percent or 0),
        }
        if percent is not None:
            result["progress"] = percent
        store.update_job(job_id, lease, result=result)
        legacy._persist_active_elapsed(request["slug"], elapsed)

    try:
        if cancelled():
            raise RuntimeError("任务已停止。")
        if request["kind"] == "sequence":
            options = request["options"]

            async def advance():
                with legacy.experiment_scope(workspace / "experiments"):
                    directory = legacy._sequence_dir(options["sequence_id"])
                    with legacy.exclusive_lock(directory / ".web.lock", blocking=False):
                        detail = legacy.sequence_detail(options["sequence_id"])
                        if detail["status"] == "done" or detail["current_round"] > options["round"]:
                            return detail
                        task = asyncio.create_task(
                            legacy.advance_sequence_api(
                                options["sequence_id"],
                                merge_from=[options["merge_from"]]
                                if options.get("merge_from")
                                else [],
                                components=options.get("components"),
                            )
                        )
                        while not task.done():
                            if cancelled():
                                task.cancel()
                                try:
                                    await task
                                except asyncio.CancelledError:
                                    pass
                                raise RuntimeError("任务已停止。")
                            await asyncio.wait({task}, timeout=0.5)
                        return await task

            result = asyncio.run(advance())
            store.update_job(
                job_id,
                lease,
                status="done",
                result={
                    "sequence_id": request["options"]["sequence_id"],
                    "stage": "done",
                    "progress": 1.0,
                },
            )
            return
        keys = None
        if request["kind"] == "regen":
            options = request["options"]
            prior = json.loads(job["result_json"])
            if "regen_keys" in prior:
                keys = prior["regen_keys"]
            else:
                _, keys = legacy.prepare_regen(
                    request["slug"],
                    out_dir=output_dir,
                    stale=bool(options.get("stale")),
                    keys=options.get("keys"),
                )
                store.update_job(job_id, lease, result={"regen_keys": keys})
        result = asyncio.run(
            run_until_complete(
                request["text"],
                output_dir=str(output_dir),
                project_id=request["slug"],
                output_format=request["format"],
                style_guide=request["style"] or None,
                panel_keys=keys,
                progress_callback=progress,
                cancel_check=cancelled,
                data_dir=str(workspace / "data"),
            )
        )
        payload = {"log": []}
        if isinstance(result, PausedRun):
            legacy._fill_job_from_paused(payload, result)
            status = "paused"
        else:
            legacy._fill_job_from_project(payload, result)
            payload.update(progress=1.0, stage="done")
            status = "done"
        payload.pop("log", None)
        current = store.worker_job(job_id, lease)
        if current and current["cancel_requested"]:
            status = "cancelled"
        elif stopping.is_set():
            status = "queued"
        payload["elapsed_seconds"] = base_elapsed + time.monotonic() - started
        store.update_job(job_id, lease, result=payload, status=status)
    except Exception as exc:
        current = store.worker_job(job_id, lease)
        status = "error"
        if current and current["cancel_requested"]:
            status = "cancelled"
        elif stopping.is_set():
            status = "queued"
        store.append_log(job_id, lease, f"{type(exc).__name__}: {exc}")
        store.update_job(job_id, lease, status=status, result={"error": str(exc)[:2000]})
    finally:
        legacy._persist_active_elapsed(request["slug"], base_elapsed + time.monotonic() - started)
        root.removeHandler(log)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--lease", required=True)
    parser.add_argument("--parent", type=int, required=True)
    args = parser.parse_args()
    store = Store(Settings(state_dir=args.state_dir))
    stopping = threading.Event()
    finished = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())

    def parent_watch() -> None:
        while not finished.wait(1):
            if os.getppid() != args.parent:
                stopping.set()
                if not finished.wait(10):
                    os._exit(75)  # bounded orphan lifetime after dispatcher death
                return

    threading.Thread(target=parent_watch, daemon=True).start()
    try:
        execute(store, args.job_id, args.lease, stopping)
    finally:
        finished.set()


if __name__ == "__main__":
    main()
