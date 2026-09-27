"""Tests for the .env reader.

The parser is small but full of edge cases that only surface with real
credentials in the file -- a trailing quote, a ``#`` in a token, a BOM from
Notepad -- so each of those gets its own case.
"""

from __future__ import annotations

import os

import pytest

from pyexec_mcp import dotenv, server
from pyexec_mcp.config import Settings

TOKEN = "correct-horse-battery-staple-32chars"


@pytest.fixture
def env_guard():
    """Undo whatever a load() does to os.environ.

    monkeypatch cannot help here: it tracks the keys it sets itself, and
    dotenv writes to os.environ directly.
    """
    before = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(before)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

def test_simple_assignment():
    assert dotenv.parse("KEY=value") == {"KEY": "value"}


def test_comments_and_blank_lines_are_skipped():
    text = "# a comment\n\nKEY=value\n   \n# another\n"
    assert dotenv.parse(text) == {"KEY": "value"}


def test_export_prefix_is_accepted():
    assert dotenv.parse("export KEY=value") == {"KEY": "value"}


def test_surrounding_whitespace_is_trimmed():
    assert dotenv.parse("  KEY  =  value  ") == {"KEY": "value"}


def test_double_quotes_are_stripped():
    assert dotenv.parse('KEY="value"') == {"KEY": "value"}


def test_single_quotes_are_stripped_without_escape_processing():
    # A Windows path or a regex must survive verbatim.
    assert dotenv.parse(r"KEY='C:\new\table'") == {"KEY": r"C:\new\table"}


def test_escapes_apply_inside_double_quotes():
    assert dotenv.parse(r'KEY="line\nbreak"') == {"KEY": "line\nbreak"}


def test_escaped_backslash_does_not_become_a_newline():
    # r"\\n" is backslash-backslash-n: an escaped backslash, then a literal n.
    # Chained str.replace calls get this wrong; one regex pass does not.
    assert dotenv.parse(r'KEY="\\n"') == {"KEY": r"\n"}


def test_hash_inside_quotes_is_data():
    assert dotenv.parse('KEY="a#b"') == {"KEY": "a#b"}


def test_trailing_comment_on_an_unquoted_value():
    assert dotenv.parse("KEY=value # a note") == {"KEY": "value"}


def test_value_may_contain_equals_signs():
    assert dotenv.parse("KEY=a=b=c") == {"KEY": "a=b=c"}


def test_empty_value_is_allowed():
    assert dotenv.parse("KEY=") == {"KEY": ""}


@pytest.mark.parametrize(
    "line",
    ["novalue", "=value", "1KEY=value", "KEY WITH SPACE=value"],
)
def test_malformed_lines_are_ignored(line):
    assert dotenv.parse(line) == {}


def test_urlsafe_token_alphabet_round_trips():
    # What secrets.token_urlsafe(32) actually produces.
    assert dotenv.parse(f"KEY={TOKEN}") == {"KEY": TOKEN}


# --------------------------------------------------------------------------
# Loading into the environment
# --------------------------------------------------------------------------

def test_load_sets_missing_variables(tmp_path, monkeypatch, env_guard):
    monkeypatch.delenv("PYEXEC_AUTH_TOKEN", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(f"PYEXEC_AUTH_TOKEN={TOKEN}\n", encoding="utf-8")

    applied = dotenv.load(env_file)

    assert os.environ["PYEXEC_AUTH_TOKEN"] == TOKEN
    assert applied == ["PYEXEC_AUTH_TOKEN"]


def test_load_never_returns_values(tmp_path, env_guard):
    # The return value is logged, so it must not be able to leak the token.
    env_file = tmp_path / ".env"
    env_file.write_text(f"PYEXEC_AUTH_TOKEN={TOKEN}\n", encoding="utf-8")

    assert TOKEN not in repr(dotenv.load(env_file, override=True))


def test_the_real_environment_wins(tmp_path, monkeypatch, env_guard):
    monkeypatch.setenv("PYEXEC_AUTH_TOKEN", "from-the-real-environment")
    env_file = tmp_path / ".env"
    env_file.write_text(f"PYEXEC_AUTH_TOKEN={TOKEN}\n", encoding="utf-8")

    assert dotenv.load(env_file) == []
    assert os.environ["PYEXEC_AUTH_TOKEN"] == "from-the-real-environment"


def test_override_opts_into_the_file_winning(tmp_path, monkeypatch, env_guard):
    monkeypatch.setenv("PYEXEC_AUTH_TOKEN", "from-the-real-environment")
    env_file = tmp_path / ".env"
    env_file.write_text(f"PYEXEC_AUTH_TOKEN={TOKEN}\n", encoding="utf-8")

    dotenv.load(env_file, override=True)

    assert os.environ["PYEXEC_AUTH_TOKEN"] == TOKEN


def test_missing_file_is_not_an_error(tmp_path):
    assert dotenv.load(tmp_path / "absent.env") == []


def test_missing_file_can_be_made_an_error(tmp_path):
    with pytest.raises(ValueError, match="does not exist"):
        dotenv.load(tmp_path / "absent.env", required=True)


def test_bom_is_stripped(tmp_path, monkeypatch, env_guard):
    # Notepad and PowerShell write UTF-8 with a BOM; without stripping it the
    # first key becomes "\ufeffPYEXEC_AUTH_TOKEN" and silently never matches.
    monkeypatch.delenv("PYEXEC_AUTH_TOKEN", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"\xef\xbb\xbf" + f"PYEXEC_AUTH_TOKEN={TOKEN}\n".encode())

    assert dotenv.load(env_file) == ["PYEXEC_AUTH_TOKEN"]


# --------------------------------------------------------------------------
# Wiring into the server
# --------------------------------------------------------------------------

def test_server_reads_the_file_named_by_pyexec_env_file(
    tmp_path, monkeypatch, env_guard
):
    monkeypatch.delenv("PYEXEC_AUTH_TOKEN", raising=False)
    env_file = tmp_path / "custom.env"
    env_file.write_text(f"PYEXEC_AUTH_TOKEN={TOKEN}\n", encoding="utf-8")
    monkeypatch.setenv("PYEXEC_ENV_FILE", str(env_file))

    server._load_env_file()

    # The point of loading it: Settings must now see a valid, long-enough token.
    assert Settings.from_env().auth_token == TOKEN


def test_env_file_satisfies_the_binding_interlock(
    tmp_path, monkeypatch, env_guard, make_settings
):
    # The failure this whole module exists to fix: a token that looks
    # configured in a .env, and a server that refuses to bind because the
    # process never saw it.
    monkeypatch.delenv("PYEXEC_AUTH_TOKEN", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(f"PYEXEC_AUTH_TOKEN={TOKEN}\n", encoding="utf-8")
    monkeypatch.setenv("PYEXEC_ENV_FILE", str(env_file))

    started = {}
    monkeypatch.setattr(
        "uvicorn.run", lambda app, host, port: started.update(host=host, port=port)
    )

    server._load_env_file()
    server.use_settings(make_settings(tmp_path, auth_token=Settings.from_env().auth_token))
    server.run_http(host="0.0.0.0", port=8125)

    assert started == {"host": "0.0.0.0", "port": 8125}
