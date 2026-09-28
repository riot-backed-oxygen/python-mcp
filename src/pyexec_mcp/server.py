"""MCP tools for running Python.

The server exposes one persistent session (fast iteration, state carries over)
alongside isolated one-shot execution (clean room, nothing leaks), plus the
file and dependency operations needed to do real work in the workspace.

Tools and resources are declared here; execution lives in
:mod:`pyexec_mcp.session` and the guardrails in :mod:`pyexec_mcp.sandbox`.
"""

from __future__ import annotations

import atexit
import logging
import os
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import jobs, packages, telegram
from .auth import BearerTokenMiddleware
from .config import Settings, is_loopback
from .downloads import DOWNLOAD_PREFIX, DownloadRouter, build_url, workspace_relative
from .jobs import JobLimitReached, JobManager
from .sandbox import GuardrailError, check_code, resolve_within
from .session import ExecutionSession, SessionError, run_isolated

SERVER_NAME = "pyexec_mcp"

logger = logging.getLogger(__name__)

mcp = MCPServer(
    SERVER_NAME,
    instructions=(
        "Runs Python code on this machine. pyexec_run keeps state between calls "
        "like a notebook; pyexec_run_isolated starts from a clean interpreter "
        "every time. Prefer pyexec_run for multi-step work, pyexec_run_isolated "
        "when a fresh environment matters, and pyexec_run_script for a file "
        "already on disk. This is not a sandbox: code has the same access to "
        "the machine as the server process."
    ),
)

_SETTINGS: Settings | None = None
_SESSION: ExecutionSession | None = None
_JOBS: JobManager | None = None
#: Guards every module-level singleton above, not just _SESSION.
_SESSION_LOCK = threading.Lock()


def settings() -> Settings:
    """The process-wide settings, resolved from the environment on first use."""
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = Settings.from_env()
    return _SETTINGS


def session() -> ExecutionSession:
    """The persistent session, started on first use.

    A module-level singleton rather than a lifespan-managed object: the session
    must survive across calls, and this keeps the server usable from tests that
    invoke tools directly.
    """
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            _SESSION = ExecutionSession(settings())
        return _SESSION


def job_manager() -> JobManager:
    """The background job registry, started on first use.

    Built against the settings resolved at that moment, and rebuilt whenever
    :func:`use_settings` swaps them: the concurrency cap and the history size
    are configuration, and a job's worker has the workspace of its own
    configuration as its working directory, so its records cannot be carried
    across a change of one.
    """
    global _JOBS
    with _SESSION_LOCK:
        if _JOBS is None:
            _JOBS = JobManager(settings(), on_finish=_announce_job)
        return _JOBS


@atexit.register
def _close_session() -> None:
    if _SESSION is not None:
        _SESSION.close()
    if _JOBS is not None:
        _JOBS.shutdown()


def use_settings(new_settings: Settings) -> None:
    """Swap in a different configuration, discarding the current session.

    Running background jobs go with it. Their workers were started in the old
    workspace, their artifact links were signed against the old address, and
    their completion messages were addressed to whatever chat was configured
    then -- so leaving them to finish would have them report on a world that no
    longer exists. They are killed rather than forgotten, so no half-written
    file is left with a process still adding to it.

    Exists so tests (and embedders) can point the server at a temporary
    workspace without going through the environment.
    """
    global _SETTINGS, _SESSION, _JOBS
    with _SESSION_LOCK:
        if _SESSION is not None:
            _SESSION.close()
        _SESSION = None
        _SETTINGS = new_settings
        # Taken out of the lock before shutting it down: killing workers waits
        # on processes, and nothing else should queue behind that.
        manager, _JOBS = _JOBS, None
    if manager is not None:
        manager.shutdown()


# --------------------------------------------------------------------------
# Output models
# --------------------------------------------------------------------------

class ExecutionError(BaseModel):
    """A failure raised by the executed code, or by the harness around it."""

    type: str = Field(description="Exception class, e.g. 'ValueError' or 'TimeoutError'")
    message: str = Field(description="The exception message")
    traceback: str = Field(
        default="", description="Formatted traceback; empty for harness errors"
    )


class ExecutionResult(BaseModel):
    """Outcome of one snippet."""

    ok: bool = Field(description="True only if the snippet ran to completion")
    stdout: str = Field(description="Captured print output")
    stderr: str = Field(description="Captured stderr and warnings")
    result: str | None = Field(
        default=None,
        description="repr() of a trailing expression, as the REPL would show it",
    )
    error: ExecutionError | None = Field(default=None, description="Set when ok is False")
    duration_ms: float = Field(description="Wall-clock time spent executing")
    raw_output: str = Field(
        default="",
        description="Output written straight to the file descriptor, bypassing print",
    )
    truncated: bool = Field(
        default=False, description="True if any stream hit the output cap"
    )
    session_restarted: bool = Field(
        default=False,
        description="True if the session was (re)started for this call, so earlier variables are gone",
    )


class SessionInfo(BaseModel):
    """What the persistent session currently holds."""

    alive: bool = Field(description="Whether a worker process is running")
    pid: int | None = Field(default=None, description="Worker process id")
    python_version: str | None = Field(default=None)
    cwd: str = Field(description="Working directory of the worker")
    workspace: str = Field(description="Root the file tools are confined to")
    names: list[str] = Field(
        default_factory=list, description="Variables and functions defined so far"
    )
    installed_package_count: int | None = Field(default=None)


class FileEntry(BaseModel):
    path: str
    size_bytes: int
    is_dir: bool
    modified: float = Field(description="Unix mtime")


class FileListing(BaseModel):
    root: str
    pattern: str | None = None
    count: int
    has_more: bool = Field(description="True if the result was cut off by max_results")
    entries: list[FileEntry]


class FileContent(BaseModel):
    path: str
    size_bytes: int
    encoding: str
    truncated: bool
    content: str


class WriteReceipt(BaseModel):
    path: str
    bytes_written: int
    created: bool = Field(description="True if the file did not exist before")


class DownloadLink(BaseModel):
    """A time-limited URL that downloads one file, for a browser to click."""

    url: str = Field(description="Complete URL; opening it downloads the file")
    path: str = Field(description="Workspace-relative path the link was signed for")
    filename: str = Field(description="Name the browser will save it as")
    size_bytes: int
    expires_at: str = Field(description="ISO 8601 UTC time the link stops working")
    expires_in_seconds: int
    note: str = Field(description="Caveats the caller should know about this link")


class PackageList(BaseModel):
    executable: str
    count: int
    packages: list[str]


class InstallReceipt(BaseModel):
    ok: bool
    command: str
    returncode: int
    stdout: str
    stderr: str


class NotifyReceipt(BaseModel):
    """Whether a message reached the configured Telegram chat."""

    ok: bool = Field(description="True if Telegram accepted the message")
    reason: str = Field(
        default="",
        description="Why the message did not go out, when ok is false",
    )


class JobReceipt(BaseModel):
    """What a background submission produced, whether or not it was accepted."""

    ok: bool = Field(
        description="True if the job was registered and is now running. False means nothing was started."
    )
    job_id: str = Field(default="", description="Handle for the pyexec_job_* tools")
    tool: str = Field(default="", description="The tool the job is running")
    subject: str = Field(
        default="", description="What it runs against: a script path, or a label"
    )
    state: str = Field(default="", description="Lifecycle state as of this reply")
    timeout_s: float = Field(
        default=0.0, description="Deadline the job runs under, after clamping"
    )
    artifacts: list[str] = Field(
        default_factory=list,
        description="Workspace-relative paths the completion message will link to",
    )
    notifies: bool = Field(
        default=False,
        description=(
            "True if Telegram is configured, so this job's completion will be "
            "announced. When false nothing will report on it: poll "
            "pyexec_job_status, or tell whoever asked for the job to check back."
        ),
    )
    reason: str = Field(
        default="", description="Why nothing was started, when ok is false"
    )


