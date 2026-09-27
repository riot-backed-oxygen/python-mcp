"""Tests for the Python execution MCP server.

The session tests go through the same tool functions an MCP client would call,
so they exercise the real guardrails rather than a parallel code path.
"""

from __future__ import annotations

import json
import sys
import time

import pytest

from pyexec_mcp import server
from pyexec_mcp.sandbox import GuardrailError, check_code, check_package_specs


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------

def test_state_persists_across_calls(workspace):
    first = server.pyexec_run("counter = 40")
    assert first.ok

    second = server.pyexec_run("counter + 2")
    assert second.ok
    assert second.result == "42"


def test_isolated_run_cannot_see_session_state(workspace):
    server.pyexec_run("secret = 'session-value'")

    leaked = server.pyexec_run_isolated("secret")
    assert leaked.ok is False
    assert leaked.error.type == "NameError"


def test_isolated_run_does_not_pollute_session(workspace):
    server.pyexec_run_isolated("isolated_only = 1")

    result = server.pyexec_run("isolated_only")
    assert result.ok is False
    assert result.error.type == "NameError"


def test_trailing_expression_is_reprd(workspace):
    assert server.pyexec_run("1 + 1").result == "2"
    assert server.pyexec_run("'abc'.upper()").result == "'ABC'"
    assert server.pyexec_run("[i * 2 for i in range(3)]").result == "[0, 2, 4]"


def test_statement_only_code_has_no_result(workspace):
    result = server.pyexec_run("import math")
    assert result.ok
    assert result.result is None


def test_stdout_and_stderr_are_captured(workspace):
    result = server.pyexec_run(
        "import sys\nprint('to stdout')\nprint('to stderr', file=sys.stderr)"
    )
    assert result.stdout == "to stdout\n"
    assert result.stderr == "to stderr\n"


def test_exception_reports_type_message_and_traceback(workspace):
    result = server.pyexec_run("def boom():\n    raise ValueError('nope')\n\nboom()")

    assert result.ok is False
    assert result.error.type == "ValueError"
    assert result.error.message == "nope"
    # The traceback should point at the snippet and name the failing frame...
    assert 'File "<pyexec>", line 2, in boom' in result.error.traceback
    # ...without dragging the harness's own frames along with it.
    assert "worker.py" not in result.error.traceback


def test_syntax_error_is_reported_before_a_worker_starts(workspace):
    result = server.pyexec_run("def broken(:\n    pass")

    assert result.ok is False
    assert result.error.type == "GuardrailError"
    assert "SyntaxError" in result.error.message


def test_sys_exit_does_not_kill_the_session(workspace):
    result = server.pyexec_run("import sys\nsys.exit(3)")

    assert result.ok is False
    assert result.error.type == "SystemExit"
    # The session must still answer.
    assert server.pyexec_run("1 + 1").result == "2"


def test_exception_in_one_call_does_not_poison_the_next(workspace):
    server.pyexec_run("kept = 'still here'")
    server.pyexec_run("raise RuntimeError('boom')")

    assert server.pyexec_run("kept").result == "'still here'"


def test_defining_a_function_persists(workspace):
    server.pyexec_run("def double(n):\n    return n * 2")
    assert server.pyexec_run("double(21)").result == "42"


# --------------------------------------------------------------------------
# Timeouts
# --------------------------------------------------------------------------

def test_pure_python_loop_is_interrupted(workspace):
    started = time.monotonic()
    result = server.pyexec_run("while True:\n    pass", timeout=1)
    elapsed = time.monotonic() - started

    assert result.ok is False
    assert result.error.type == "TimeoutError"
    assert elapsed < 5, f"soft interrupt should be prompt, took {elapsed:.1f}s"


def test_interruptible_timeout_preserves_the_session(workspace):
    server.pyexec_run("before_timeout = 'kept'")
    result = server.pyexec_run("while True:\n    pass", timeout=1)

    # A pure-Python loop is interrupted in place, so nothing is lost.
    assert result.session_restarted is False
    assert server.pyexec_run("before_timeout").result == "'kept'"


