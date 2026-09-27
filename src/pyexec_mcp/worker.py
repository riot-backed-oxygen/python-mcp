#!/usr/bin/env python3
"""Isolated Python worker, driven over stdin/stdout.

Deliberately standalone: it imports nothing from ``pyexec_mcp``, so the parent
can launch it by absolute path and it keeps working no matter how (or whether)
the package is installed.

Protocol
--------
The parent writes one JSON request per line to stdin::

    {"id": 1, "op": "exec", "code": "1 + 1", "timeout": 10.0}

The worker answers with one JSON frame per line on stdout::

    @@PYEXEC@@<nonce>@@{"id": 1, "ok": true, "stdout": "2\\n", ...}

``<nonce>`` is a per-session random token from the parent, so code running in
the worker cannot print a line the parent would accept as a response.

Output handling: ``print`` is captured by swapping ``sys.stdout`` for the
duration of a call and shipped inside the frame. Bytes written straight to file
descriptor 1 -- by a C extension, a subprocess, ``os.write`` -- bypass that swap
and reach the parent as raw output just ahead of the frame, which the parent
reports separately.
"""

from __future__ import annotations

import argparse
import ast
import ctypes
import io
import json
import platform
import sys
import threading
import time
import traceback
from typing import Any

MISSING = object()


class Timeout(BaseException):
    """Raised inside user code when the soft deadline expires.

    Inherits from ``BaseException`` on purpose: a stray ``except Exception`` in
    the executed snippet must not be able to swallow our timeout.
    """


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

def _new_namespace() -> dict[str, Any]:
    """A fresh global namespace, matching what a script would see."""
    import builtins

    return {
        "__name__": "__main__",
        "__builtins__": builtins,
        "__doc__": None,
        "__package__": None,
    }


def _execute(code: str, namespace: dict[str, Any]) -> Any:
    """Run *code* in *namespace*, returning the value of a trailing expression.

    Mirrors the REPL: when the last statement is a bare expression it is
    evaluated and returned so the caller can render its ``repr``.
    """
    tree = ast.parse(code, "<pyexec>", "exec")

    if tree.body and isinstance(tree.body[-1], ast.Expr):
        tail = ast.Expression(body=tree.body[-1].value)
        ast.fix_missing_locations(tail)
        head = ast.Module(body=tree.body[:-1], type_ignores=[])
        ast.fix_missing_locations(head)
        exec(compile(head, "<pyexec>", "exec"), namespace)
        return eval(compile(tail, "<pyexec>", "eval"), namespace)

    exec(compile(tree, "<pyexec>", "exec"), namespace)
    return MISSING


def _arm_soft_timeout(seconds: float, done: threading.Event) -> None:
    """Ask the main thread to raise :class:`Timeout` once *seconds* elapse.

    CPython cannot preempt a running frame, so this is best effort: the
    exception surfaces at the next bytecode boundary. A snippet parked inside a
    blocking C call (a socket read, say) never reaches one -- that case is
    covered by the parent's hard kill.
    """
    ident = threading.get_ident()

    def _watch() -> None:
        if done.wait(seconds):
            return
        # Re-check to narrow the window in which we inject after the call has
        # already finished; the request loop also tolerates a stray Timeout.
        if done.is_set():
            return
        affected = ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_ulong(ident), ctypes.py_object(Timeout)
        )
        if affected > 1:  # pragma: no cover - defensive
            ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(ident), None)

    threading.Thread(target=_watch, name="pyexec-deadline", daemon=True).start()


def _user_traceback(exc: BaseException) -> str:
    """Format *exc*'s traceback with this file's own frames removed.

    The parent reads these tracebacks, so the frames belonging to the worker
    harness are noise: what matters is the line in the snippet that failed.
    """
    stack = traceback.extract_tb(exc.__traceback__)
    user_frames = [frame for frame in stack if frame.filename != __file__]
    if not user_frames:
        # Nothing user-side -- a harness bug. Show everything we have.
        user_frames = stack

    parts = []
    if user_frames:
        parts.append("Traceback (most recent call last):\n")
        parts.extend(traceback.format_list(user_frames))
    parts.extend(traceback.format_exception_only(type(exc), exc))
    return "".join(parts)


def _describe_error(exc: BaseException) -> dict[str, str]:
    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": _user_traceback(exc),
    }


