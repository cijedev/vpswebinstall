"""Background jobs with a shared log buffer for SSE / polling."""

from __future__ import annotations

import io
import logging
import sys
import threading
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from cije_vpsinstallerweb.bridge import (
    load_or_default,
    run_backup_job,
    run_install_job,
    run_restore_job,
    run_test_job,
    run_troubleshoot_job,
)
from cije_vpsinstallerweb.settings import Settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    id: str
    kind: str
    status: str = "queued"
    started_at: str | None = None
    finished_at: str | None = None
    lines: list[str] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    _cv: threading.Condition = field(default_factory=threading.Condition, repr=False)

    def append(self, line: str) -> None:
        text = line.rstrip("\n")
        with self._cv:
            self.lines.append(text)
            self._cv.notify_all()

    def snapshot(self) -> dict[str, Any]:
        with self._cv:
            return {
                "id": self.id,
                "kind": self.kind,
                "status": self.status,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "lines": list(self.lines),
                "line_count": len(self.lines),
                "result": self.result,
                "error": self.error,
            }


class _Tee(io.TextIOBase):
    def __init__(self, job: Job, original) -> None:
        self.job = job
        self.original = original
        self._buf = ""

    def write(self, s: str) -> int:  # type: ignore[override]
        if not s:
            return 0
        try:
            self.original.write(s)
        except Exception:
            pass
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip() or line == "":
                self.job.append(line)
        return len(s)

    def flush(self) -> None:
        try:
            self.original.flush()
        except Exception:
            pass
        if self._buf:
            self.job.append(self._buf)
            self._buf = ""


class _LogHandler(logging.Handler):
    def __init__(self, job: Job) -> None:
        super().__init__()
        self.job = job

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.job.append(self.format(record))
        except Exception:
            pass


class JobManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._current_id: str | None = None
        self._order: list[str] = []

    def current(self) -> Job | None:
        with self._lock:
            if not self._current_id:
                return None
            return self._jobs.get(self._current_id)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def latest(self) -> Job | None:
        with self._lock:
            if not self._order:
                return None
            return self._jobs.get(self._order[-1])

    def busy(self) -> bool:
        job = self.current()
        return bool(job and job.status in {"queued", "running"})

    def start(self, kind: str, fn: Callable[[Job], None]) -> Job:
        with self._lock:
            current = self._jobs.get(self._current_id) if self._current_id else None
            if current and current.status in {"queued", "running"}:
                raise RuntimeError(f"job {current.id} ({current.kind}) is still running")
            job = Job(id=uuid.uuid4().hex[:12], kind=kind, status="queued")
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._current_id = job.id
        thread = threading.Thread(target=self._run, args=(job, fn), name=f"cije-job-{job.kind}", daemon=True)
        thread.start()
        return job

    def _run(self, job: Job, fn: Callable[[Job], None]) -> None:
        job.status = "running"
        job.started_at = _now()
        job.append(f"=== {job.kind} started {job.started_at} ===")
        tee_out = _Tee(job, sys.stdout)
        tee_err = _Tee(job, sys.stderr)
        handler = _LogHandler(job)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        root_log = logging.getLogger()
        dvp_log = logging.getLogger("debian_vps_setup")
        root_log.addHandler(handler)
        dvp_log.addHandler(handler)
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = tee_out, tee_err  # type: ignore[assignment]
        try:
            fn(job)
            if job.status == "running":
                job.status = "ok" if job.result.get("ok", True) else "error"
        except Exception as exc:
            job.error = str(exc)
            job.result = {"ok": False, "error": str(exc)}
            job.status = "error"
            job.append(traceback.format_exc())
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            root_log.removeHandler(handler)
            dvp_log.removeHandler(handler)
            tee_out.flush()
            tee_err.flush()
            job.finished_at = _now()
            job.append(f"=== {job.kind} finished {job.status} ===")
            with job._cv:
                job._cv.notify_all()

    def submit_install(
        self,
        settings: Settings,
        payload: dict[str, Any],
        cfg,
    ) -> Job:
        def work(job: Job) -> None:
            job.result = run_install_job(
                cfg,
                settings,
                force=bool(payload.get("force")),
                only=payload.get("only"),
                skip=payload.get("skip"),
                no_backup=bool(payload.get("no_backup")),
            )

        return self.start("install", work)

    def submit_test(self, settings: Settings, cfg) -> Job:
        def work(job: Job) -> None:
            job.result = run_test_job(cfg, settings)

        return self.start("test", work)

    def submit_troubleshoot(self, settings: Settings, cfg, components: list[str] | None) -> Job:
        def work(job: Job) -> None:
            job.result = run_troubleshoot_job(cfg, settings, components)

        return self.start("troubleshoot", work)

    def submit_backup(self, settings: Settings) -> Job:
        cfg = load_or_default(settings)

        def work(job: Job) -> None:
            job.result = run_backup_job(cfg, settings)

        return self.start("backup", work)

    def submit_restore(self, settings: Settings, name: str) -> Job:
        def work(job: Job) -> None:
            job.result = run_restore_job(settings, name)

        return self.start("restore", work)