def test_hard_kill_timeout_restarts_the_session(workspace):
    server.pyexec_run("before_timeout = 'lost'")

    started = time.monotonic()
    result = server.pyexec_run("import time\ntime.sleep(30)", timeout=1)
    elapsed = time.monotonic() - started

    # A blocking C call never reaches a bytecode boundary, so the in-worker soft
    # interrupt cannot land and the parent's hard kill is what ends this.
    assert result.ok is False
    assert result.error.type == "TimeoutError"
    assert elapsed < 15, f"hard kill should bound this, took {elapsed:.1f}s"
    assert result.session_restarted is True
    assert server.pyexec_run("before_timeout").error.type == "NameError"


def test_session_is_usable_after_a_timeout(workspace):
    server.pyexec_run("while True:\n    pass", timeout=1)

    assert server.pyexec_run("6 * 7").result == "42"


def test_timeout_argument_is_clamped_to_the_configured_maximum(
    workspace, make_settings
):
    server.use_settings(make_settings(workspace, default_timeout=5.0, max_timeout=2.0))
    result = server.pyexec_run("while True:\n    pass", timeout=9999)

    assert result.error.type == "TimeoutError"
    assert "2s" in result.error.message


# --------------------------------------------------------------------------
# Session management
# --------------------------------------------------------------------------

def test_reset_clears_state(workspace):
    server.pyexec_run("doomed = 'value'")
    info = server.pyexec_reset_session()

    assert info.alive is True
    assert server.pyexec_run("doomed").error.type == "NameError"


def test_session_info_lists_defined_names(workspace):
    server.pyexec_run("alpha = 1\nbeta = 2")

    info = server.pyexec_session_info()
    assert info.alive is True
    assert {"alpha", "beta"} <= set(info.names)
    assert info.pid is not None


def test_session_info_is_safe_before_any_call(workspace):
    info = server.pyexec_session_info()
    assert info.alive is False
    assert info.names == []


# --------------------------------------------------------------------------
# Guardrails
# --------------------------------------------------------------------------

def test_import_allowlist_blocks_other_modules(workspace, make_settings):
    server.use_settings(make_settings(workspace, allowed_modules=frozenset({"math"})))
    result = server.pyexec_run("import os")

    assert result.ok is False
    assert result.error.type == "GuardrailError"
    assert "os" in result.error.message


def test_import_allowlist_permits_listed_modules(workspace, make_settings):
    server.use_settings(make_settings(workspace, allowed_modules=frozenset({"math"})))
    assert server.pyexec_run("import math\nmath.floor(1.5)").result == "1"


def test_allowlist_catches_dunder_import(workspace, make_settings):
    server.use_settings(make_settings(workspace, allowed_modules=frozenset({"math"})))
    result = server.pyexec_run("__import__('subprocess')")

    assert result.ok is False
    assert result.error.type == "GuardrailError"


def test_no_allowlist_means_no_restriction(workspace):
    assert server.pyexec_run("import json\njson.dumps([1])").ok is True


def test_check_code_reports_syntax_errors():
    with pytest.raises(GuardrailError, match="SyntaxError"):
        check_code("def broken(:", None)


def test_check_package_specs_rejects_flags():
    with pytest.raises(GuardrailError):
        check_package_specs(["--target=/tmp/evil"])


def test_check_package_specs_accepts_normal_specs():
    check_package_specs(["pandas", "requests==2.31.0", "uvicorn[standard]>=0.30"])


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------

def test_write_then_read_round_trip(workspace):
    receipt = server.pyexec_write_file("notes/hello.txt", "hi there")

    assert receipt.created is True
    assert receipt.bytes_written == len("hi there")

    content = server.pyexec_read_file("notes/hello.txt")
    assert content.content == "hi there"
    assert content.truncated is False


def test_write_reports_when_it_replaces_a_file(workspace):
    server.pyexec_write_file("f.txt", "first")
    assert server.pyexec_write_file("f.txt", "second").created is False


