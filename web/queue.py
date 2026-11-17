"""One dispatcher per state directory; bounded, isolated generation processes."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path

from core.comic.locks import exclusive_lock
from web.store import Store

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, store: Store):
        self.store = store
        self.stop_event = threading.Event()
        self.children: dict[str, tuple[subprocess.Popen, str, object]] = {}
        self.thread: threading.Thread | None = None
        self.lock = None

    def start(self) -> None:
        self.lock = exclusive_lock(
            self.store.settings.state_dir / ".scheduler.lock",
            blocking=False,
            busy_message="此数据目录已有 Web 进程，请只运行一个 Web 实例。",
        )
        self.lock.__enter__()
        self.store.recover()
        self.thread = threading.Thread(target=self._loop, name="sorvflow-queue", daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("Queue dispatcher failed; will retry")
            self.stop_event.wait(0.5)

    def tick(self) -> None:
        for job_id, (process, lease, output) in list(self.children.items()):
            if process.poll() is None:
                continue
            output.close()
            self.children.pop(job_id)
            if self.store.worker_job(job_id, lease):
                self.store.update_job(
                    job_id,
                    lease,
                    status="error",
                    result={
                        "error": "生成进程意外退出。进度已保留，可重新提交原项目继续。",
                    },
                )
        while not self.stop_event.is_set() and len(self.children) < self.store.settings.workers:
            job = self.store.claim()
            if job is None:
                break
            if not self._spawn(job):
                break

    def _spawn(self, job: dict) -> bool:
        workspace = self.store.settings.workspace(job["user_id"])
        logs = workspace / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        request = json.loads(job["request_json"])
        project = workspace / "projects" / request["slug"]
        # A worker surviving an abrupt parent death may still hold the project
        # lock briefly. Leave this job queued until the old process releases it.
        try:
            with exclusive_lock(project / ".inkstone.lock", blocking=False):
                pass
        except RuntimeError:
            self.store.update_job(job["id"], job["lease"], status="queued")
            return False
        output = (logs / f"{job['id']}.stderr.log").open("ab")
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "web.worker",
                    "--state-dir",
                    str(self.store.settings.state_dir),
                    "--job-id",
                    job["id"],
                    "--lease",
                    job["lease"],
                    "--parent",
                    str(os.getpid()),
                ],
                cwd=Path(__file__).resolve().parents[1],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                close_fds=True,
            )
            self.children[job["id"]] = (process, job["lease"], output)
            self.store.update_job(job["id"], job["lease"], pid=process.pid)
            return True
        except Exception:
            output.close()
            self.store.update_job(
                job["id"],
                job["lease"],
                status="error",
                result={
                    "error": "无法启动生成进程，请联系管理员检查服务日志。",
                },
            )
            raise

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=20)
        for process, _, _ in self.children.values():
            if process.poll() is None:
                process.terminate()
        for job_id, (process, lease, output) in self.children.items():
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            finally:
                output.close()
            # A cooperative worker may already have put itself back in queue.
            job = self.store.worker_job(job_id, lease)
            if job:
                self.store.update_job(
                    job_id, lease, status="cancelled" if job["cancel_requested"] else "queued"
                )
        self.children.clear()
        if self.lock:
            self.lock.__exit__(None, None, None)
            self.lock = None
