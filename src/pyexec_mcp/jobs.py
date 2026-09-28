"""Background jobs: hand off a long run, be told when it lands.

A tool call returns only once its code has finished, which caps every blocking
tool at ``PYEXEC_MAX_TIMEOUT``. That is the right shape for a snippet and the
wrong one for a sweep: the caller waits on the line, the client may give up
first, and a forgotten job has nowhere to report to.

A background job is the same execution with the waiting removed. Submitting one
starts a worker, returns an id straight away, and lets a daemon thread collect
the result; the caller hears about it through Telegram when it lands and can
read the output whenever it likes.

Three decisions shape this module:

* **Each job gets its own process.** The persistent session is a hard
  serialization point -- one non-reentrant lock held across a whole round trip,
  and a worker that answers one request at a time -- so a job sharing it would
  either block foreground calls or be blocked by them. Jobs run down the same
  throwaway-process path as :func:`pyexec_mcp.session.run_isolated`.
* **Nothing here knows about Telegram.** Finishing calls ``on_finish``, and
  what that does -- summarise, build download links, post a message -- belongs
  to the caller. That keeps this module testable without a bot, and keeps the
  dependency arrow pointing one way.
* **A job that dies takes nothing with it.** The worker thread swallows every
  exception, for the same reason the notifier thread does. A failed background
  job is a status, not an outage.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Literal

from .config import Settings
from .session import CallResult, ExecutionSession, SessionError

logger = logging.getLogger(__name__)

#: Where a job is in its life. There is deliberately no "lost": records live in
#: memory, so a job that outlived the process holding it does not exist to be
#: reported missing.
JobState = Literal["running", "done", "cancelled"]

#: Prefix every id carries, so one is recognisable in a chat message.
ID_PREFIX = "job-"

#: Random characters kept in an id. Short enough to read off a phone, long
#: enough that a stale id from a previous run collides with nothing -- which
#: matters because these records are memory-only and get reused addresses.
ID_CHARS = 8

#: How long :meth:`JobManager.cancel` waits for the worker thread to record the
#: outcome. Killing a process and seeing end-of-file are both fast; this is a
#: ceiling for a machine under enough load that it is not.
CANCEL_SETTLE = 5.0


class JobLimitReached(RuntimeError):
    """The configured number of concurrent jobs is already running."""


@dataclass
class Job:
    """One background run, from submission to whichever end it meets."""

    id: str
    #: Tool that submitted it, e.g. ``pyexec_run_background``.
    tool: str
    #: What it runs against -- a script path, or a caller-supplied label.
    subject: str
    #: Source handed to the worker.
    code: str
    timeout: float
    #: Workspace-relative files this job is expected to produce. Validated as
    #: paths at submit, but not required to exist yet: producing them is the
    #: job.
    artifacts: tuple[str, ...] = ()
    started_at: float = field(default_factory=time.time)
    state: JobState = "running"

    #: The live session, so a cancel has something to kill. Cleared once the
    #: job ends, which is also how "is there anything left to cancel" is
    #: answered.
    session: ExecutionSession | None = None
    #: Set by :meth:`JobManager.cancel`. The runner checks it before sending,
    #: so a cancel landing in the moment between registration and request is
    #: not quietly forgotten.
    cancel_requested: bool = False

    result: CallResult | None = None
    finished_at: float | None = None
    #: Whether a Telegram message about this job was handed to the delivery
    #: thread. Handed, not delivered: delivery happens on that thread and its
    #: failures are logged rather than reported, so no synchronous answer could
    #: mean "delivered". False when notifications are unconfigured, which is a
    #: configuration, not a failure.
    notified: bool = False

    def duration_s(self) -> float:
        """Wall-clock seconds so far, settled once the job ends."""
        end = self.finished_at if self.finished_at is not None else time.time()
        return max(0.0, end - self.started_at)


def _new_id() -> str:
    """A short random job id; see :data:`ID_CHARS`."""
    return ID_PREFIX + uuid.uuid4().hex[:ID_CHARS]


def _cancelled_result() -> CallResult:
    """The outcome a job reports when it was stopped rather than finished."""
    return CallResult(
        ok=False,
        error={
            "type": "CancelledError",
            "message": (
                "The job was cancelled and its worker was killed. Any file it "
                "was part way through writing may be incomplete."
            ),
            "traceback": "",
        },
    )


def _harness_failure(exc: Exception) -> CallResult:
    """The outcome a job reports when the machinery, not the code, failed."""
    return CallResult(
        ok=False,
        error={
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": "",
        },
    )


class JobManager:
    """The registry of background jobs, and the threads that run them.

    One per configuration, discarded when the configuration is replaced -- see
    :func:`pyexec_mcp.server.use_settings`. That is deliberate: a job's worker
    has the old workspace as its working directory, so it cannot be carried
    across a change of workspace, and pretending otherwise would have it
    writing into a directory nobody is looking at any more.
    """

    def __init__(
        self,
        settings: Settings,
        on_finish: Callable[[Job, CallResult, float], bool] | None = None,
    ) -> None:
        self._settings = settings
        self._on_finish = on_finish
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        #: Set by :meth:`shutdown`. A server on its way out should not post
        #: "job cancelled" to a chat for every job it was still running.
        self._closing = False

    # -- reading -----------------------------------------------------------

    def get(self, job_id: str) -> Job | None:
        """The job with *job_id*, or ``None`` if it was never here or has been
        forgotten."""
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list[Job]:
        """Every job held, newest first."""
        with self._lock:
            return list(self._jobs.values())[::-1]

    def running(self) -> list[Job]:
        """The jobs still going, newest first."""
        with self._lock:
            return [job for job in self._jobs.values() if job.state == "running"][::-1]

    @property
    def limit(self) -> int:
        """How many jobs may run at once."""
        return self._settings.max_background_jobs

    @property
    def history(self) -> int:
        """How many finished jobs are remembered."""
        return self._settings.job_history

    # -- running -----------------------------------------------------------

    def submit(
        self,
        *,
        tool: str,
        subject: str,
        code: str,
        timeout: float,
        artifacts: tuple[str, ...] = (),
    ) -> Job:
        """Start a job and return it, without waiting for the code.

        The worker is spawned before this returns. That costs an interpreter
        launch on the submit path -- negligible against a run measured in
        minutes -- and buys two things: a cancel issued the instant this
        returns has a process to kill rather than a race to lose, and an
        interpreter that cannot start is reported to the submitter instead of
        only in a notification.

        Raises:
            JobLimitReached: if :attr:`limit` jobs are already running.
            SessionError: if the worker could not be started.
        """
        with self._lock:
            active = sum(1 for job in self._jobs.values() if job.state == "running")
            if active >= self._settings.max_background_jobs:
                raise JobLimitReached(
                    f"{active} background job(s) are already running, which is "
                    "the limit. Wait for one to finish, cancel one with "
                    "pyexec_job_cancel, or raise PYEXEC_MAX_BACKGROUND_JOBS."
                )

            job = Job(
                id=_new_id(),
                tool=tool,
                subject=subject,
                code=code,
                timeout=timeout,
                artifacts=tuple(artifacts),
            )
            session = ExecutionSession(self._settings)
            session.start()  # SessionError propagates; nothing is registered
            job.session = session
            self._jobs[job.id] = job

        threading.Thread(
            target=self._run,
            args=(job, session),
            name=f"pyexec-{job.id}",
            daemon=True,
        ).start()
        return job

    def _run(self, job: Job, session: ExecutionSession) -> None:
        """Drive one job to its end, whatever that turns out to be."""
        try:
            result = self._execute(job, session)
        except Exception as exc:  # noqa: BLE001 - a job thread must not escape
            logger.exception("Background job %s failed unexpectedly", job.id)
            result = _harness_failure(exc)
        finally:
            try:
                session.close()
            except Exception:  # noqa: BLE001 - shutdown is best effort
                logger.exception("Could not close the worker for job %s", job.id)

        self._finish(job, result)

    def _execute(self, job: Job, session: ExecutionSession) -> CallResult:
        """Send the request, unless a cancel beat it here."""
        with self._lock:
            if job.cancel_requested:
                return _cancelled_result()
            if self._closing:
                return _cancelled_result()
        return session.request(
            "exec", timeout=job.timeout, background=True, code=job.code
        )

    def _finish(self, job: Job, result: CallResult) -> None:
        """Record the outcome, forget the oldest jobs, announce this one."""
        with self._lock:
            job.session = None
            # A cancel that landed after the code had already run to
            # completion is not a cancellation: the work happened, and calling
            # it cancelled would hide a result that exists.
            cancelled = job.cancel_requested and not result.ok
            job.state = "cancelled" if cancelled else "done"
            job.result = _cancelled_result() if cancelled else result
            job.finished_at = time.time()
            duration = job.duration_s()
            self._forget_oldest()
            closing = self._closing

        if self._on_finish is None or closing:
            return
        try:
            job.notified = bool(self._on_finish(job, job.result, duration))
        except Exception:  # noqa: BLE001 - announcing is strictly best effort
            logger.exception("Could not announce background job %s", job.id)

    def _forget_oldest(self) -> None:
        """Drop finished jobs past the configured history. Caller holds the lock.

        Running jobs are never dropped, so the configured number bounds the
        jobs you can no longer act on rather than the ones you can.
        """
        finished = [job for job in self._jobs.values() if job.state != "running"]
        for job in finished[: max(0, len(finished) - self._settings.job_history)]:
            self._jobs.pop(job.id, None)

    # -- stopping ----------------------------------------------------------

    def cancel(self, job_id: str) -> Job | None:
        """Kill a running job and wait, briefly, for it to be recorded.

        Returns the job, or ``None`` if there is no such job. A job that has
        already finished is returned unchanged -- there is nothing to stop, and
        the caller can read its real outcome from the returned state.

        Waiting is what makes the caller's receipt honest: the state it reads
        is the one the job ended in, not a promise about one.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.state != "running" or job.session is None:
                return job
            job.cancel_requested = True
            session = job.session

        # Outside the lock: killing a process takes a moment, and the runner
        # needs the lock to record that it died.
        session.abort()

        deadline = time.monotonic() + CANCEL_SETTLE
        while job.state == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
        return job

    def shutdown(self) -> None:
        """Kill everything running, without announcing any of it.

        Called when the process is going away or the configuration is being
        replaced. The worker threads are left to record their own outcomes;
        they are daemons, and there is nothing left worth waiting for.
        """
        with self._lock:
            self._closing = True
            pending = [
                job.session
                for job in self._jobs.values()
                if job.state == "running" and job.session is not None
            ]
            for job in self._jobs.values():
                if job.state == "running":
                    job.cancel_requested = True

        for session in pending:
            try:
                session.abort()
            except Exception:  # noqa: BLE001 - already on the way out
                logger.exception("Could not stop a background worker")


__all__ = [
    "CANCEL_SETTLE",
    "ID_CHARS",
    "ID_PREFIX",
    "Job",
    "JobLimitReached",
    "JobManager",
    "JobState",
]