class JobStatus(BaseModel):
    """Where a background job has got to."""

    job_id: str
    tool: str
    subject: str = ""
    state: Literal["running", "done", "cancelled"]
    ok: bool | None = Field(
        default=None,
        description=(
            "The outcome. None while the job is still running -- deliberately "
            "not false, which would read as a failure."
        ),
    )
    started_at: str = Field(default="", description="ISO 8601 UTC time it was submitted")
    finished_at: str | None = Field(default=None, description="ISO 8601 UTC, once it ends")
    duration_s: float = Field(
        default=0.0,
        description="Wall-clock seconds so far for a running job, total for a finished one",
    )
    timeout_s: float = 0.0
    error: str = Field(default="", description="'Type: message', set when the job failed")
    excerpt: str = Field(default="", description="Tail of the output, for reading in a chat")
    truncated: bool = False
    artifacts: list[str] = Field(
        default_factory=list, description="Paths the job declared it would produce"
    )
    missing_artifacts: list[str] = Field(
        default_factory=list,
        description=(
            "Declared paths that were not there once the job finished. Empty "
            "while it is still running -- a file it has not written yet is not "
            "missing, it is pending."
        ),
    )
    notified: bool = Field(
        default=False,
        description=(
            "True if a Telegram message about this job was handed to the "
            "delivery thread. Handed, not delivered: delivery failures are "
            "logged rather than reported, so this cannot mean 'delivered'."
        ),
    )
    note: str = Field(default="", description="The caveat that matters for this state")


class JobListing(BaseModel):
    """The background jobs this server still knows about."""

    count: int
    running: int = Field(description="How many are going right now")
    limit: int = Field(description="How many may run at once")
    history: int = Field(description="How many finished jobs are remembered")
    jobs: list[JobStatus]


class JobResult(ExecutionResult):
    """A finished job's full output.

    Extends the shape a blocking call returns, so a caller that already knows
    how to read one knows how to read this.
    """

    job_id: str = ""
    state: str = ""
    artifacts: list[DownloadLink] = Field(
        default_factory=list,
        description="Signed links, rebuilt per call, for the artifacts that exist",
    )
    missing_artifacts: list[str] = Field(
        default_factory=list,
        description="Declared paths that were not there when the job finished",
    )
    note: str = Field(default="", description="Caveats about this particular job")


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------

def _guard(code: str) -> None:
    """Apply the configured import allowlist, if any."""
    check_code(code, settings().allowed_modules)


def _to_result(call) -> ExecutionResult:
    return ExecutionResult(
        ok=call.ok,
        stdout=call.stdout,
        stderr=call.stderr,
        result=call.result,
        error=ExecutionError(**call.error) if call.error else None,
        duration_ms=call.duration_ms,
        raw_output=call.raw_output,
        truncated=call.truncated,
        session_restarted=call.session_restarted,
    )


def _guardrail_failure(exc: GuardrailError) -> ExecutionResult:
    return ExecutionResult(
        ok=False,
        stdout="",
        stderr="",
        error=ExecutionError(type="GuardrailError", message=str(exc)),
        duration_ms=0.0,
    )


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------

def _summarise(
    tool: str, result: ExecutionResult, duration_s: float, subject: str = ""
) -> telegram.ExecutionSummary:
    """Describe a finished run in the terms a notification needs."""
    return telegram.ExecutionSummary(
        tool=tool,
        subject=subject,
        ok=result.ok,
        duration_s=duration_s,
        error=(
            f"{result.error.type}: {result.error.message}"
            if result.error is not None
            else ""
        ),
        excerpt=_excerpt(result),
        truncated=result.truncated,
        machine=telegram.machine_name(),
    )


def _excerpt(result: ExecutionResult) -> str:
    """The tail of what a run produced, sized for a chat message.

    The tail and not the head: the part worth reading is where the run ended up
    -- the final total, the last progress line, the exception -- and Telegram
    imposes a hard length limit regardless.
    """
    body = "\n".join(
        part
        for part in (result.stdout, result.stderr, result.raw_output)
        if part.strip()
    ).strip()

    if not body:
        # Nothing was printed, so the run's point was its value -- a computed
        # total, a shape. Clipped from the front, since a repr leads with the
        # structure that identifies it.
        value = result.result or ""
        if len(value) <= telegram.EXCERPT_CHARS:
            return value
        return value[: telegram.EXCERPT_CHARS] + "..."

    if len(body) <= telegram.EXCERPT_CHARS:
        return body
    return "..." + body[-telegram.EXCERPT_CHARS :]


def _announce(
    tool: str, result: ExecutionResult, duration_s: float, subject: str = ""
) -> None:
    """Tell Telegram a run finished, when it ran long enough to want knowing.

    Wall-clock time, not the worker's own measure of the snippet: what makes a
    run worth a notification is how long its caller was left waiting, which
    includes starting the interpreter and getting the output back.

    Never raises. The run's outcome is settled by the time this is called, and a
    chat message that cannot be delivered must not turn it into a failure.
    """
    try:
        telegram.notify(settings(), _summarise(tool, result, duration_s, subject))
    except Exception:  # noqa: BLE001 - a notification is strictly best effort
        logger.exception("Could not queue the Telegram notification")


# --------------------------------------------------------------------------
# Background jobs
# --------------------------------------------------------------------------

def _iso_utc(when: float | None) -> str | None:
    """Unix seconds as ISO 8601 UTC, the shape ``DownloadLink.expires_at`` uses."""
    if when is None:
        return None
    return (
        datetime.fromtimestamp(when, timezone.utc).isoformat().replace("+00:00", "Z")
    )


def _download_link(
    current: Settings, target: Path, ttl_seconds: float | None = None
) -> DownloadLink:
    """Build one signed, time-limited link to *target*.

    The single place that knows how a link is shaped. Both
    ``pyexec_get_download_url`` and a background job's artifacts come through
    here, so a link looks the same whichever handed it out.
    """
    relative = workspace_relative(target, current.workspace)
    url, expires = build_url(
        current.base_url,
        relative,
        current.auth_token,
        current.clamp_download_ttl(ttl_seconds),
    )

    if current.auth_token is None:
        note = (
            "This server has no PYEXEC_AUTH_TOKEN, so the link is unsigned. "
            "That is only possible on a loopback bind, which means the link "
            "works on this machine and nowhere else."
        )
    else:
        note = (
            "Anyone holding this URL can download the file until it expires, "
            "so share it only with the person who asked for it."
        )

    return DownloadLink(
        url=url,
        path=relative,
        filename=target.name,
        size_bytes=target.stat().st_size,
        expires_at=_iso_utc(expires) or "",
        expires_in_seconds=max(0, int(expires - time.time())),
        note=note,
    )


def _declare_artifacts(
    artifacts: list[str] | None, current: Settings
) -> tuple[str, ...]:
    """Normalise declared artifact paths, refusing any that escape the workspace.

    Stored as workspace-relative posix paths -- the spelling a download link
    signs, so the same file cannot be named two ways -- but deliberately not
    required to exist: producing them is the job.

    Raises:
        GuardrailError: if a path resolves outside the workspace. Checked here,
            at submit, so a job that could never have produced the file is not
            started only to fail an hour later.
    """
    declared: list[str] = []
    for path in artifacts or []:
        relative = workspace_relative(
            resolve_within(path, current.workspace), current.workspace
        )
        if relative not in declared:
            declared.append(relative)
    return tuple(declared)


