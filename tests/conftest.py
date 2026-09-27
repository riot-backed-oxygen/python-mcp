"""Shared fixtures for the test suite."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from pyexec_mcp import server
from pyexec_mcp.config import Settings

#: Bearer token the live servers in this suite are started with. Long enough to
#: clear the minimum the server enforces.
LIVE_TOKEN = "correct-horse-battery-staple-32chars"


@pytest.fixture
def make_settings():
    """Factory for Settings pointed at a throwaway workspace."""

    def _make(workspace, **overrides) -> Settings:
        base = dict(
            workspace=workspace,
            python_executable=sys.executable,
            default_timeout=15.0,
            max_timeout=60.0,
            max_output_chars=20_000,
            allowed_modules=None,
            inherit_env=True,
        )
        base.update(overrides)
        return Settings(**base)

    return _make


@pytest.fixture
def workspace(tmp_path, make_settings):
    """Point the server at a throwaway workspace and tear the session down."""
    server.use_settings(make_settings(tmp_path))
    yield tmp_path
    server.use_settings(make_settings(tmp_path))  # closes the session


# --------------------------------------------------------------------------
# A real server on a real port
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class LiveServer:
    """A streamable-http server running in its own process."""

    #: The MCP endpoint clients post to.
    url: str
    #: The address the server advertises, i.e. what download links start with.
    base_url: str
    token: str
    workspace: Path


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until_listening(url: str, timeout: float = 30.0) -> None:
    """Poll until the server answers.

    ``trust_env=False`` matters here: with a proxy configured in the
    environment, httpx would route these loopback requests through it and the
    proxy's error response would look like a server that is already up.
    """
    deadline = time.monotonic() + timeout
    with httpx.Client(trust_env=False, timeout=1.0) as client:
        while time.monotonic() < deadline:
            try:
                client.get(url)
                return
            except httpx.TransportError:
                time.sleep(0.25)
    raise AssertionError(f"server at {url} never started listening")


@pytest.fixture
def spawn_server(tmp_path):
    """Factory for a real streamable-http server, on a free port.

    Keyword arguments are extra environment variables for the server process,
    which is how a test turns a feature on or off.
    """

    procs: list[subprocess.Popen] = []

    def _spawn(**overrides: str) -> LiveServer:
        port = free_port()
        base_url = f"http://127.0.0.1:{port}"
        env = dict(os.environ)
        env["PYEXEC_AUTH_TOKEN"] = LIVE_TOKEN
        env["PYEXEC_WORKSPACE"] = str(tmp_path)
        env["PYEXEC_BASE_URL"] = base_url
        env["PYEXEC_DOWNLOAD_TTL"] = "300"
        # Ignore any .env the developer has in the project root; this test
        # supplies its own configuration and should not care what else is
        # lying around.
        env["PYEXEC_ENV_FILE"] = str(tmp_path / "no-such.env")
        env.update(overrides)

        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "pyexec_mcp",
                "--transport",
                "streamable-http",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Registered before the wait, so a server that never comes up is still
        # killed during teardown.
        procs.append(proc)
        live = LiveServer(
            url=f"{base_url}/mcp",
            base_url=base_url,
            token=env["PYEXEC_AUTH_TOKEN"],
            workspace=tmp_path,
        )
        wait_until_listening(live.url)
        return live

    yield _spawn

    for proc in procs:
        proc.kill()
        proc.wait(timeout=10)


@pytest.fixture
def live_server(spawn_server) -> LiveServer:
    """A real streamable-http server with default configuration."""
    return spawn_server()
