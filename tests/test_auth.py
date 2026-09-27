"""Tests for bearer-token authentication on the streamable-http transport."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

from pyexec_mcp import server
from pyexec_mcp.auth import BearerTokenMiddleware, presented_token
from pyexec_mcp.config import Settings, is_loopback

TOKEN = "correct-horse-battery-staple-32chars"


def make_scope(headers=(), scope_type="http", method="POST", path="/mcp"):
    return {
        "type": scope_type,
        "method": method,
        "path": path,
        "headers": [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in headers
        ],
        "client": ("127.0.0.1", 54321),
    }


class Recorder:
    """Collects the ASGI messages a middleware emits."""

    def __init__(self):
        self.messages = []

    async def __call__(self, message):
        self.messages.append(message)

    @property
    def status(self):
        return self.messages[0]["status"]

    @property
    def headers(self):
        return dict(self.messages[0]["headers"])


class InnerApp:
    """Stands in for the MCP endpoint; records that it was reached."""

    def __init__(self):
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


# --------------------------------------------------------------------------
# Token extraction
# --------------------------------------------------------------------------

def test_bearer_token_is_extracted():
    scope = make_scope([("Authorization", f"Bearer {TOKEN}")])
    assert presented_token(scope) == TOKEN


def test_scheme_matching_is_case_insensitive():
    scope = make_scope([("authorization", f"bEaReR {TOKEN}")])
    assert presented_token(scope) == TOKEN


def test_other_schemes_are_ignored():
    scope = make_scope([("Authorization", f"Basic {TOKEN}")])
    assert presented_token(scope) is None


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("Authorization", "Bearer")],
        [("Authorization", "Bearer   ")],
        [("X-Api-Key", TOKEN)],
    ],
)
def test_absent_or_malformed_credentials_yield_nothing(headers):
    assert presented_token(make_scope(headers)) is None


def test_token_is_not_read_from_the_query_string():
    # A token in the URL would leak into access logs and Referer headers.
    scope = make_scope()
    scope["query_string"] = f"token={TOKEN}".encode()
    assert presented_token(scope) is None


# --------------------------------------------------------------------------
# Middleware behaviour
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_valid_token_reaches_the_endpoint():
    inner = InnerApp()
    middleware = BearerTokenMiddleware(inner, TOKEN)
    send = Recorder()

    await middleware(
        make_scope([("Authorization", f"Bearer {TOKEN}")]), None, send
    )

    assert inner.called is True
    assert send.status == 200


@pytest.mark.asyncio
async def test_missing_token_is_rejected():
    inner = InnerApp()
    middleware = BearerTokenMiddleware(inner, TOKEN)
    send = Recorder()

    await middleware(make_scope(), None, send)

    assert inner.called is False
    assert send.status == 401
    assert b"www-authenticate" in send.headers


@pytest.mark.asyncio
async def test_wrong_token_is_rejected():
    inner = InnerApp()
    middleware = BearerTokenMiddleware(inner, TOKEN)
    send = Recorder()

    await middleware(
        make_scope([("Authorization", f"Bearer {'x' * len(TOKEN)}")]), None, send
    )

    assert inner.called is False
    assert send.status == 401


@pytest.mark.asyncio
async def test_rejection_distinguishes_missing_from_invalid():
    middleware = BearerTokenMiddleware(InnerApp(), TOKEN)

    missing = Recorder()
    await middleware(make_scope(), None, missing)
    assert b"invalid_request" in missing.headers[b"www-authenticate"]

    invalid = Recorder()
    await middleware(make_scope([("Authorization", "Bearer nope")]), None, invalid)
    assert b"invalid_token" in invalid.headers[b"www-authenticate"]


@pytest.mark.asyncio
async def test_non_http_scopes_pass_through():
    # Lifespan startup must not be blocked by the gate.
    inner = InnerApp()
    middleware = BearerTokenMiddleware(inner, TOKEN)

    await middleware(make_scope(scope_type="lifespan"), None, Recorder())

    assert inner.called is True


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", " 127.0.0.1 "])
def test_loopback_hosts_are_recognised(host):
    assert is_loopback(host) is True


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com", "::"])
def test_public_hosts_are_not_loopback(host):
    assert is_loopback(host) is False


def test_short_token_is_refused(monkeypatch):
    monkeypatch.setenv("PYEXEC_AUTH_TOKEN", "tooshort")

    with pytest.raises(ValueError, match="at least 16"):
        Settings.from_env()


def test_token_from_file_is_supported(tmp_path, monkeypatch):
    secret = tmp_path / "token"
    secret.write_text(f"  {TOKEN}\n", encoding="utf-8")
    monkeypatch.delenv("PYEXEC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("PYEXEC_AUTH_TOKEN_FILE", str(secret))

    assert Settings.from_env().auth_token == TOKEN


def test_missing_token_file_is_an_actionable_error(monkeypatch):
    monkeypatch.delenv("PYEXEC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("PYEXEC_AUTH_TOKEN_FILE", "does-not-exist")

    with pytest.raises(ValueError, match="could not be read"):
        Settings.from_env()


def test_no_token_configured_is_allowed(monkeypatch):
    monkeypatch.delenv("PYEXEC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("PYEXEC_AUTH_TOKEN_FILE", raising=False)

    assert Settings.from_env().auth_token is None


# --------------------------------------------------------------------------
# Startup interlock
# --------------------------------------------------------------------------

def test_binding_a_public_host_without_a_token_is_refused(tmp_path, make_settings):
    server.use_settings(make_settings(tmp_path, auth_token=None))

    with pytest.raises(SystemExit) as excinfo:
        server.run_http(host="0.0.0.0", port=8000)

    message = str(excinfo.value)
    assert "Refusing to bind" in message
    assert "PYEXEC_AUTH_TOKEN" in message


def test_binding_loopback_without_a_token_is_allowed(
    tmp_path, make_settings, monkeypatch
):
    server.use_settings(make_settings(tmp_path, auth_token=None))

    # uvicorn is stubbed out: the point is that we get past the interlock, not
    # that a server actually starts.
    started = {}
    monkeypatch.setattr(
        "uvicorn.run", lambda app, host, port: started.update(host=host, port=port)
    )

    server.run_http(host="127.0.0.1", port=8123)
    assert started == {"host": "127.0.0.1", "port": 8123}


def test_public_host_with_a_token_is_allowed(tmp_path, make_settings, monkeypatch):
    server.use_settings(make_settings(tmp_path, auth_token=TOKEN))

    started = {}
    monkeypatch.setattr(
        "uvicorn.run", lambda app, host, port: started.update(host=host, port=port)
    )

    server.run_http(host="0.0.0.0", port=8124)
    assert started == {"host": "0.0.0.0", "port": 8124}


# --------------------------------------------------------------------------
# Live HTTP server
# --------------------------------------------------------------------------

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


MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}


@pytest.fixture
def live_server(tmp_path):
    """A real streamable-http server, started with a token, on a free port."""
    port = free_port()
    env = dict(os.environ)
    env["PYEXEC_AUTH_TOKEN"] = TOKEN
    env["PYEXEC_WORKSPACE"] = str(tmp_path)
    # Ignore any .env the developer has in the project root; this test supplies
    # its own configuration and should not care what else is lying around.
    env["PYEXEC_ENV_FILE"] = str(tmp_path / "no-such.env")

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
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        wait_until_listening(url)
        yield url
    finally:
        proc.kill()
        proc.wait(timeout=10)


def test_live_server_rejects_a_request_without_a_token(live_server):
    with httpx.Client(trust_env=False, timeout=10) as client:
        response = client.post(live_server, json=INITIALIZE, headers=MCP_HEADERS)

    assert response.status_code == 401
    assert "www-authenticate" in {k.lower() for k in response.headers}


def test_live_server_rejects_a_wrong_token(live_server):
    with httpx.Client(trust_env=False, timeout=10) as client:
        response = client.post(
            live_server,
            json=INITIALIZE,
            headers={**MCP_HEADERS, "Authorization": "Bearer wrong-token-entirely"},
        )

    assert response.status_code == 401


def test_live_server_accepts_the_right_token(live_server):
    with httpx.Client(trust_env=False, timeout=10) as client:
        response = client.post(
            live_server,
            json=INITIALIZE,
            headers={**MCP_HEADERS, "Authorization": f"Bearer {TOKEN}"},
        )

    assert response.status_code == 200
    assert "serverInfo" in response.text or "capabilities" in response.text
