"""Runtime configuration, resolved once from the environment.

Everything the server needs to know about its environment is read here so a
client can reconfigure it from the ``env`` block of its MCP config without any
code change.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

#: Timeout applied when a tool call does not pass one explicitly.
DEFAULT_TIMEOUT = 30.0

#: Ceiling for caller-supplied timeouts, so a typo cannot pin a worker forever.
DEFAULT_MAX_TIMEOUT = 300.0

#: Per-stream output kept before truncation kicks in.
DEFAULT_MAX_OUTPUT_CHARS = 40_000

#: Extra wall-clock slack the parent grants past the worker's own deadline
#: before it gives up waiting and kills the process outright.
TIMEOUT_GRACE = 5.0

#: Shortest bearer token accepted for the streamable-http transport. A guessable
#: token on a code-execution server is worse than no token at all, because it
#: looks like protection.
MIN_TOKEN_LENGTH = 16

#: Hosts that are reachable only from this machine.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _env_str(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    raw = _env_str(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def is_loopback(host: str) -> bool:
    """Whether *host* is reachable only from this machine."""
    return host.strip().lower() in LOOPBACK_HOSTS


def _resolve_auth_token() -> str | None:
    """Read the HTTP bearer token, preferring the direct variable over a file.

    A file is offered because an environment variable shows up in ``ps`` output
    and in crash dumps, which is a poor place for a secret that guards code
    execution.

    Raises:
        ValueError: if a token is configured but is too short to be worth
            anything.
    """
    token = _env_str("PYEXEC_AUTH_TOKEN")

    if token is None:
        path = _env_str("PYEXEC_AUTH_TOKEN_FILE")
        if path:
            try:
                token = Path(path).expanduser().read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ValueError(
                    f"PYEXEC_AUTH_TOKEN_FILE points at {path!r}, which could not "
                    f"be read: {exc}"
                ) from exc

    if token is None:
        return None

    if len(token) < MIN_TOKEN_LENGTH:
        raise ValueError(
            f"The configured auth token is only {len(token)} characters; at "
            f"least {MIN_TOKEN_LENGTH} are required. A short token on a server "
            "that executes arbitrary code is worse than none, because it looks "
            "like protection. Generate one with:\n"
            '  python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    return token


@dataclass(frozen=True)
class Settings:
    """Everything the server and its workers need to run."""

    #: Root that file tools are confined to, and the working directory of every
    #: worker process.
    workspace: Path

    #: Interpreter used for workers *and* for package installs, so anything
    #: ``pyexec_install_package`` installs is importable by the next call.
    python_executable: str

    default_timeout: float
    max_timeout: float
    max_output_chars: int

    #: Top-level modules the code under test may import. ``None`` means no
    #: restriction at all.
    allowed_modules: frozenset[str] | None

    #: Whether workers inherit the server's environment (and therefore its
    #: secrets) or run against a scrubbed one.
    inherit_env: bool

    #: Shared secret required from HTTP clients as ``Authorization: Bearer``.
    #: ``None`` disables authentication, which is only permitted on loopback.
    auth_token: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        workspace = Path(
            _env_str("PYEXEC_WORKSPACE", str(Path.cwd()))
        ).expanduser().resolve()

        allowed = _env_str("PYEXEC_ALLOWED_MODULES")
        allowed_modules = (
            frozenset(m.strip() for m in allowed.split(",") if m.strip())
            if allowed
            else None
        )

        return cls(
            workspace=workspace,
            python_executable=_env_str("PYEXEC_PYTHON", sys.executable),
            default_timeout=_env_float("PYEXEC_TIMEOUT", DEFAULT_TIMEOUT),
            max_timeout=_env_float("PYEXEC_MAX_TIMEOUT", DEFAULT_MAX_TIMEOUT),
            max_output_chars=int(
                _env_float("PYEXEC_MAX_OUTPUT_CHARS", DEFAULT_MAX_OUTPUT_CHARS)
            ),
            allowed_modules=allowed_modules,
            inherit_env=_env_bool("PYEXEC_INHERIT_ENV", True),
            auth_token=_resolve_auth_token(),
        )

    def clamp_timeout(self, timeout: float | None) -> float:
        """Fold a caller-supplied timeout into the configured range."""
        if timeout is None or timeout <= 0:
            return self.default_timeout
        return min(float(timeout), self.max_timeout)

    def child_env(self) -> dict[str, str]:
        """Environment for a worker or installer subprocess.

        Forces UTF-8 so a Chinese or otherwise non-ASCII locale cannot mangle
        the framing on stdout.
        """
        env = dict(os.environ) if self.inherit_env else _MINIMAL_ENV.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("PYTHONSTARTUP", None)
        return env


#: Used when ``PYEXEC_INHERIT_ENV`` is false: enough to start an interpreter,
#: nothing that carries credentials.
_MINIMAL_ENV: dict[str, str] = {
    key: os.environ[key]
    for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE", "PATHEXT")
    if key in os.environ
}