def test_write_can_refuse_to_overwrite(workspace):
    server.pyexec_write_file("f.txt", "original")

    with pytest.raises(ValueError, match="already exists"):
        server.pyexec_write_file("f.txt", "replacement", overwrite=False)
    assert server.pyexec_read_file("f.txt").content == "original"


def test_read_truncates_large_files(workspace):
    server.pyexec_write_file("big.txt", "x" * 5000)

    content = server.pyexec_read_file("big.txt", max_bytes=100)
    assert content.truncated is True
    assert len(content.content) == 100
    assert content.size_bytes == 5000


def test_read_missing_file_is_an_actionable_error(workspace):
    with pytest.raises(ValueError, match="No such file"):
        server.pyexec_read_file("nope.txt")


def test_paths_cannot_escape_the_workspace(workspace):
    with pytest.raises(ValueError, match="escapes the workspace"):
        server.pyexec_write_file("../outside.txt", "nope")

    with pytest.raises(ValueError, match="escapes the workspace"):
        server.pyexec_read_file("../../etc/passwd")


def test_list_files_finds_and_filters_entries(workspace):
    server.pyexec_write_file("a.py", "x")
    server.pyexec_write_file("b.txt", "y")
    server.pyexec_write_file("nested/c.py", "z")

    flat = server.pyexec_list_files()
    assert "a.py" in {e.path for e in flat.entries}
    assert flat.has_more is False

    recursive = server.pyexec_list_files(pattern="**/*.py", recursive=True)
    paths = {e.path for e in recursive.entries}
    assert "a.py" in paths
    assert "nested/c.py" in paths
    assert "b.txt" not in paths


def test_list_files_reports_truncation(workspace):
    for index in range(10):
        server.pyexec_write_file(f"f{index}.txt", "x")

    listing = server.pyexec_list_files(max_results=3)
    assert listing.count == 3
    assert listing.has_more is True


# --------------------------------------------------------------------------
# Scripts
# --------------------------------------------------------------------------

def test_run_script_executes_a_file_as_main(workspace):
    server.pyexec_write_file(
        "script.py",
        "import sys\n"
        "print('name is', __name__)\n"
        "print('args are', sys.argv[1:])\n",
    )

    result = server.pyexec_run_script("script.py", args=["--flag", "value"])

    assert result.ok is True
    assert "name is __main__" in result.stdout
    assert "args are ['--flag', 'value']" in result.stdout


def test_run_script_reports_a_nonzero_exit(workspace):
    server.pyexec_write_file("bad.py", "raise SystemExit(2)")

    result = server.pyexec_run_script("bad.py")
    assert result.ok is False


def test_run_script_rejects_a_missing_file(workspace):
    result = server.pyexec_run_script("ghost.py")

    assert result.ok is False
    assert result.error.type == "FileNotFoundError"


def test_run_script_cannot_escape_the_workspace(workspace):
    result = server.pyexec_run_script("../outside.py")

    assert result.ok is False
    assert result.error.type == "GuardrailError"


# --------------------------------------------------------------------------
# Packages
# --------------------------------------------------------------------------

def test_list_packages_reports_this_projects_interpreter(workspace):
    listing = server.pyexec_list_packages()

    assert listing.count > 0
    assert listing.executable == sys.executable
    # The server's own distribution being visible proves we are listing the
    # interpreter the workers actually use.
    assert any(p.startswith("pyexec-mcp") for p in listing.packages)


def test_list_packages_filters_by_substring(workspace):
    listing = server.pyexec_list_packages(match="pytest")

    assert listing.count >= 1
    assert all("pytest" in p.lower() for p in listing.packages)


def test_install_rejects_a_flag_shaped_spec(workspace):
    with pytest.raises(GuardrailError):
        server.pyexec_install_package(["--target=/tmp/x"])


# --------------------------------------------------------------------------
# Resource
# --------------------------------------------------------------------------

def test_config_resource_reports_effective_settings(workspace):
    payload = json.loads(server.config_resource())

    assert payload["workspace"] == str(workspace)
    assert payload["allowed_modules"] is None
    assert payload["default_timeout"] == 15.0