# --------------------------------------------------------------------------
# Request handling
# --------------------------------------------------------------------------

def _handle_exec(
    request: dict[str, Any], namespace: dict[str, Any]
) -> dict[str, Any]:
    code = request.get("code") or ""
    timeout = request.get("timeout") or None

    captured_out, captured_err = io.StringIO(), io.StringIO()
    real_streams = sys.stdout, sys.stderr
    finished = threading.Event()

    started = time.perf_counter()
    result_repr: str | None = None
    error: dict[str, str] | None = None
    ok = True

    try:
        if timeout:
            _arm_soft_timeout(float(timeout), finished)
        sys.stdout, sys.stderr = captured_out, captured_err
        try:
            value = _execute(code, namespace)
            if value is not MISSING:
                result_repr = repr(value)
        finally:
            sys.stdout, sys.stderr = real_streams
            finished.set()

    except Timeout:
        ok = False
        error = {
            "type": "TimeoutError",
            "message": (
                f"Execution exceeded the {float(timeout):g}s deadline and was "
                "interrupted. The session and its variables are intact."
            ),
            "traceback": "",
        }
    except SystemExit as exc:
        # ``sys.exit()`` in a snippet should end the snippet, not the worker.
        ok = False
        error = {
            "type": "SystemExit",
            "message": f"Code called sys.exit({exc.code!r}). The session is still alive.",
            "traceback": "",
        }
    except BaseException as exc:  # noqa: BLE001 - report anything the snippet raises
        ok = False
        error = _describe_error(exc)

    return {
        "id": request.get("id"),
        "ok": ok,
        "stdout": captured_out.getvalue(),
        "stderr": captured_err.getvalue(),
        "result": result_repr,
        "error": error,
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def _handle_info(request: dict[str, Any], namespace: dict[str, Any]) -> dict[str, Any]:
    """Report what the session currently holds."""
    import os

    user_names = sorted(
        name
        for name in namespace
        if not name.startswith("__") and not name.startswith("_pyexec")
    )
    return {
        "id": request.get("id"),
        "ok": True,
        "pid": os.getpid(),
        "python_version": platform.python_version(),
        "executable": sys.executable,
        "cwd": os.getcwd(),
        "names": user_names,
    }


def _dispatch(
    request: dict[str, Any], namespace: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Route one request. Returns ``(payload, should_shutdown)``."""
    op = request.get("op")

    if op == "exec":
        return _handle_exec(request, namespace), False
    if op == "info":
        return _handle_info(request, namespace), False
    if op == "ping":
        return {"id": request.get("id"), "ok": True, "pong": True}, False
    if op == "reset":
        return {"id": request.get("id"), "ok": True, "reset": True}, False
    if op == "shutdown":
        return {"id": request.get("id"), "ok": True, "bye": True}, True

    return {
        "id": request.get("id"),
        "ok": False,
        "error": {
            "type": "ProtocolError",
            "message": f"Unknown op: {op!r}",
            "traceback": "",
        },
    }, False


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def _emit(stream, nonce: str, payload: dict[str, Any]) -> None:
    """Write one response frame.

    No guard newline is needed ahead of the frame: the parent locates the magic
    prefix anywhere in the line, so program output that was written without a
    trailing newline is preserved rather than corrupting the frame.
    """
    stream.write(f"@@PYEXEC@@{nonce}@@{json.dumps(payload)}\n")
    stream.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pyexec-worker")
    parser.add_argument("--nonce", required=True, help="session token for framing")
    args = parser.parse_args(argv)

    namespace = _new_namespace()
    stream = sys.stdout
    stdin = sys.stdin

    while True:
        line = stdin.readline()
        if not line:  # parent closed the pipe
            break
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        try:
            payload, should_shutdown = _dispatch(request, namespace)
        except Timeout:
            # A deadline that fired between requests. Harmless -- drop it.
            continue
        except BaseException as exc:  # noqa: BLE001 - never let the loop die
            payload = {
                "id": request.get("id"),
                "ok": False,
                "error": _describe_error(exc),
            }
            should_shutdown = False

        if request.get("op") == "reset":
            namespace = _new_namespace()

        _emit(stream, args.nonce, payload)

        if should_shutdown:
            break

    return 0


if __name__ == "__main__":
    sys.exit(main())
