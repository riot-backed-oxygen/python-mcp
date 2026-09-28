"""Parent-side management of a worker subprocess.

One :class:`ExecutionSession` owns one long-lived worker. Calls are serialised
so the namespace stays coherent, and a call that overruns its deadline costs
the worker its life -- the session transparently starts a fresh one next time.
"""

from __future__ import annotations

import itertools
import json
import queue
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import TIMEOUT_GRACE, Settings
from .protocol import split_frame

#: Absolute path to the worker script, so it can be launched without relying on
#: the package being importable from the child's point of view.
WORKER_PATH = Path(__file__).with_name("worker.py")

#: How long a caller waits for the session lock before being told it is busy.
LOCK_WAIT = 5.0


class SessionError(RuntimeError):
    """The worker could not be started, or died mid-call."""


@dataclass
class CallResult:
    """Outcome of one request against a worker."""

    ok: bool
    stdout: str = ""
    stderr: str = ""
    result: str | None = None
    error: dict[str, Any] | None = None
    duration_ms: float = 0.0
    #: Output the snippet wrote straight to fd 1/2, which we could not capture
    #: inside the worker.
    raw_output: str = ""
    truncated: bool = False
    timed_out: bool = False
    session_restarted: bool = False
    #: Op-specific fields that have no place in the common result shape, such as
    #: the worker's pid and version for ``info`` requests.
    extra: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        """Flatten into the dict shape the MCP tools return."""
        return {
            "ok": self.ok,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "result": self.result,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "raw_output": self.raw_output,
            "truncated": self.truncated,
        }


