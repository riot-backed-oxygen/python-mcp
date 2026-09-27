"""Shared fixtures for the test suite."""

from __future__ import annotations

import sys

import pytest

from pyexec_mcp import server
from pyexec_mcp.config import Settings


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
