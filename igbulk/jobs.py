"""Bulk job orchestration: a queue of links, a small worker pool, live state.

State lives in memory and is guarded by one lock. Every mutation bumps a
version counter so the web UI can long-poll for changes instead of hammering
the server; `JobManager.wait_for_change` is that hook.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .engine import Engine, EngineError, Options
from .links import Link, ParseResult

QUEUED, RUNNING, WAITING, DONE, SKIPPED, FAILED, CANCELED, BLOCKED = (
    "queued", "running", "waiting", "done", "skipped", "failed", "canceled", "blocked"
)
TERMINAL = {DONE, SKIPPED, FAILED, CANCELED, BLOCKED}


@dataclass
class Item:
    link: Link
    status: str = QUEUED
    percent: float | None = None
    speed: str | None = None
    eta: str | None = None
    message: str = ""
    files: list[dict] = field(default_factory=list)
    uploader: str | None = None
    current_file: str | None = None
    started: float | None = None
    finished: float | None = None
    attempts: int = 0
    log: str = ""

    @property
    def bytes(self) -> int:
        return sum(f.get("size", 0) for f in self.files)

    def as_dict(self) -> dict:
        d = self.link.as_dict()
        d.update({
            "status": self.status,
            "percent": self.percent,
            "speed": self.speed,
            "eta": self.eta,
            "message": self.message,
            "files": self.files,
            "uploader": self.uploader,
            "current_file": self.current_file,
            "bytes": self.bytes,
            "attempts": self.attempts,
            "elapsed": round((self.finished or time.time()) - self.started, 1)
                       if self.started else None,
        })
        return d


@dataclass
class Job:
    id: str
    items: list[Item]
    opts: Options
    created: float = field(default_factory=time.time)
    finished: float | None = None
    concurrency: int = 3
    cancel: threading.Event = field(default_factory=threading.Event)
    rejected: list[str] = field(default_factory=list)
    duplicates: int = 0

    @property
    def counts(self) -> dict:
        counts = {k: 0 for k in
                  (QUEUED, RUNNING, WAITING, DONE, SKIPPED, FAILED, CANCELED, BLOCKED)}
        for item in self.items:
            counts[item.status] = counts.get(item.status, 0) + 1
        return counts

    @property
    def is_complete(self) -> bool:
        return all(i.status in TERMINAL for i in self.items)

    def as_dict(self, include_logs: bool = False) -> dict:
        counts = self.counts
        total = len(self.items)
        settled = sum(counts[k] for k in TERMINAL)
        payload = {
            "job_id": self.id,
            "created": self.created,
            "finished": self.finished,
            "complete": self.is_complete,
            "canceled": self.cancel.is_set(),
            "total": total,
            "settled": settled,
            "counts": counts,
            "bytes": sum(i.bytes for i in self.items),
            "files": [f for i in self.items for f in i.files],
            "dest": str(self.opts.dest),
            "options": self.opts.as_dict(),
            "concurrency": self.concurrency,
            "rejected": self.rejected,
            "duplicates": self.duplicates,
            "items": [i.as_dict() for i in self.items],
        }
        if include_logs:
            for item, out in zip(self.items, payload["items"]):
                out["log"] = item.log
        return payload


class JobManager:
    def __init__(self, max_jobs_kept: int = 40):
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._version = 0
        self._max_jobs_kept = max_jobs_kept

    # -- state plumbing -------------------------------------------------------
    def _touch(self) -> None:
        with self._changed:
            self._version += 1
            self._changed.notify_all()

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def wait_for_change(self, since: int, timeout: float = 20.0) -> int:
        """Block until the version passes `since`, or the timeout elapses."""
        deadline = time.monotonic() + timeout
        with self._changed:
            while self._version <= since:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._changed.wait(remaining)
            return self._version

    # -- job lifecycle --------------------------------------------------------
    def create(self, parsed: ParseResult, opts: Options, concurrency: int = 3,
               start: bool = True) -> Job:
        job = Job(
            id=uuid.uuid4().hex[:12],
            items=[Item(link=l) for l in parsed.links],
            opts=opts,
            concurrency=max(1, min(8, concurrency)),
            rejected=list(parsed.rejected),
            duplicates=parsed.duplicates,
        )
        # Links Instagram will not serve anonymously are marked up front rather
        # than burning a request (and a rate-limit slot) to fail. Profile links
        # stay blocked even with cookies — yt-dlp can't enumerate a profile
        # reliably, and attempting it just produces a confusing error.
        for item in job.items:
            always_blocked = item.link.kind == "profile"
            if always_blocked or (not item.link.supported and not opts.authenticated):
                item.status = BLOCKED
                item.message = item.link.note or "Needs a logged-in session."
                item.finished = time.time()

        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._evict_old()
        self._touch()

        if start:
            threading.Thread(target=self._run, args=(job,),
                             name=f"igbulk-job-{job.id}", daemon=True).start()
        return job

    def _evict_old(self) -> None:
        while len(self._order) > self._max_jobs_kept:
            oldest = self._order[0]
            job = self._jobs.get(oldest)
            if job and not job.is_complete:
                break
            self._order.pop(0)
            self._jobs.pop(oldest, None)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return [self._jobs[i] for i in reversed(self._order) if i in self._jobs]

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        job.cancel.set()
        with self._lock:
            for item in job.items:
                if item.status in (QUEUED, WAITING):
                    item.status = CANCELED
                    item.message = "Canceled."
                    item.finished = time.time()
        self._touch()
        return True

    # -- execution ------------------------------------------------------------
    def _run(self, job: Job) -> None:
        pending = [i for i in job.items if i.status == QUEUED]
        if not pending:
            with self._lock:
                job.finished = time.time()
            self._touch()
            return

        queue: list[Item] = list(pending)
        queue_lock = threading.Lock()

        def worker() -> None:
            engine = Engine(job.opts)
            while not job.cancel.is_set():
                with queue_lock:
                    if not queue:
                        return
                    item = queue.pop(0)
                self._process(job, engine, item)

        threads = [
            threading.Thread(target=worker, daemon=True,
                             name=f"igbulk-w{n}-{job.id}")
            for n in range(min(job.concurrency, len(queue)))
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        with self._lock:
            job.finished = time.time()
        self._touch()

    def _process(self, job: Job, engine: Engine, item: Item) -> None:
        with self._lock:
            item.status = RUNNING
            item.started = time.time()
            item.attempts += 1
            item.message = "Fetching…"
        self._touch()

        last_push = 0.0

        def on_progress(payload: dict) -> None:
            nonlocal last_push
            with self._lock:
                phase = payload.get("phase")
                if phase == "waiting":
                    item.status = WAITING
                    item.message = payload.get("message", "Waiting…")
                    item.attempts += 1
                else:
                    item.status = RUNNING
                    if payload.get("file"):
                        item.current_file = payload["file"]
                    if phase == "converting":
                        # ffmpeg gives us no percentage, so hold the bar where it
                        # is and just say what's happening.
                        item.message = payload.get("message", "Converting…")
                        item.speed = item.eta = None
                    else:
                        if payload.get("percent") is not None:
                            item.percent = payload["percent"]
                        if "speed" in payload:
                            item.speed = payload.get("speed")
                        if "eta" in payload:
                            item.eta = payload.get("eta")
                        item.message = "Downloading…"
            now = time.monotonic()
            # Throttle notifications so a fast download doesn't spin the pollers.
            if now - last_push > 0.25:
                last_push = now
                self._touch()

        try:
            result = engine.download(item.link.url, on_progress=on_progress,
                                     cancel=job.cancel)
        except EngineError as exc:
            with self._lock:
                canceled = job.cancel.is_set()
                item.status = CANCELED if canceled else FAILED
                item.message = "Canceled." if canceled else str(exc)
                item.log = getattr(exc, "log", "") or item.log
                item.percent = None
                item.speed = item.eta = None
                item.finished = time.time()
            self._touch()
            return
        except Exception as exc:  # pragma: no cover - unexpected engine bug
            with self._lock:
                item.status = FAILED
                item.message = f"Internal error: {exc}"
                item.finished = time.time()
            self._touch()
            return

        with self._lock:
            item.files = result["files"]
            item.uploader = result.get("uploader") or item.uploader
            item.log = result.get("log", "")
            item.percent = 100.0
            item.speed = item.eta = None
            item.current_file = None
            item.finished = time.time()
            if result["skipped"]:
                item.status = SKIPPED
                item.message = "Already downloaded."
            else:
                n = len(item.files)
                item.status = DONE
                item.message = f"{n} file{'s' if n != 1 else ''}"
                # e.g. ffmpeg missing, so VP9 couldn't be converted.
                for note in result.get("notes") or []:
                    item.message += f" — {note}"
        self._touch()

    def retry_failed(self, job_id: str, concurrency: int | None = None) -> Job | None:
        """Start a fresh job containing only the links that didn't land."""
        job = self.get(job_id)
        if job is None:
            return None
        retryable = [i.link for i in job.items if i.status in (FAILED, CANCELED)]
        if not retryable:
            return None
        parsed = ParseResult(links=retryable)
        return self.create(parsed, job.opts,
                           concurrency or job.concurrency)


def run_sync(parsed: ParseResult, opts: Options, concurrency: int = 3,
             on_change=None) -> Job:
    """Blocking helper used by the CLI and MCP server."""
    manager = JobManager()
    job = manager.create(parsed, opts, concurrency)
    seen = 0
    while True:
        seen = manager.wait_for_change(seen, timeout=1.0)
        if on_change:
            on_change(job)
        if job.is_complete and job.finished:
            break
    if on_change:
        on_change(job)
    return job


def ensure_dest(path: str | Path) -> Path:
    dest = Path(path).expanduser().resolve()
    dest.mkdir(parents=True, exist_ok=True)
    return dest
