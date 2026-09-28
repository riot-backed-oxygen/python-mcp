"""Tests for Telegram notifications.

Two properties matter more than the happy path. First, an unconfigured or
unreachable bot must not change anything about a run: the tool's return value is
what the caller acts on, and a chat message is only ever a convenience. Second,
the bot token must not leak -- not into a log line, not into an error message,
not into the configuration resource a client can read.
"""

from __future__ import annotations

import io
import json
import time
import urllib.error

import pytest

from conftest import (
    BOT_TOKEN,
    CHAT_ID,
    FakeOpener,
    wait_for,
)
from pyexec_mcp import server, telegram
from pyexec_mcp.config import (
    DEFAULT_TELEGRAM_MIN_DURATION,
    Settings,
)
from pyexec_mcp.telegram import (
    MAX_MESSAGE_CHARS,
    ExecutionSummary,
    NotificationError,
    TelegramNotifier,
    format_message,
)

def test_notifications_need_both_a_token_and_a_chat(make_settings, tmp_path):
    half = [
        dict(telegram_bot_token=BOT_TOKEN),
        dict(telegram_chat_id=CHAT_ID),
    ]
    for overrides in half:
        settings = make_settings(tmp_path, **overrides)
        assert telegram.notifier(settings).enabled is False

    both = make_settings(
        tmp_path, telegram_bot_token=BOT_TOKEN, telegram_chat_id=CHAT_ID
    )
    assert telegram.notifier(both).enabled is True