def _artifact_state(
    job: jobs.Job, current: Settings
) -> tuple[list[DownloadLink], list[str]]:
    """Links for the artifacts that exist, and the declared ones that do not.

    Never raises. A job that declared a file it never wrote, or a server with no
    address to build links from, must still report its outcome -- finishing is
    the point, and the links are a convenience.
    """
    if not current.base_url:
        # Nothing to build a link from. The files may well be sitting there;
        # there is simply no address a person could reach them at, and calling
        # them "missing" would be a lie about the job.
        return [], []

    links: list[DownloadLink] = []
    missing: list[str] = []
    for relative in job.artifacts:
        try:
            target = resolve_within(relative, current.workspace)
            if not target.is_file():
                missing.append(relative)
                continue
            links.append(_download_link(current, target))
        except (GuardrailError, OSError):
            missing.append(relative)
    return links, missing


def _job_note(job: jobs.Job) -> str:
    """The caveat that matters for where this job has got to."""
    if job.state == "cancelled":
        return (
            "This job was cancelled and its worker was killed. A killed process "
            "gets no chance to flush a buffer or close a file, so any artifact "
            "it declared may be incomplete. Processes the job started itself "
            "were not killed with it."
        )
    if job.state == "running":
        return "Still running; there is no result to fetch yet."
    return ""


def _announce_job(job: jobs.Job, result, duration_s: float) -> bool:
    """Tell Telegram a background job landed. Returns whether it was queued.

    Queued rather than delivered: :func:`telegram.notify` hands the message to a
    daemon thread and reports only that it did. Delivery failures are logged, by
    the same rule that makes a notification unable to change a result.

    Never raises, and it cannot: the job's result is stored before this is
    called, so a chat message that cannot be delivered must not turn a finished
    job into a failed one.

    Sent with ``always=True``, bypassing ``PYEXEC_TELEGRAM_MIN_DURATION``. The
    threshold exists because a blocking call's caller already holds the result,
    so a message about a short run is a duplicate; a job's caller has explicitly
    stopped watching, so this message is the only report there will be.
    """
    current = settings()
    links, _ = _artifact_state(job, current)
    summary = replace(
        _summarise(job.tool, _to_result(result), duration_s, job.subject),
        job_id=job.id,
        links=tuple(link.url for link in links),
    )
    try:
        return telegram.notify(current, summary, always=True) is not None
    except Exception:  # noqa: BLE001 - a notification is strictly best effort
        logger.exception("Could not queue the Telegram notification for job %s", job.id)
        return False


def _job_status(job: jobs.Job, current: Settings) -> JobStatus:
    """Describe a job in the terms a status poll reads."""
    finished = job.state != "running"
    result = _to_result(job.result) if job.result is not None else None
    _, missing = _artifact_state(job, current)

    return JobStatus(
        job_id=job.id,
        tool=job.tool,
        subject=job.subject,
        state=job.state,
        # None, not False, while it is still going: a caller reads False as
        # "it failed" and would say so.
        ok=bool(result is not None and result.ok) if finished else None,
        started_at=_iso_utc(job.started_at) or "",
        finished_at=_iso_utc(job.finished_at),
        duration_s=job.duration_s(),
        timeout_s=job.timeout,
        error=(
            f"{result.error.type}: {result.error.message}"
            if result is not None and result.error is not None
            else ""
        ),
        excerpt=_excerpt(result) if result is not None else "",
        truncated=bool(result is not None and result.truncated),
        artifacts=list(job.artifacts),
        missing_artifacts=missing if finished else [],
        notified=job.notified,
        note=_job_note(job),
    )


def _unknown_job(job_id: str) -> ToolError:
    """The error a lookup raises for an id this process does not know.

    It cannot tell "never existed" from "finished and was forgotten" from "the
    server restarted", because all three leave no record -- so it says all
    three rather than guessing at one.
    """
    return ToolError(
        f"No such job: {job_id!r}. It may never have existed, it may have "
        f"finished long enough ago to be forgotten (the last "
        f"{settings().job_history} are kept), or the server may have restarted "
        "since. Job records live in memory only. pyexec_job_status with no "
        "argument lists what is still known."
    )


# --------------------------------------------------------------------------
# Execution tools
# --------------------------------------------------------------------------

