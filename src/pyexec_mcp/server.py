"""MCP tools for running Python.

The server exposes one persistent session (fast iteration, state carries over)
alongside isolated one-shot execution (clean room, nothing leaks), plus the
file and dependency operations needed to do real work in the workspace.

Tools and resources are declared here; execution lives in
:mod:`pyexec_mcp.session` and the guardrails in :mod:`pyexec_mcp.sandbox`.
"""

from __future__ import annotations

import atexit
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import packages
from .auth import BearerTokenMiddleware
from .config import Settings, is_loopback
from .downloads import DOWNLOAD_PREFIX, DownloadRouter, build_url, workspace_relative
from .sandbox import GuardrailError, check_code, resolve_within
from .session import ExecutionSession, run_isolated

SERVER_NAME = "pyexec_mcp"

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


@atexit.register
def _close_session() -> None:
    if _SESSION is not None:
        _SESSION.close()


def use_settings(new_settings: Settings) -> None:
    """Swap in a different configuration, discarding the current session.

    Exists so tests (and embedders) can point the server at a temporary
    workspace without going through the environment.
    """
    global _SETTINGS, _SESSION
    with _SESSION_LOCK:
        if _SESSION is not None:
            _SESSION.close()
        _SESSION = None
        _SETTINGS = new_settings


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
    return _to_result(session().request("exec", timeout=deadline, code=code))


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
    return _to_result(run_isolated(settings(), code, deadline))


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

    call = run_isolated(
        current, _script_invocation(script, args or []), current.clamp_timeout(timeout)
    )
    return _to_result(call)


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
        - Don't use when: the content is small text you want to read anyway --
          pyexec_read_file puts it where you can reason about it.

    Raises:
        ValueError: if the path is outside the workspace, is not a file, or the
            server has no address to build a link from.
    """
    current = settings()
    if not current.base_url:
        raise ValueError(
            "This server has no address to build a download link from. Set "
            "PYEXEC_BASE_URL to the address clients use to reach it, e.g. "
            "http://192.168.1.10:8000, and restart."
        )

    try:
        target = resolve_within(path, current.workspace)
    except GuardrailError as exc:
        raise ValueError(str(exc)) from exc

    if not target.is_file():
        raise ValueError(
            f"No such file: {target}. Use pyexec_list_files to see what is in "
            "the workspace."
        )

    relative = workspace_relative(target, current.workspace)
    url, expires = build_url(
        current.base_url,
        relative,
        current.auth_token,
        current.clamp_download_ttl(ttl_seconds),
    )
    expires_in = max(0, int(expires - time.time()))

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
        expires_at=datetime.fromtimestamp(expires, timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        expires_in_seconds=expires_in,
        note=note,
    )


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
# Resources
# --------------------------------------------------------------------------

@mcp.resource(
    "pyexec://config",
    name="Effective configuration",
    description=(
        "The settings this server resolved from its environment: workspace, "
        "interpreter, timeouts, output cap and import allowlist."
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
    _resolve_settings_or_exit()
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        run_http(host=host, port=port, **kwargs)


__all__ = ["mcp", "main", "run_http", "settings", "session", "use_settings"]
