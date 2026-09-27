"""End-to-end test: launch the server as a real MCP client would.

Everything else in the suite calls the tool functions directly. This file
starts the server over stdio and speaks the protocol, so it also catches
problems the direct tests cannot see -- a tool whose schema will not serialise,
a server that dies on startup, a bad entry point.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

EXPECTED_TOOLS = {
    "pyexec_run",
    "pyexec_run_isolated",
    "pyexec_run_script",
    "pyexec_reset_session",
    "pyexec_session_info",
    "pyexec_read_file",
    "pyexec_write_file",
    "pyexec_list_files",
    "pyexec_list_packages",
    "pyexec_install_package",
}


def server_params(workspace) -> StdioServerParameters:
    env = dict(os.environ)
    env["PYEXEC_WORKSPACE"] = str(workspace)
    # The subprocess inherits this test runner's cwd, so without this it would
    # also pick up whatever .env the developer happens to have sitting there.
    # Tests must fail for their own reasons, not the machine's.
    env["PYEXEC_ENV_FILE"] = str(Path(workspace) / "no-such.env")
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "pyexec_mcp", "--workspace", str(workspace)],
        env=env,
    )


def payload(result) -> dict:
    """Read a tool's structured response.

    Falls back to parsing the text block so the test still reports something
    useful if structured output is ever dropped.
    """
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_tools_are_discoverable_over_stdio(tmp_path):
    async with stdio_client(server_params(tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            listing = await session.list_tools()
            names = {tool.name for tool in listing.tools}
            assert EXPECTED_TOOLS <= names

            for tool in listing.tools:
                assert tool.description, f"{tool.name} has no description"
                assert tool.input_schema, f"{tool.name} has no input schema"


@pytest.mark.asyncio
async def test_session_state_survives_across_protocol_calls(tmp_path):
    async with stdio_client(server_params(tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            first = await session.call_tool("pyexec_run", {"code": "n = 21"})
            assert payload(first)["ok"] is True

            second = await session.call_tool("pyexec_run", {"code": "n * 2"})
            body = payload(second)
            assert body["ok"] is True
            assert body["result"] == "42"


@pytest.mark.asyncio
async def test_isolated_run_is_actually_isolated(tmp_path):
    async with stdio_client(server_params(tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            await session.call_tool("pyexec_run", {"code": "shared = 'yes'"})
            result = await session.call_tool("pyexec_run_isolated", {"code": "shared"})

            body = payload(result)
            assert body["ok"] is False
            assert body["error"]["type"] == "NameError"


@pytest.mark.asyncio
async def test_file_tools_share_the_workspace(tmp_path):
    async with stdio_client(server_params(tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            await session.call_tool(
                "pyexec_write_file", {"path": "data.txt", "content": "42"}
            )
            content = await session.call_tool("pyexec_read_file", {"path": "data.txt"})
            assert payload(content)["content"] == "42"

            # And the executed code sees the same directory.
            listing = await session.call_tool(
                "pyexec_run", {"code": "__import__('os').listdir('.')"}
            )
            assert "data.txt" in payload(listing)["result"]


@pytest.mark.asyncio
async def test_config_resource_is_exposed(tmp_path):
    async with stdio_client(server_params(tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            resources = await session.list_resources()
            uris = {str(r.uri) for r in resources.resources}
            assert "pyexec://config" in uris

            content = await session.read_resource("pyexec://config")
            config = json.loads(content.contents[0].text)
            assert config["workspace"] == str(tmp_path)


@pytest.mark.asyncio
async def test_errors_come_back_as_results_not_protocol_failures(tmp_path):
    async with stdio_client(server_params(tmp_path)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # A failing snippet must be reported through the result object, and
            # the session must stay healthy afterwards.
            result = await session.call_tool("pyexec_run", {"code": "raise ValueError('x')"})
            assert payload(result)["error"]["type"] == "ValueError"

            after = await session.call_tool("pyexec_run", {"code": "1 + 1"})
            assert payload(after)["result"] == "2"


@pytest.mark.asyncio
async def test_guardrail_rejection_is_reported_cleanly(tmp_path):
    env = dict(os.environ)
    env["PYEXEC_WORKSPACE"] = str(tmp_path)
    env["PYEXEC_ALLOWED_MODULES"] = "math"
    env["PYEXEC_ENV_FILE"] = str(tmp_path / "no-such.env")
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "pyexec_mcp"],
        env=env,
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            result = await session.call_tool("pyexec_run", {"code": "import socket"})
            body = payload(result)
            assert body["ok"] is False
            assert body["error"]["type"] == "GuardrailError"

            allowed = await session.call_tool("pyexec_run", {"code": "import math"})
            assert payload(allowed)["ok"] is True