@mcp.tool(
    name="pyexec_run",
    title="Run Python in the persistent session",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
def pyexec_run(
    code: Annotated[
        str,
        Field(
            description=(
                "Python source to execute. Variables, imports and function "
                "definitions persist for later pyexec_run calls in this session."
            )
        ),
    ],
    timeout: Annotated[
        float | None,
        Field(
            description="Seconds before the snippet is interrupted. Defaults to the configured 30s."
        ),
    ] = None,
) -> ExecutionResult:
    """Execute Python in the long-lived session and return what it produced.

    State carries over between calls: names defined by one snippet are visible
    to the next, which makes this the right tool for iterative work -- load some
    data, inspect it, adjust, plot it. Use pyexec_reset_session to start over.

    Args:
        code: Python source. A trailing expression is evaluated and its repr
            returned in `result`, exactly as a REPL would print it.
        timeout: Seconds before the snippet is interrupted.

    Returns:
        ExecutionResult: `ok` plus the captured `stdout`, `stderr`, the `repr`
        of any trailing expression in `result`, and an `error` object with the
        exception type, message and traceback when something failed. Set
        `truncated` if output hit the cap.

        On timeout the outcome depends on why the snippet overran, and
        `session_restarted` says which happened: a pure-Python loop is
        interrupted in place, leaving the session and its variables intact
        (`session_restarted` false), while a snippet wedged in a blocking C call
        cannot be interrupted and costs the worker its life
        (`session_restarted` true, earlier variables gone). Check that field
        rather than assuming.

    Examples:
        - "Load this CSV and show me the head" -> read the file, then
          `pyexec_run("import pandas as pd; df = pd.read_csv('x.csv'); df.head()")`
        - "What was the mean of that column?" -> the variable is still around,
          so `pyexec_run("df['col'].mean()")` just works.
        - Don't use when: the snippet must not see earlier state, or may hang or
          crash the interpreter -- use pyexec_run_isolated instead.
    """
    try:
        _guard(code)
    except GuardrailError as exc:
        return _guardrail_failure(exc)

    deadline = settings().clamp_timeout(timeout)
    started = time.monotonic()
    result = _to_result(session().request("exec", timeout=deadline, code=code))
    _announce("pyexec_run", result, time.monotonic() - started)
    return result


@mcp.tool(
    name="pyexec_run_isolated",
    title="Run Python in a throwaway interpreter",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
def pyexec_run_isolated(
    code: Annotated[
        str, Field(description="Python source to execute in a fresh interpreter.")
    ],
    timeout: Annotated[
        float | None, Field(description="Seconds before the process is killed.")
    ] = None,
) -> ExecutionResult:
    """Execute Python in a brand-new interpreter that is discarded afterwards.

    Nothing is shared with the persistent session: no variables, no imported
    modules, no monkey-patching. Use this to check that a snippet stands on its
    own, to run something that might corrupt global state or call `os._exit`,
    or to reproduce a bug free of whatever the session has accumulated.

    Args:
        code: Python source. A trailing expression is evaluated and returned.
        timeout: Seconds before the interpreter is killed and the call fails.

    Returns:
        ExecutionResult: Same shape as pyexec_run. `session_restarted` is not
        meaningful here, since every call gets a fresh process.

    Examples:
        - "Verify this snippet works from a clean slate" -> pyexec_run_isolated
        - "This keeps failing in the session, is it my state?" -> run the same
          code through both tools and compare.
        - Don't use when: you need values from earlier calls -- use pyexec_run.
    """
    try:
        _guard(code)
    except GuardrailError as exc:
        return _guardrail_failure(exc)

    deadline = settings().clamp_timeout(timeout)
    started = time.monotonic()
    result = _to_result(run_isolated(settings(), code, deadline))
    _announce("pyexec_run_isolated", result, time.monotonic() - started)
    return result


@mcp.tool(
    name="pyexec_reset_session",
    title="Reset the persistent session",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def pyexec_reset_session() -> SessionInfo:
    """Restart the persistent session, discarding every variable and import.

    Use this when the session has accumulated state you no longer want, when a
    previous snippet left a module patched or a file handle open, or before a
    run whose result you need to be reproducible. Isolated runs are unaffected,
    since they never shared this state.

    Returns:
        SessionInfo: The state of the freshly started session.

    Examples:
        - "Start clean before the benchmark" -> pyexec_reset_session, then run.
        - Don't use when: you only want a fresh interpreter for one snippet --
          pyexec_run_isolated does that without disturbing the session.
    """
    session().reset()
    return _session_info()


@mcp.tool(
    name="pyexec_session_info",
    title="Inspect the persistent session",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def pyexec_session_info() -> SessionInfo:
    """Report the interpreter backing the persistent session and what it holds.

    Call this before redefining something, to check whether a name already
    exists, or to confirm which interpreter and working directory are in play.

    Returns:
        SessionInfo: The worker's pid, Python version and working directory,
        the workspace root, and the variables and functions currently defined.

    Examples:
        - "What do I have loaded?" -> pyexec_session_info lists defined names.
        - "Which Python is this?" -> python_version and cwd.
    """
    return _session_info()


def _session_info() -> SessionInfo:
    current = settings()
    live = session()
    base = {
        "alive": live.alive,
        "cwd": str(current.workspace),
        "workspace": str(current.workspace),
    }
    if not live.alive:
        return SessionInfo(**base)

    result = live.request("info", timeout=10)
    return SessionInfo(
        **base,
        pid=result.extra.get("pid"),
        python_version=result.extra.get("python_version"),
        names=result.extra.get("names") or [],
    )


# --------------------------------------------------------------------------
# Script and file tools
# --------------------------------------------------------------------------

@mcp.tool(
    name="pyexec_run_script",
    title="Run a Python file",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
def pyexec_run_script(
    path: Annotated[
        str, Field(description="Script path, absolute or relative to the workspace.")
    ],
    args: Annotated[
        list[str] | None,
        Field(description="Command-line arguments passed to the script."),
    ] = None,
    timeout: Annotated[
        float | None, Field(description="Seconds before the process is killed.")
    ] = None,
) -> ExecutionResult:
    """Run a Python file on disk as `__main__`, and capture what it prints.

    Unlike pyexec_run this is a real interpreter invocation with `sys.argv`,
    `__file__` and `if __name__ == "__main__"` behaving normally, so it is the
    right way to run a script that already exists rather than a snippet you are
    composing. It runs in its own process with the workspace as the working
    directory.

    Args:
        path: Script to run. Paths outside the workspace are rejected.
        args: Arguments placed in `sys.argv[1:]`.
        timeout: Seconds before the process is killed and the call fails.

    Returns:
        ExecutionResult: `stdout` and `stderr` as the script produced them, and
        `ok` false with the exit code in `error.message` if it failed. `result`
        is always None -- a script has no trailing expression.

    Examples:
        - "Run the test suite" -> pyexec_run_script("tests/run.py")
        - "Try the CLI with these flags" -> args=["--verbose", "input.txt"]
        - Don't use when: the code is a snippet, not a file -- use pyexec_run.
    """
    current = settings()
    try:
        script = resolve_within(path, current.workspace)
    except GuardrailError as exc:
        return _guardrail_failure(exc)

    if not script.is_file():
        return ExecutionResult(
            ok=False,
            stdout="",
            stderr="",
            error=ExecutionError(
                type="FileNotFoundError",
                message=f"No such file: {script}. Use pyexec_list_files to see what is in the workspace.",
            ),
            duration_ms=0.0,
        )

    started = time.monotonic()
    call = run_isolated(
        current, _script_invocation(script, args or []), current.clamp_timeout(timeout)
    )
    result = _to_result(call)
    _announce(
        "pyexec_run_script",
        result,
        time.monotonic() - started,
        subject=_display_path(script, current.workspace),
    )
    return result


def _script_invocation(script: Path, args: list[str]) -> str:
    """Build a snippet that runs *script* the way the CLI would.

    ``runpy`` is used rather than a plain ``exec`` so that ``__file__``,
    ``sys.argv`` and the ``__main__`` guard all behave normally.
    """
    import json

    return (
        "import runpy, sys\n"
        f"sys.argv = [{json.dumps(str(script))}] + {json.dumps(args)}\n"
        f"runpy.run_path({json.dumps(str(script))}, run_name='__main__')\n"
    )


@mcp.tool(
    name="pyexec_read_file",
    title="Read a workspace file",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def pyexec_read_file(
    path: Annotated[
        str, Field(description="File path, absolute or relative to the workspace.")
    ],
    max_bytes: Annotated[
        int, Field(description="Read at most this many bytes.", ge=1, le=5_000_000)
    ] = 100_000,
    encoding: Annotated[
        str, Field(description="Text encoding to decode with.")
    ] = "utf-8",
) -> FileContent:
    """Read a text file from the workspace.

    Reads are confined to the workspace root; a path that resolves outside it is
    refused. For binary files, read them from executed code instead.

    Args:
        path: File to read.
        max_bytes: Cap on how much is read; the result is marked `truncated`
            when the file is larger.
        encoding: Codec used to decode the bytes.

    Returns:
        FileContent: The decoded `content` plus `size_bytes` and `truncated`.

    Examples:
        - "What's in config.yaml?" -> pyexec_read_file("config.yaml")
        - Don't use when: you want to analyse the file in Python -- reading it
          inside pyexec_run avoids a round trip through the model.
    """
    try:
        target = resolve_within(path, settings().workspace)
    except GuardrailError as exc:
        raise ValueError(str(exc)) from exc

    if not target.is_file():
        raise ValueError(f"No such file: {target}")

    size = target.stat().st_size
    with target.open("rb") as handle:
        raw = handle.read(max_bytes)

    return FileContent(
        path=str(target),
        size_bytes=size,
        encoding=encoding,
        truncated=size > len(raw),
        content=raw.decode(encoding, errors="replace"),
    )


@mcp.tool(
    name="pyexec_write_file",
    title="Write a workspace file",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def pyexec_write_file(
    path: Annotated[
        str, Field(description="File path, absolute or relative to the workspace.")
    ],
    content: Annotated[str, Field(description="Text to write.")],
    encoding: Annotated[
        str, Field(description="Text encoding to encode with.")
    ] = "utf-8",
    overwrite: Annotated[
        bool, Field(description="Set false to refuse to replace an existing file.")
    ] = True,
    create_parents: Annotated[
        bool, Field(description="Create missing parent directories.")
    ] = True,
) -> WriteReceipt:
    """Write a text file inside the workspace.

    Writes are confined to the workspace root. Pass `overwrite=false` when you
    mean to create a new file and would rather fail than clobber one that is
    already there.

    Args:
        path: Destination path.
        content: Text to write.
        encoding: Codec used to encode the text.
        overwrite: Refuse to replace an existing file when false.
        create_parents: Create missing parent directories.

    Returns:
        WriteReceipt: The resolved `path`, `bytes_written`, and `created` (true
        when the file did not exist before).

    Examples:
        - "Save this as analyse.py" -> pyexec_write_file("analyse.py", src)
        - Don't use when: the content is generated by code you are already
          running -- write it from inside the snippet instead.
    """
    try:
        target = resolve_within(path, settings().workspace)
    except GuardrailError as exc:
        raise ValueError(str(exc)) from exc

    existed = target.exists()
    if existed and not overwrite:
        raise ValueError(
            f"{target} already exists. Pass overwrite=true to replace it, or "
            "choose another path."
        )

    if create_parents:
        target.parent.mkdir(parents=True, exist_ok=True)

    data = content.encode(encoding)
    target.write_bytes(data)

    return WriteReceipt(
        path=str(target), bytes_written=len(data), created=not existed
    )


def _display_path(item: Path, workspace: Path) -> str:
    """Path relative to the workspace when possible, always with '/' separators."""
    try:
        return item.relative_to(workspace).as_posix()
    except ValueError:
        return item.as_posix()


@mcp.tool(
    name="pyexec_list_files",
    title="List workspace files",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def pyexec_list_files(
    path: Annotated[
        str, Field(description="Directory to list, relative to the workspace.")
    ] = ".",
    pattern: Annotated[
        str | None,
        Field(description="Glob to filter by, e.g. '*.csv' or '**/*.py'."),
    ] = None,
    recursive: Annotated[
        bool, Field(description="Walk subdirectories.")
    ] = False,
    max_results: Annotated[
        int, Field(description="Stop after this many entries.", ge=1, le=5_000)
    ] = 200,
    include_dirs: Annotated[
        bool, Field(description="Include directories.")
    ] = True,
) -> FileListing:
    """List files in the workspace, newest-agnostic and ordered by path.

    Use it to discover what is available before reading or running something,
    and to confirm where a previous write landed.

    Args:
        path: Directory to list. Must resolve inside the workspace.
        pattern: Optional glob. Combined with `recursive`, `'**/*.py'` walks the
            whole tree.
        recursive: Recurse into subdirectories.
        max_results: Cap on entries returned; `has_more` flags a truncated list.
        include_dirs: Include directories as well as files.

    Returns:
        FileListing: `entries` with size and mtime, plus `count` and `has_more`.

    Examples:
        - "What data files are here?" -> pyexec_list_files(pattern="*.csv")
        - "Show me the source tree" -> recursive=true, pattern="**/*.py"
    """
    current = settings()
    try:
        root = resolve_within(path, current.workspace)
    except GuardrailError as exc:
        raise ValueError(str(exc)) from exc

    if not root.is_dir():
        raise ValueError(f"Not a directory: {root}")

    if pattern:
        candidates = root.rglob(pattern) if recursive else root.glob(pattern)
    else:
        candidates = root.rglob("*") if recursive else root.iterdir()

    entries: list[FileEntry] = []
    has_more = False
    for item in sorted(candidates, key=lambda p: str(p)):
        try:
            stat = item.stat()
        except OSError:
            continue
        if item.is_dir() and not include_dirs:
            continue
        if len(entries) >= max_results:
            has_more = True
            break
        entries.append(
            FileEntry(
                # Reported relative to the workspace with forward slashes, so a
                # listing looks the same on Windows as it does elsewhere.
                path=_display_path(item, current.workspace),
                size_bytes=stat.st_size,
                is_dir=item.is_dir(),
                modified=stat.st_mtime,
            )
        )

    return FileListing(
        root=str(root),
        pattern=pattern,
        count=len(entries),
        has_more=has_more,
        entries=entries,
    )


# --------------------------------------------------------------------------
# Download tool
# --------------------------------------------------------------------------

@mcp.tool(
    name="pyexec_get_download_url",
    title="Get a download link for a workspace file",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
def pyexec_get_download_url(
    path: Annotated[
        str, Field(description="File path, absolute or relative to the workspace.")
    ],
    ttl_seconds: Annotated[
        float | None,
        Field(description="How long the link should stay valid, in seconds."),
    ] = None,
) -> DownloadLink:
    """Build a URL that downloads a file from the workspace.

    Use this to hand a person something they can actually open -- a chart, a
    report, an archive, anything binary or too large to usefully pass through
    the conversation. The URL goes straight to their browser; the file never
    travels through the model context.

    Only works on the streamable-http transport, since a stdio server has no
    address to link to. The link is signed and time-limited: it works for
    `ttl_seconds` (default 1 hour) and grants access to that one file only.
    Rotating PYEXEC_AUTH_TOKEN invalidates every link already handed out.

    Args:
        path: File to link to. Must resolve inside the workspace, like the
            other file tools.
        ttl_seconds: Requested lifetime. Capped at the configured
            PYEXEC_DOWNLOAD_TTL, which is the ceiling rather than a default.

    Returns:
        DownloadLink: `url` plus the `filename`, `size_bytes` and
        `expires_at` the caller needs to describe it. `note` carries any
        caveat about this particular link.

    Examples:
        - "Show me the chart you just made" -> plot it to a .png from code,
          then return the URL from pyexec_get_download_url("chart.png").
        - "Export the results as csv" -> write the file, then link it.
        - "Send me the report when it is ready" -> pyexec_notify can carry the
          URL to Telegram, for someone who is no longer watching this session.
        - Don't use when: the content is small text you want to read anyway --
          pyexec_read_file puts it where you can reason about it.

    Raises:
        ToolError: if the path is outside the workspace, is not a file, or the
            server has no address to build a link from. Raised as a ToolError
            rather than a plain exception so the reason reaches you: anything
            else arrives as a nameless "Error executing tool" with the
            explanation stripped out.
    """
    current = settings()
    if not current.base_url:
        raise ToolError(
            "This server has no address to build a download link from. Set "
            "PYEXEC_BASE_URL to the address clients use to reach it, e.g. "
            "http://192.168.1.10:8000, and restart."
        )

    try:
        target = resolve_within(path, current.workspace)
    except GuardrailError as exc:
        raise ToolError(str(exc)) from exc

    if not target.is_file():
        raise ToolError(
            f"No such file: {target}. Use pyexec_list_files to see what is in "
            "the workspace."
        )

    return _download_link(current, target, ttl_seconds)


# --------------------------------------------------------------------------
# Dependency tools
# --------------------------------------------------------------------------

@mcp.tool(
    name="pyexec_list_packages",
    title="List installed packages",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def pyexec_list_packages(
    match: Annotated[
        str | None,
        Field(description="Case-insensitive substring filter, e.g. 'pandas'."),
    ] = None,
) -> PackageList:
    """List the Python packages available to executed code.

    Check here before importing something, and before installing: it tells you
    whether the dependency is already satisfied.

    Args:
        match: Optional substring filter over `name==version`.

    Returns:
        PackageList: `packages` as `name==version` strings, and the interpreter
        they are installed in.

    Examples:
        - "Is pandas available?" -> pyexec_list_packages(match="pandas")
        - "What's installed?" -> pyexec_list_packages()
    """
    current = settings()
    found = packages.list_installed(current, match)
    return PackageList(
        executable=current.python_executable, count=len(found), packages=found
    )


@mcp.tool(
    name="pyexec_install_package",
    title="Install Python packages",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
def pyexec_install_package(
    packages_to_install: Annotated[
        list[str],
        Field(
            description="Package specifiers, e.g. ['pandas', 'requests==2.31.0'].",
            min_length=1,
            max_length=20,
        ),
    ],
    upgrade: Annotated[
        bool, Field(description="Upgrade the packages if already installed.")
    ] = False,
) -> InstallReceipt:
    """Install packages into the interpreter that executed code uses.

    Installs land in the same interpreter the session runs on, so the very next
    pyexec_run can import them. Only run this when pyexec_list_packages shows
    the dependency is genuinely absent -- installs are slow and they mutate the
    environment for every later call.

    Args:
        packages_to_install: Distribution specifiers; version pins are allowed.
        upgrade: Pass --upgrade to pip.

    Returns:
        InstallReceipt: `ok` plus pip's `stdout` and `stderr`, so a resolution
        failure can be read rather than guessed at.

    Examples:
        - "I need pandas" -> pyexec_install_package(["pandas"])
        - Pin when reproducibility matters -> ["pandas==2.2.0"]
        - Don't use when: pyexec_list_packages already shows it installed.
    """
    result = packages.install(settings(), packages_to_install, upgrade=upgrade)
    return InstallReceipt(
        ok=result.ok,
        command=result.command,
        returncode=result.returncode,
        stdout=result.stdout[-8_000:],
        stderr=result.stderr[-8_000:],
    )


# --------------------------------------------------------------------------
# Notification tool
# --------------------------------------------------------------------------

@mcp.tool(
    name="pyexec_notify",
    title="Send a Telegram message",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
def pyexec_notify(
    message: Annotated[
        str,
        Field(
            description="Text to send. Keep it short; Telegram caps a message at 4096 characters.",
            min_length=1,
            max_length=4000,
        ),
    ],
) -> NotifyReceipt:
    """Send a message to the chat this server is configured to notify.

    Runs over the threshold already announce themselves, so this is for the rest:
    a batch of calls that added up to a long job, a checkpoint worth reporting
    before the long part starts, or an answer that matters enough to interrupt
    someone. It waits for Telegram to answer, so `ok` is the real delivery
    status rather than "queued".

    Only works when the operator configured PYEXEC_TELEGRAM_BOT_TOKEN and
    PYEXEC_TELEGRAM_CHAT_ID; otherwise `ok` is false and `reason` says so. The
    message leaves this machine for Telegram's servers, so treat it as
    publishing, not logging.

    Args:
        message: The text to send, as plain text.

    Returns:
        NotifyReceipt: `ok`, plus `reason` when Telegram refused the message or
        the server has no bot configured. A refusal from Telegram itself quotes
        its own explanation, since "chat not found" and "bot was blocked" are
        fixed in different places.

    Examples:
        - "Tell me when this is done" on a job made of many short calls ->
          pyexec_notify("Finished the 40 backtests, results in results.csv").
        - Don't use when: a single call is already long -- those notify on their
          own, provided the operator set a threshold worth waiting for.
    """
    try:
        telegram.notifier(settings()).send(message)
    except telegram.NotificationError as exc:
        return NotifyReceipt(ok=False, reason=str(exc))
    return NotifyReceipt(ok=True)


# --------------------------------------------------------------------------
# Background job tools
# --------------------------------------------------------------------------

def _job_receipt(job: jobs.Job, current: Settings) -> JobReceipt:
    """What a submit hands back: what was started, and what to expect of it."""
    return JobReceipt(
        ok=True,
        job_id=job.id,
        tool=job.tool,
        subject=job.subject,
        state=job.state,
        timeout_s=job.timeout,
        artifacts=list(job.artifacts),
        notifies=telegram.notifier(current).enabled,
    )


def _refused(tool: str, exc: Exception) -> JobReceipt:
    """A submit that never became a job.

    Deliberately a receipt rather than an exception, and deliberately no job: a
    submission that was never going to work must not start, because a job that
    runs and fails sends a notification about work that should never have begun.
    """
    return JobReceipt(ok=False, tool=tool, reason=f"{type(exc).__name__}: {exc}")


def _require_job(job_id: str) -> jobs.Job:
    """The named job, or a ToolError saying why there is none.

    Raises:
        ToolError: if this process has no such job.
    """
    job = job_manager().get(job_id)
    if job is None:
        raise _unknown_job(job_id)
    return job


def _tail(text: str, limit: int) -> str:
    """The last *limit* characters, marked as clipped."""
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]


@mcp.tool(
    name="pyexec_run_background",
    title="Run Python in the background",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
def pyexec_run_background(
    code: Annotated[
        str,
        Field(
            description=(
                "Python source to execute. Runs in a fresh interpreter with an "
                "empty namespace, not in the persistent session."
            )
        ),
    ],
    timeout: Annotated[
        float | None,
        Field(
            description=(
                "Seconds the job may run before its process is killed. Defaults "
                "to PYEXEC_BACKGROUND_TIMEOUT and is capped at "
                "PYEXEC_MAX_BACKGROUND_TIMEOUT -- a different, much larger range "
                "than a blocking call gets."
            )
        ),
    ] = None,
    artifacts: Annotated[
        list[str] | None,
        Field(
            description=(
                "Workspace-relative files this job is expected to produce, e.g. "
                "['results.csv', 'chart.png']. Those that exist when it finishes "
                "are linked in the completion message and in pyexec_job_result."
            )
        ),
    ] = None,
    label: Annotated[
        str,
        Field(description="Short label for the notification, e.g. 'nightly sweep'."),
    ] = "",
) -> JobReceipt:
    """Start a long run and return at once, with a job id to follow it by.

    Use this when the work will outlast anyone's patience. A blocking call holds
    the tool call open until the code finishes and is killed at
    PYEXEC_MAX_TIMEOUT -- 300 seconds by default -- so an hour-long sweep cannot
    be run that way at all. This returns as soon as the job is registered and
    the work carries on in its own interpreter after the call has returned.

    The job does not share the persistent session. It gets a fresh interpreter,
    exactly as pyexec_run_isolated does, so it can neither read the session's
    variables nor disturb them, and it does not take the session's lock, so jobs
    run alongside each other and alongside pyexec_run. The price of that
    isolation is that the only way to hand data in is through a file.

    When the job finishes, one Telegram message carries its state, how long it
    took, the tail of its output, and links to the artifacts it declared --
    provided a bot token and chat id are configured, which `notifies` in the
    receipt tells you. Unlike a blocking run, that message is not subject to
    PYEXEC_TELEGRAM_MIN_DURATION: a job's caller has explicitly stopped watching,
    so there is no result for the message to duplicate.

    When `notifies` is false, nothing will report on this job and nobody will
    look at it unless something asks. Say so, and hand the job id to whoever is
    waiting: poll pyexec_job_status, or read pyexec_job_result later.

    Args:
        code: Python source, the same shape pyexec_run takes, including the
            trailing expression whose repr comes back in `result`.
        timeout: How long the job may run. Defaults to
            PYEXEC_BACKGROUND_TIMEOUT (an hour) and is capped at
            PYEXEC_MAX_BACKGROUND_TIMEOUT (a day).
        artifacts: Files the job should produce, relative to the workspace.
            Declaring one that is never written is not a failure -- it comes
            back in `missing_artifacts`. Each one that exists gets a signed,
            time-limited download link, the same kind pyexec_get_download_url
            hands out.
        label: A name for the job, for a sweep rather than a snippet. The job id
            is always included in the message either way.

    Returns:
        JobReceipt: `ok` and the `job_id` to follow it by, or `ok` false with a
        `reason` -- a syntax error or a blocked import, an artifact path outside
        the workspace, an interpreter that would not start, or all the
        configured job slots being busy. A refusal never creates a job.

    Examples:
        - "This sweep takes about an hour, tell me when it's done" ->
          pyexec_run_background(code, artifacts=["results/sweep.csv"]) and give
          the human the job id.
        - "Kick off the training run, I'll check back" -> pyexec_run_background,
          then pyexec_job_status(job_id) when asked.
        - Don't use when: the code finishes in seconds -- pyexec_run_isolated
          returns the answer directly, with no registry in between.
        - Don't use when: the job needs the session's variables. Jobs are always
          isolated; write the state to a file and read it from inside the job.
        - Don't use when: the next step depends on the result. A job only works
          when the next step can wait for a notification.
    """
    current = settings()
    try:
        _guard(code)
        declared = _declare_artifacts(artifacts, current)
    except GuardrailError as exc:
        return _refused("pyexec_run_background", exc)

    try:
        job = job_manager().submit(
            tool="pyexec_run_background",
            subject=label,
            code=code,
            timeout=current.clamp_background_timeout(timeout),
            artifacts=declared,
        )
    except (JobLimitReached, SessionError) as exc:
        return _refused("pyexec_run_background", exc)

    return _job_receipt(job, current)


@mcp.tool(
    name="pyexec_run_script_background",
    title="Run a Python file in the background",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
def pyexec_run_script_background(
    path: Annotated[
        str, Field(description="Script path, absolute or relative to the workspace.")
    ],
    args: Annotated[
        list[str] | None,
        Field(description="Command-line arguments passed to the script."),
    ] = None,
    timeout: Annotated[
        float | None,
        Field(
            description=(
                "Seconds the job may run before its process is killed. Defaults "
                "to PYEXEC_BACKGROUND_TIMEOUT and is capped at "
                "PYEXEC_MAX_BACKGROUND_TIMEOUT."
            )
        ),
    ] = None,
    artifacts: Annotated[
        list[str] | None,
        Field(
            description=(
                "Workspace-relative files this job is expected to produce. Those "
                "that exist when it finishes are linked in the notification."
            )
        ),
    ] = None,
) -> JobReceipt:
    """Start a script on disk as a background job, and return at once.

    The background counterpart of pyexec_run_script, and the right tool for a
    sweep that already exists as a file: the worker runs it through `runpy` with
    `sys.argv`, `__file__` and the `__main__` guard behaving normally, in its own
    process, with the workspace as the working directory.

    The file is read when the job starts, which is immediate -- but note that
    editing the script afterwards changes nothing about the run in flight.

    Args:
        path: Script to run. Paths outside the workspace are rejected.
        args: Arguments placed in `sys.argv[1:]`.
        timeout: How long the job may run; see pyexec_run_background.
        artifacts: Files the job should produce; see pyexec_run_background.

    Returns:
        JobReceipt: The same shape pyexec_run_background returns, with `subject`
        set to the script's workspace-relative path so the notification says
        which script finished.

    Examples:
        - "Run the nightly report and ping me" ->
          pyexec_run_script_background("reports/nightly.py",
          artifacts=["out/nightly.csv"]).
        - Don't use when: the script finishes in seconds -- pyexec_run_script
          gives the output directly.
    """
    current = settings()
    try:
        script = resolve_within(path, current.workspace)
        declared = _declare_artifacts(artifacts, current)
    except GuardrailError as exc:
        return _refused("pyexec_run_script_background", exc)

    if not script.is_file():
        return JobReceipt(
            ok=False,
            tool="pyexec_run_script_background",
            reason=(
                f"FileNotFoundError: no such file: {script}. Use "
                "pyexec_list_files to see what is in the workspace."
            ),
        )

    try:
        job = job_manager().submit(
            tool="pyexec_run_script_background",
            subject=_display_path(script, current.workspace),
            code=_script_invocation(script, args or []),
            timeout=current.clamp_background_timeout(timeout),
            artifacts=declared,
        )
    except (JobLimitReached, SessionError) as exc:
        return _refused("pyexec_run_script_background", exc)

    return _job_receipt(job, current)


@mcp.tool(
    name="pyexec_job_status",
    title="Check on background jobs",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def pyexec_job_status(
    job_id: Annotated[
        str | None,
        Field(
            description=(
                "The handle a submit returned. Omit it to list every job this "
                "server still knows about."
            )
        ),
    ] = None,
) -> JobListing:
    """Report where a background job has got to, or list them all.

    This reads the server's own registry and makes no call into any worker, so
    it is cheap and safe to poll -- a running job is reported as `running` no
    matter how long it has been going, and polling it neither slows it down nor
    disturbs the persistent session.

    `ok` is deliberately None while a job is running rather than false, because
    false would read as "it failed". Check `state` for whether there is anything
    to fetch yet.

    Args:
        job_id: A job to look up. Omit to list everything known.

    Returns:
        JobListing: `jobs`, newest first, each with its `state`, elapsed
        `duration_s`, an `excerpt` of the output so far, the artifacts it
        declared and which of those are `missing_artifacts`, and a `note`
        carrying whatever caveat matters for that state. `running` and `limit`
        describe the server as a whole rather than any one job.

    Raises:
        ToolError: if `job_id` names no job this server knows. Job records live
            in memory, so one can also be missing because the server restarted
            or because it finished long enough ago to be forgotten. Raised as a
            ToolError rather than a plain exception because the reason has to
            reach you: anything else is reported to the client as a nameless
            "Error executing tool pyexec_job_status", which tells you nothing
            about which of those three happened.

    Examples:
        - "Is that sweep done yet?" -> pyexec_job_status("job-1a2b3c4d")
        - "What's running?" -> pyexec_job_status()
        - Don't use when: you want the actual output -- pyexec_job_result
          returns it, this only reports where the job got to.
    """
    current = settings()
    manager = job_manager()

    if job_id is None:
        known = manager.all()
    else:
        known = [_require_job(job_id)]

    return JobListing(
        count=len(known),
        running=len(manager.running()),
        limit=manager.limit,
        history=manager.history,
        jobs=[_job_status(job, current) for job in known],
    )


@mcp.tool(
    name="pyexec_job_result",
    title="Fetch a finished job's output",
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def pyexec_job_result(
    job_id: Annotated[str, Field(description="The handle a submit returned.")],
    max_chars: Annotated[
        int,
        Field(
            description=(
                "Return only the last N characters of each stream. Zero returns "
                "everything the job produced, up to the configured output cap."
            ),
            ge=0,
        ),
    ] = 0,
) -> JobResult:
    """Read everything a finished background job produced.

    The notification carries only a tail of the output, because a chat message
    is a glance rather than a transcript. This is where the rest of it is: the
    full `stdout`, `stderr`, the repr of a trailing expression in `result`, and
    an `error` object with the exception type, message and traceback when the
    job failed.

    `max_chars` is worth setting for a job known to be chatty. A long run is
    exactly the kind that prints a great deal, and this is the one call that
    would put all of it in front of the model at once.

    Args:
        job_id: The job to read.
        max_chars: Keep only the last N characters of each stream, 0 for all of
            it. Truncation by the server's output cap is separate and reported
            in `truncated`.

    Returns:
        JobResult: The same fields a blocking call returns, plus the `job_id`,
        the final `state`, signed download links for the `artifacts` that exist,
        the declared ones that do not in `missing_artifacts`, and a `note` with
        any caveat -- for a cancelled job, that a killed process gets no chance
        to finish writing what it had started.

    Raises:
        ToolError: if the job is unknown, or is still running. A result that
            does not exist yet is not reported as a failure, because `ok` false
            would be read as "it failed" and said out loud. The reason travels
            as a ToolError so that it actually arrives -- see pyexec_job_status.

    Examples:
        - The notification says a job failed -> pyexec_job_result(job_id) to
          read the traceback.
        - A job printed megabytes -> pyexec_job_result(job_id, max_chars=4000).
        - Don't use when: the job is still running -- pyexec_job_status is the
          one to poll.
    """
    current = settings()
    job = _require_job(job_id)

    if job.state == "running":
        raise ToolError(
            f"Job {job_id} is still running, so there is no result yet. Poll "
            "pyexec_job_status, or wait for the notification. An empty result "
            "here would read as a failure."
        )

    result = (
        _to_result(job.result)
        if job.result is not None
        else ExecutionResult(ok=False, stdout="", stderr="", duration_ms=0.0)
    )
    if max_chars:
        result = result.model_copy(
            update={
                "stdout": _tail(result.stdout, max_chars),
                "stderr": _tail(result.stderr, max_chars),
                "raw_output": _tail(result.raw_output, max_chars),
            }
        )

    links, missing = _artifact_state(job, current)
    return JobResult(
        **result.model_dump(),
        job_id=job.id,
        state=job.state,
        artifacts=links,
        missing_artifacts=missing,
        note=_job_note(job),
    )


@mcp.tool(
    name="pyexec_job_cancel",
    title="Stop a background job",
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def pyexec_job_cancel(
    job_id: Annotated[str, Field(description="The handle a submit returned.")],
) -> JobStatus:
    """Kill a running background job and report where it ended up.

    Use this for a job that turned out to be wrong -- a bad parameter, a loop
    that will never terminate, work no longer wanted -- and for freeing a job
    slot when the concurrency limit is reached.

    Cancelling is idempotent: a job that has already finished, or was already
    cancelled, is returned unchanged rather than treated as an error, so a
    retried cancel is harmless.

    The kill is immediate and the process gets no chance to tidy up. Anything it
    had already written stays in the workspace and may be half-finished, which
    the returned `note` says. Processes the job started itself are not killed
    with it.

    Args:
        job_id: The job to stop.

    Returns:
        JobStatus: The job as it stands after the cancel. `state` is
        `cancelled` when this call is what stopped it, and otherwise whatever it
        had already reached.

    Raises:
        ToolError: if `job_id` names no job this server knows. The reason
            travels as a ToolError so that it actually arrives -- see
            pyexec_job_status.

    Examples:
        - "Stop that sweep, the parameters were wrong" ->
          pyexec_job_cancel("job-1a2b3c4d").
        - "No job slots left" -> pyexec_job_status to see what is running, then
          cancel the one that no longer matters.
        - Don't use when: the job is already done -- that is a status question,
          and this returns the same answer without changing anything.
    """
    current = settings()
    job = _require_job(job_id)
    return _job_status(job_manager().cancel(job_id) or job, current)


# --------------------------------------------------------------------------
# Resources
# --------------------------------------------------------------------------

@mcp.resource(
    "pyexec://config",
    name="Effective configuration",
    description=(
        "The settings this server resolved from its environment: workspace, "
        "interpreter, timeouts, output cap, import allowlist and background "
        "job limits."
    ),
    mime_type="application/json",
)
def config_resource() -> str:
    """Expose the live configuration so a client can see the guardrails in force."""
    import json

    current = settings()
    return json.dumps(
        {
            "workspace": str(current.workspace),
            "python_executable": current.python_executable,
            "default_timeout": current.default_timeout,
            "max_timeout": current.max_timeout,
            "max_output_chars": current.max_output_chars,
            "allowed_modules": (
                sorted(current.allowed_modules)
                if current.allowed_modules is not None
                else None
            ),
            "inherit_env": current.inherit_env,
            "base_url": current.base_url,
            "download_ttl": current.download_ttl,
            "telegram": {
                "enabled": telegram.notifier(current).enabled,
                "chat_id": current.telegram_chat_id,
                "min_duration": current.telegram_min_duration,
            },
            # Read from the global rather than through job_manager(): that
            # accessor builds one, and a resource whose job is to describe the
            # configuration should not start machinery to do it. A limit is
            # still worth reporting on a server that has submitted nothing.
            "jobs": {
                "running": len(_JOBS.running()) if _JOBS is not None else 0,
                "limit": current.max_background_jobs,
                "history": current.job_history,
                "background_timeout": current.background_timeout,
                "background_max_timeout": current.background_max_timeout,
            },
            "session_alive": session().alive,
        },
        indent=2,
    )


# --------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------

def _unauthenticated_binding_message(host: str, port: int) -> str:
    return (
        f"Refusing to bind {host}:{port} without authentication.\n"
        "\n"
        "This server executes arbitrary Python. On a non-loopback address,\n"
        "anyone who can reach the port can run code on this machine as you.\n"
        "\n"
        "Either set a token:\n"
        '  python -c "import secrets; print(secrets.token_urlsafe(32))"\n'
        "  export PYEXEC_AUTH_TOKEN=<that value>\n"
        "or put PYEXEC_AUTH_TOKEN=<that value> in a .env file next to where\n"
        "you start the server (see also PYEXEC_ENV_FILE),\n"
        "\n"
        "or bind the loopback address instead:\n"
        "  pyexec-mcp --transport streamable-http --host 127.0.0.1"
    )


def _configure_logging() -> None:
    """Send logs to stderr.

    Not cosmetic: on the stdio transport, stdout is the protocol channel, so a
    stray log line there would corrupt the session.
    """
    import logging

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _resolve_settings_or_exit() -> Settings:
    """Read the environment, reporting a bad value without a stack trace.

    A mistyped token is a configuration mistake, not a bug, and the message
    already says what to do -- burying it under twenty frames of traceback
    only makes the reader hunt for the one line that matters.
    """
    try:
        return settings()
    except ValueError as exc:
        raise SystemExit(f"pyexec-mcp: {exc}") from None


def _report_notifications(current: Settings) -> None:
    """Say once, at startup, whether long runs will be announced.

    The half-configured bot is the case worth catching: a token with no chat id
    looks like a working setup, silently sends nothing, and the first symptom
    would be a long run nobody was told about.
    """
    log = logging.getLogger("pyexec_mcp.telegram")

    if current.telegram_bot_token and not current.telegram_chat_id:
        log.warning(
            "PYEXEC_TELEGRAM_BOT_TOKEN is set but PYEXEC_TELEGRAM_CHAT_ID is "
            "not, so no notifications will be sent: a bot cannot pick a chat on "
            "its own. Send the bot a message, then read the chat id from "
            "https://api.telegram.org/bot<token>/getUpdates."
        )
    elif telegram.notifier(current).enabled:
        log.info(
            "Telegram notifications on, chat %s: runs of %gs or longer are "
            "announced, and pyexec_notify is available for anything shorter.",
            current.telegram_chat_id,
            current.telegram_min_duration,
        )


def run_http(host: str = "127.0.0.1", port: int = 8000, **kwargs: Any) -> None:
    """Serve the streamable-http transport.

    Authentication is enforced whenever ``PYEXEC_AUTH_TOKEN`` is set, and
    binding a non-loopback address without one is refused outright rather than
    warned about -- an accidentally exposed code-execution endpoint is not
    something to discover later from a log line.

    Raises:
        SystemExit: if ``host`` is not loopback and no token is configured.
    """
    import logging

    import uvicorn

    _configure_logging()
    log = logging.getLogger("pyexec_mcp.transport")
    current = settings()

    if current.auth_token is None and not is_loopback(host):
        raise SystemExit(_unauthenticated_binding_message(host, port))

    app = mcp.streamable_http_app(host=host, **kwargs)

    if current.auth_token is not None:
        app.add_middleware(BearerTokenMiddleware, token=current.auth_token)
        log.info("Bearer-token authentication enabled for %s:%s", host, port)
    else:
        log.warning(
            "No PYEXEC_AUTH_TOKEN set; any local process that can reach %s:%s "
            "can execute code. Allowed only because that address is loopback.",
            host,
            port,
        )

    # Wrapped outermost, so the download route sits outside the bearer gate and
    # answers for itself with signatures in the URL. Without a token there is
    # nothing to sign with and the route asks for nothing -- safe precisely
    # because the interlock above confined such a server to loopback.
    app = DownloadRouter(app, current)
    log.info(
        "File downloads served at %s%s, links valid for %gs",
        current.base_url,
        DOWNLOAD_PREFIX,
        current.download_ttl,
    )

    uvicorn.run(app, host=host, port=port)


def _load_env_file() -> None:
    """Merge a ``.env`` into the environment before settings are read.

    Runs at the top of :func:`main` because ``Settings`` is built from the
    environment and cached on first use -- load any later and the server would
    already have decided it has no token.
    """
    import logging

    from . import dotenv

    path = os.environ.get("PYEXEC_ENV_FILE", dotenv.DEFAULT_ENV_FILENAME)
    applied = dotenv.load(path)
    if applied:
        # Names only. A token must never reach the log.
        logging.getLogger("pyexec_mcp.config").info(
            "Loaded %s from %s", ", ".join(sorted(applied)), path
        )


def main(
    transport: Literal["stdio", "streamable-http"] = "stdio",
    host: str = "127.0.0.1",
    port: int = 8000,
    **kwargs: Any,
) -> None:
    """Run the server on the requested transport."""
    _configure_logging()
    _load_env_file()
    # Resolved up front so a bad setting is reported once, cleanly, instead of
    # surfacing from whichever code path happens to touch it first.
    _report_notifications(_resolve_settings_or_exit())
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        run_http(host=host, port=port, **kwargs)


__all__ = ["mcp", "main", "run_http", "settings", "session", "use_settings"]