def test_configuration_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("PYEXEC_TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("PYEXEC_TELEGRAM_CHAT_ID", CHAT_ID)
    monkeypatch.setenv("PYEXEC_TELEGRAM_MIN_DURATION", "5")

    settings = Settings.from_env()

    assert settings.telegram_bot_token == BOT_TOKEN
    assert settings.telegram_chat_id == CHAT_ID
    assert settings.telegram_min_duration == 5.0


def test_the_threshold_defaults_to_a_minute(monkeypatch):
    monkeypatch.setenv("PYEXEC_TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    monkeypatch.setenv("PYEXEC_TELEGRAM_CHAT_ID", CHAT_ID)

    assert Settings.from_env().telegram_min_duration == DEFAULT_TELEGRAM_MIN_DURATION


def test_token_from_file_is_supported(tmp_path, monkeypatch):
    secret = tmp_path / "telegram-token"
    secret.write_text(f"  {BOT_TOKEN}\n", encoding="utf-8")
    monkeypatch.setenv("PYEXEC_TELEGRAM_BOT_TOKEN_FILE", str(secret))

    assert Settings.from_env().telegram_bot_token == BOT_TOKEN


def test_a_misshapen_token_is_refused_without_echoing_it(monkeypatch):
    monkeypatch.setenv("PYEXEC_TELEGRAM_BOT_TOKEN", "not-a-bot-token")

    with pytest.raises(ValueError, match="shaped like a bot token") as caught:
        Settings.from_env()

    assert "not-a-bot-token" not in str(caught.value)


def test_an_empty_proxy_means_connect_directly(monkeypatch):
    assert Settings.from_env().telegram_proxy is None

    monkeypatch.setenv("PYEXEC_TELEGRAM_PROXY", "  ")
    assert Settings.from_env().telegram_proxy == ""

    monkeypatch.setenv("PYEXEC_TELEGRAM_PROXY", "  http://127.0.0.1:7890 ")
    assert Settings.from_env().telegram_proxy == "http://127.0.0.1:7890"


def _proxies(opener: urllib.request.OpenerDirector) -> dict[str, str]:
    for handler in opener.handlers:
        if isinstance(handler, urllib.request.ProxyHandler):
            return dict(handler.proxies)
    return {}


def test_the_proxy_setting_decides_what_the_opener_uses(
    make_settings, tmp_path, monkeypatch
):
    # Both variables, because ``urllib`` reads the whole environment and a
    # developer's own HTTP_PROXY would otherwise show up in the assertion.
    monkeypatch.setenv("HTTP_PROXY", "http://environment-proxy:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://environment-proxy:3128")

    env_default = make_settings(tmp_path).telegram_proxy
    assert env_default is None
    assert _proxies(TelegramNotifier(make_settings(tmp_path))._opener()) == {
        "http": "http://environment-proxy:3128",
        "https": "http://environment-proxy:3128",
    }

    direct = make_settings(tmp_path, telegram_proxy="")
    assert _proxies(TelegramNotifier(direct)._opener()) == {}

    explicit = make_settings(tmp_path, telegram_proxy="http://127.0.0.1:7890")
    assert _proxies(TelegramNotifier(explicit)._opener()) == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }


# --------------------------------------------------------------------------
# The threshold
# --------------------------------------------------------------------------

def test_short_runs_are_not_announced(make_settings, tmp_path):
    settings = make_settings(
        tmp_path,
        telegram_bot_token=BOT_TOKEN,
        telegram_chat_id=CHAT_ID,
        telegram_min_duration=60.0,
    )
    notifier = telegram.notifier(settings)

    assert notifier.should_notify(1.0) is False
    assert notifier.should_notify(59.9) is False
    assert notifier.should_notify(60.0) is True
    assert notifier.notify(ExecutionSummary(tool="pyexec_run", duration_s=1.0)) is None


def test_zero_announces_every_run(make_settings, tmp_path, sent):
    settings = make_settings(
        tmp_path,
        telegram_bot_token=BOT_TOKEN,
        telegram_chat_id=CHAT_ID,
        telegram_min_duration=0.0,
    )

    thread = telegram.notify(settings, ExecutionSummary(tool="pyexec_run"))

    assert thread is not None
    thread.join(timeout=10)
    assert len(sent) == 1


def test_an_unconfigured_server_announces_nothing(make_settings, tmp_path):
    thread = telegram.notify(
        make_settings(tmp_path), ExecutionSummary(tool="pyexec_run", duration_s=3600)
    )
    assert thread is None


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------

def test_send_posts_the_message_to_the_bot(configured, fake_api):
    telegram.notifier(configured).send("hello")

    assert fake_api.url == f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    assert fake_api.fields["chat_id"] == CHAT_ID
    assert fake_api.fields["text"] == "hello"
    assert fake_api.fields["disable_web_page_preview"] == "true"


def test_send_surfaces_telegrams_own_reason(configured, monkeypatch):
    body = json.dumps({"ok": False, "description": "chat not found"}).encode()
    error = urllib.error.HTTPError(
        "https://api.telegram.org", 400, "Bad Request", {}, io.BytesIO(body)
    )
    monkeypatch.setattr(TelegramNotifier, "_opener", lambda self: FakeOpener(error=error))

    with pytest.raises(NotificationError, match="chat not found"):
        telegram.notifier(configured).send("hello")


def test_a_200_that_says_not_ok_is_still_a_failure(configured, monkeypatch):
    body = json.dumps({"ok": False, "description": "bot was blocked"}).encode()
    monkeypatch.setattr(
        TelegramNotifier, "_opener", lambda self: FakeOpener(body=body)
    )

    with pytest.raises(NotificationError, match="bot was blocked"):
        telegram.notifier(configured).send("hello")


def test_an_unconfigured_send_explains_what_to_set(make_settings, tmp_path):
    with pytest.raises(NotificationError, match="PYEXEC_TELEGRAM_BOT_TOKEN"):
        telegram.notifier(make_settings(tmp_path)).send("hello")


def test_an_unreachable_api_points_at_the_proxy_setting(configured, monkeypatch):
    error = urllib.error.URLError("no route to host")
    monkeypatch.setattr(TelegramNotifier, "_opener", lambda self: FakeOpener(error=error))

    with pytest.raises(NotificationError, match="PYEXEC_TELEGRAM_PROXY"):
        telegram.notifier(configured).send("hello")


def test_a_failed_delivery_stays_on_its_thread(configured, monkeypatch, caplog):
    def explode(self, text):
        raise NotificationError("nope")

    monkeypatch.setattr(TelegramNotifier, "send", explode)

    with caplog.at_level("ERROR", logger="pyexec_mcp.telegram"):
        thread = telegram.notify(
            configured, ExecutionSummary(tool="pyexec_run", duration_s=120)
        )
        assert thread is not None
        thread.join(timeout=10)

    assert not thread.is_alive()
    assert "Could not deliver" in caplog.text


# --------------------------------------------------------------------------
# The message itself
# --------------------------------------------------------------------------

def test_a_finished_run_reads_as_a_summary():
    message = format_message(
        ExecutionSummary(
            tool="pyexec_run_script",
            subject="analysis/sweep.py",
            ok=True,
            duration_s=132.4,
            excerpt="processed 4000 rows",
            machine="workstation",
        )
    )

    assert message.splitlines()[0] == "✅ pyexec_run_script finished in 2m12s"
    assert "workstation · analysis/sweep.py" in message
    assert "processed 4000 rows" in message


def test_a_failure_carries_the_error_and_the_truncation_note():
    message = format_message(
        ExecutionSummary(
            tool="pyexec_run",
            ok=False,
            duration_s=61.0,
            error="TimeoutError: Execution exceeded 60s and the worker was killed",
            excerpt="starting pass 7",
            truncated=True,
        )
    )

    assert message.startswith("❌ pyexec_run FAILED in 1m01s")
    assert "TimeoutError" in message
    assert "output cap" in message


def test_a_long_message_is_clipped_below_telegrams_limit():
    message = format_message(
        ExecutionSummary(tool="pyexec_run", duration_s=1.0, excerpt="x" * 10_000)
    )

    assert len(message) <= MAX_MESSAGE_CHARS + len("\n... [message truncated]")
    assert message.endswith("[message truncated]")


# --------------------------------------------------------------------------
# The tools
# --------------------------------------------------------------------------

def test_pyexec_notify_reports_a_missing_configuration(workspace):
    receipt = server.pyexec_notify("hello")

    assert receipt.ok is False
    assert "PYEXEC_TELEGRAM_BOT_TOKEN" in receipt.reason


def test_pyexec_notify_sends_when_configured(configured, sent):
    receipt = server.pyexec_notify("the sweep is done")

    assert receipt.ok is True
    assert sent == ["the sweep is done"]


def test_pyexec_notify_reports_a_refusal(configured, monkeypatch):
    def refuse(self, text):
        raise NotificationError("chat not found")

    monkeypatch.setattr(TelegramNotifier, "send", refuse)

    receipt = server.pyexec_notify("hello")

    assert receipt.ok is False
    assert receipt.reason == "chat not found"


def test_a_long_script_run_is_announced(configured, sent, workspace):
    script = workspace / "slow.py"
    script.write_text("print('done')\n", encoding="utf-8")

    result = server.pyexec_run_script("slow.py")
    assert result.ok is True

    assert wait_for(lambda: len(sent) == 1)
    assert "pyexec_run_script finished" in sent[0]
    assert "slow.py" in sent[0]
    assert "done" in sent[0]


def test_a_quick_run_is_not_announced(make_settings, sent, workspace):
    server.use_settings(
        make_settings(
            workspace,
            telegram_bot_token=BOT_TOKEN,
            telegram_chat_id=CHAT_ID,
            telegram_min_duration=3600.0,
        )
    )

    assert server.pyexec_run("1 + 1").ok is True
    time.sleep(0.2)
    assert sent == []


def test_a_failed_notification_leaves_the_result_alone(configured, monkeypatch, workspace):
    def explode(self, text):
        raise NotificationError("nope")

    monkeypatch.setattr(TelegramNotifier, "send", explode)
    script = workspace / "slow.py"
    script.write_text("print('still fine')\n", encoding="utf-8")

    result = server.pyexec_run_script("slow.py")
    time.sleep(0.2)  # let the announcement thread do its worst

    assert result.ok is True
    assert "still fine" in result.stdout


# --------------------------------------------------------------------------
# What travels with the message
# --------------------------------------------------------------------------

def _result(**overrides) -> server.ExecutionResult:
    base = dict(ok=True, stdout="", stderr="", duration_ms=1.0)
    base.update(overrides)
    return server.ExecutionResult(**base)


def test_the_excerpt_carries_what_the_run_printed():
    assert server._excerpt(_result(stdout="a\nb\nc")) == "a\nb\nc"


def test_a_run_that_printed_nothing_falls_back_to_its_value():
    assert server._excerpt(_result(result="42")) == "42"


def test_a_long_excerpt_keeps_the_end():
    excerpt = server._excerpt(_result(stdout="x" * 5_000 + "THE END"))

    assert excerpt.startswith("...")
    assert excerpt.endswith("THE END")


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def test_the_config_resource_reveals_no_token(configured):
    resource = server.config_resource()

    assert BOT_TOKEN not in resource
    assert json.loads(resource)["telegram"]["enabled"] is True
    assert json.loads(resource)["telegram"]["chat_id"] == CHAT_ID


def test_a_token_without_a_chat_id_is_reported_at_startup(
    make_settings, tmp_path, caplog
):
    settings = make_settings(tmp_path, telegram_bot_token=BOT_TOKEN)

    with caplog.at_level("WARNING", logger="pyexec_mcp.telegram"):
        server._report_notifications(settings)

    assert "PYEXEC_TELEGRAM_CHAT_ID is not" in caplog.text
    assert BOT_TOKEN not in caplog.text


def test_a_configured_bot_is_reported_at_startup(configured, caplog):
    with caplog.at_level("INFO", logger="pyexec_mcp.telegram"):
        server._report_notifications(configured)

    assert CHAT_ID in caplog.text
    assert BOT_TOKEN not in caplog.text