class ExecutionSession:
    """A worker subprocess plus the plumbing needed to talk to it."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._nonce = uuid.uuid4().hex
        self._lock = threading.Lock()
        self._ids = itertools.count(1)

        self._proc: subprocess.Popen[bytes] | None = None
        self._frames: queue.Queue[tuple[str, Any]] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=200)

    # -- lifecycle ---------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _spawn(self) -> None:
        """Start a worker, replacing any previous one."""
        self._settings.workspace.mkdir(parents=True, exist_ok=True)

        try:
            proc = subprocess.Popen(
                [
                    self._settings.python_executable,
                    "-u",  # unbuffered, so framing stays prompt
                    str(WORKER_PATH),
                    "--nonce",
                    self._nonce,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self._settings.workspace),
                env=self._settings.child_env(),
            )
        except OSError as exc:
            raise SessionError(
                f"Could not start the Python worker with "
                f"{self._settings.python_executable!r}: {exc}. Check that "
                "PYEXEC_PYTHON points at a working interpreter."
            ) from exc

        frames: queue.Queue[tuple[str, Any]] = queue.Queue()
        assert proc.stdout is not None and proc.stderr is not None

        threading.Thread(
            target=self._pump_frames, args=(proc.stdout, frames), daemon=True
        ).start()
        threading.Thread(
            target=self._pump_stderr, args=(proc.stderr,), daemon=True
        ).start()

        self._proc = proc
        self._frames = frames
        self._stderr_tail.clear()

    def _pump_frames(self, stream, sink: queue.Queue[tuple[str, Any]]) -> None:
        """Move worker stdout into a queue, tagging frames vs. raw output."""
        try:
            for line in iter(stream.readline, b""):
                raw, payload = split_frame(self._nonce, line)
                if raw:
                    sink.put(("raw", raw.decode("utf-8", errors="replace")))
                if payload is not None:
                    sink.put(("frame", payload))
        finally:
            sink.put(("eof", None))

    def _pump_stderr(self, stream) -> None:
        """Keep the worker's own stderr -- tracebacks from crashes, mostly."""
        for line in iter(stream.readline, b""):
            self._stderr_tail.append(line.decode("utf-8", errors="replace"))

    def _terminate(self) -> None:
        """Kill the worker, if any, and release its pipes."""
        proc, self._proc = self._proc, None
        self._frames = None
        if proc is None:
            return
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def close(self) -> None:
        """Best-effort graceful shutdown, then a hard kill if needed."""
        if self.alive:
            try:
                self._send({"id": 0, "op": "shutdown"})
                assert self._proc is not None
                self._proc.wait(timeout=2)
            except Exception:  # noqa: BLE001 - shutdown is best effort
                pass
        self._terminate()

    def start(self) -> None:
        """Start the worker now instead of on the first request.

        :meth:`request` spawns lazily, which is right for a caller that is
        about to block on it anyway. A background job is different: it hands a
        cancel button to someone else the moment it returns, and a cancel that
        arrives before the worker exists has nothing to kill. Starting here
        costs an interpreter launch on the submit path and buys a cancellation
        with no window in it -- plus a broken ``PYEXEC_PYTHON`` that fails at
        submit, where the caller can see it, rather than only in a notification
        nobody may be reading.

        Raises:
            SessionError: if the interpreter could not be started.
        """
        with self._lock:
            if not self.alive:
                self._spawn()

    def abort(self) -> None:
        """Kill the worker without the graceful shutdown handshake.

        Cancellation lands while the worker may be mid-write, and a
        ``shutdown`` frame racing the in-flight request down the same pipe
        would be a coin toss for no benefit. The blocked :meth:`_exchange`
        sees end-of-file and reports a dead worker, which is exactly what
        happened.

        Safe to call from another thread: :meth:`_terminate` swaps ``_proc``
        out before touching it, so a concurrent call finds nothing to do.
        """
        self._terminate()

    # -- transport ---------------------------------------------------------

    def _send(self, request: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise SessionError("Worker is not running.")
        payload = (json.dumps(request) + "\n").encode("utf-8")
        self._proc.stdin.write(payload)
        self._proc.stdin.flush()

    # -- public API --------------------------------------------------------

    def request(
        self,
        op: str,
        timeout: float | None = None,
        *,
        background: bool = False,
        **fields: Any,
    ) -> CallResult:
        """Send one request and wait for its response.

        On timeout the worker is killed rather than left running, because a
        thread we cannot interrupt is still mutating the namespace.
        Background requests use their own timeout ceiling, independently of
        the ceiling for calls that keep the client waiting.
        """
        deadline = (
            self._settings.clamp_background_timeout(timeout)
            if background
            else self._settings.clamp_timeout(timeout)
        )

        if not self._lock.acquire(timeout=LOCK_WAIT):
            return CallResult(
                ok=False,
                error={
                    "type": "SessionBusy",
                    "message": (
                        "Another call is already running in this session. "
                        "Wait for it to finish, or use pyexec_run_isolated to "
                        "run this snippet in a separate process."
                    ),
                    "traceback": "",
                },
            )

        try:
            restarted = False
            if not self.alive:
                self._spawn()
                restarted = True

            return self._exchange(op, deadline, restarted, background, **fields)
        finally:
            self._lock.release()

    def _exchange(
        self,
        op: str,
        deadline: float,
        restarted: bool,
        background: bool,
        **fields: Any,
    ) -> CallResult:
        # Captured once: another thread may call close() or abort() while this
        # loop is parked in get(), and _terminate() clears the attribute. A
        # re-read per iteration would raise AttributeError instead of returning
        # the dead-worker result that actually describes what happened.
        frames = self._frames
        assert frames is not None
        request = {"id": next(self._ids), "op": op, "timeout": deadline, **fields}
        try:
            self._send(request)
        except (SessionError, OSError, BrokenPipeError) as exc:
            self._terminate()
            return CallResult(
                ok=False,
                error={
                    "type": "SessionError",
                    "message": f"Could not reach the worker: {exc}",
                    "traceback": "".join(self._stderr_tail),
                },
                session_restarted=restarted,
            )

        # The worker interrupts itself at `deadline`; we allow a grace period
        # for the frame to travel back before assuming it is wedged.
        give_up_at = time.monotonic() + deadline + TIMEOUT_GRACE
        chunks: list[str] = []
        budget = self._settings.max_output_chars * 2

        while True:
            remaining = give_up_at - time.monotonic()
            if remaining <= 0:
                return self._timeout_result(deadline, chunks, restarted, background)

            try:
                kind, item = frames.get(timeout=remaining)
            except queue.Empty:
                return self._timeout_result(deadline, chunks, restarted, background)

            if kind == "raw":
                if sum(len(c) for c in chunks) < budget:
                    chunks.append(item)
                continue

            if kind == "eof":
                self._terminate()
                return CallResult(
                    ok=False,
                    error={
                        "type": "WorkerDied",
                        "message": (
                            "The worker process exited without answering. This "
                            "usually means the snippet killed its own "
                            "interpreter (os._exit, a segfault in a C "
                            "extension). The session has been reset."
                        ),
                        "traceback": "".join(self._stderr_tail),
                    },
                    session_restarted=True,
                )

            if item.get("id") != request["id"]:
                continue  # response to a request we already gave up on

            if op == "info":
                return CallResult(ok=True, session_restarted=restarted, extra=item)

            return self._build_result(item, "".join(chunks), restarted)

    def _build_result(
        self, payload: dict[str, Any], raw: str, restarted: bool
    ) -> CallResult:
        limit = self._settings.max_output_chars
        stdout, out_cut = _clip(payload.get("stdout", ""), limit)
        stderr, err_cut = _clip(payload.get("stderr", ""), limit)
        raw, raw_cut = _clip(raw, limit)
        return CallResult(
            ok=bool(payload.get("ok")),
            stdout=stdout,
            stderr=stderr,
            result=payload.get("result"),
            error=payload.get("error"),
            duration_ms=float(payload.get("duration_ms") or 0.0),
            raw_output=raw,
            truncated=out_cut or err_cut or raw_cut,
            session_restarted=restarted,
        )

    def _timeout_result(
        self,
        deadline: float,
        chunks: list[str],
        restarted: bool,
        background: bool,
    ) -> CallResult:
        self._terminate()
        if background:
            message = (
                f"Execution exceeded {deadline:g}s and the background worker "
                "was killed. Raise the timeout argument or "
                "PYEXEC_BACKGROUND_MAX_TIMEOUT to allow a longer job."
            )
        else:
            message = (
                f"Execution exceeded {deadline:g}s and the worker was "
                "killed; the session has been reset, so variables defined "
                "before this call are gone. Raise the timeout argument, or "
                "run the work in the background."
            )
        return CallResult(
            ok=False,
            raw_output="".join(chunks),
            timed_out=True,
            session_restarted=True,
            error={
                "type": "TimeoutError",
                "message": message,
                "traceback": "",
            },
        )

    def reset(self) -> None:
        """Drop all state by restarting the worker."""
        if not self._lock.acquire(timeout=LOCK_WAIT):
            raise SessionError(
                "A call is still running in this session; wait for it to finish "
                "before resetting."
            )
        try:
            self._terminate()
            self._spawn()
        finally:
            self._lock.release()


def _clip(text: str, limit: int) -> tuple[str, bool]:
    """Truncate *text* to *limit* characters, marking that we did."""
    if len(text) <= limit:
        return text, False
    dropped = len(text) - limit
    return f"{text[:limit]}\n... [{dropped} more characters truncated]", True


def run_isolated(settings: Settings, code: str, timeout: float | None) -> CallResult:
    """Run *code* in a throwaway worker that is discarded afterwards."""
    session = ExecutionSession(settings)
    try:
        return session.request("exec", timeout=timeout, code=code)
    finally:
        session.close()
