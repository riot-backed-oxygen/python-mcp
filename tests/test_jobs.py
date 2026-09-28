"""Tests for background jobs.

The property that matters most is the one the module exists for: submitting
returns before the code has finished, and the work carries on without anyone
holding the call open. The second is that a job's end reaches its caller even
when the machinery around it fails -- a refused submission that started anyway,
or a notification that could not be delivered, are both worse than an error.

The trickiest to get right, and the ones worth reading first, are cancellation
(a job that was killed must not be relabelled as one that ran) and the
concurrency limit (which must refuse rather than queue).
"""

from __future__ import annotations

import time

import pytest

from conftest import (
    BOT_TOKEN,
    CHAT_ID,
    LIVE_TOKEN,
    wait_for,
)
from pyexec_mcp import jobs, server
from pyexec_mcp.jobs import JobLimitReached, JobManager
from pyexec_mcp.telegram import (
    MAX_MESSAGE_CHARS,
    ExecutionSummary,
    NotificationError,
    TelegramNotifier,
    format_message,
)

#: A snippet that sleeps long enough to be cancelled mid-flight, then leaves a
#: file behind. The file is the proof: if it exists, the process was not killed.
SLOW = """
import pathlib, time
time.sleep(1.5)
pathlib.Path('late.txt').write_text('the process survived')
print('finished')
"""


@pytest.fixture(autouse=True)
def no_real_telegram(sent):
    """Keep every test in this file off the network.

    Most tests here configure a bot so the job path behaves as it does in
    production, and a finishing job posts on its own thread. Without this that
    is a real request carrying a fake token -- slow, flaky, and reaching a third
    party from a unit test. Tests that assert on the message request ``sent``
    themselves and get this same list.
    """


@pytest.fixture
def jobs_server(make_settings, workspace):
    """Settings with notifications on and an address to build links from."""
    settings = make_settings(
        workspace,
        base_url="http://127.0.0.1:8123",
        auth_token=LIVE_TOKEN,
        telegram_bot_token=BOT_TOKEN,
        telegram_chat_id=CHAT_ID,
        telegram_min_duration=0.0,
    )
    server.use_settings(settings)
    return settings


@pytest.fixture
def manager(make_settings, tmp_path):
    """A bare JobManager, with no server presentation attached."""
    made = JobManager(make_settings(tmp_path))
    yield made
    made.shutdown()


# --------------------------------------------------------------------------
# Submitting returns at once
# --------------------------------------------------------------------------

def test_submitting_does_not_wait_for_the_job(jobs_server, workspace):
    receipt = server.pyexec_run_background(SLOW, label="slow one")

    assert receipt.ok is True
    assert receipt.job_id.startswith(jobs.ID_PREFIX)
    assert receipt.state == "running"
    # The whole point: the code has been handed off, not run.
    assert not (workspace / "late.txt").exists()
    assert wait_for(lambda: (workspace / "late.txt").exists())


def test_the_job_id_is_how_the_result_is_found(jobs_server):
    receipt = server.pyexec_run_background("40 + 2")

    listing = server.pyexec_job_status(receipt.job_id)
    assert listing.count == 1
    assert listing.jobs[0].job_id == receipt.job_id

    assert wait_for(lambda: server.pyexec_job_status(receipt.job_id).jobs[0].ok is True)
    result = server.pyexec_job_result(receipt.job_id)

    assert result.ok is True
    assert result.result == "42"


def test_a_script_on_disk_can_be_submitted(jobs_server, workspace):
    (workspace / "sweep.py").write_text(
        "import pathlib\npathlib.Path('sweep.csv').write_text('a,b\\n')\n",
        encoding="utf-8",
    )

    receipt = server.pyexec_run_script_background("sweep.py", artifacts=["sweep.csv"])

    assert receipt.ok is True
    assert receipt.subject == "sweep.py"
    assert receipt.artifacts == ["sweep.csv"]
    assert wait_for(lambda: (workspace / "sweep.csv").exists())
    assert wait_for(lambda: server.pyexec_job_status(receipt.job_id).jobs[0].ok is True)


def test_script_background_timeout_is_independent_of_foreground_limit(
    make_settings, workspace
):
    server.use_settings(
        make_settings(
            workspace,
            max_timeout=0.1,
            background_timeout=1.5,
            background_max_timeout=2.0,
        )
    )
    (workspace / "long.py").write_text(
        "import time\nend = time.monotonic() + 0.4\n"
        "while time.monotonic() < end:\n    pass\nprint('completed')\n",
        encoding="utf-8",
    )

    receipt = server.pyexec_run_script_background("long.py")

    assert receipt.ok is True
    assert wait_for(lambda: server.pyexec_job_status(receipt.job_id).jobs[0].state == "done")
    result = server.pyexec_job_result(receipt.job_id)
    assert result.ok is True
    assert "completed" in result.stdout


def test_script_background_timeout_uses_its_own_maximum(make_settings, workspace):
    server.use_settings(
        make_settings(
            workspace,
            max_timeout=0.1,
            background_timeout=1.5,
            background_max_timeout=0.3,
        )
    )
    (workspace / "loop.py").write_text("while True:\n    pass\n", encoding="utf-8")

    receipt = server.pyexec_run_script_background("loop.py", timeout=10)

    assert receipt.ok is True
    assert wait_for(lambda: server.pyexec_job_status(receipt.job_id).jobs[0].state == "done")
    result = server.pyexec_job_result(receipt.job_id)
    assert result.ok is False
    assert result.error.type == "TimeoutError"
    assert "0.3s" in result.error.message


# --------------------------------------------------------------------------
# Cancelling
# --------------------------------------------------------------------------

def test_cancelling_really_kills_the_process(jobs_server, workspace):
    receipt = server.pyexec_run_background(SLOW)
    session = server.job_manager().get(receipt.job_id).session

    status = server.pyexec_job_cancel(receipt.job_id)

    assert status.state == "cancelled"
    assert status.ok is False
    assert "cancelled" in status.note
    assert session.alive is False

    # Past the point the snippet would have written its file, had it lived.
    time.sleep(2.0)
    assert not (workspace / "late.txt").exists()


def test_cancelling_a_finished_job_changes_nothing(jobs_server):
    receipt = server.pyexec_run_background("1 + 1")
    assert wait_for(lambda: server.pyexec_job_status(receipt.job_id).jobs[0].ok is True)

    status = server.pyexec_job_cancel(receipt.job_id)

    # The work happened; relabelling it cancelled would hide a result that
    # exists, and this call is idempotent by design.
    assert status.state == "done"
    assert status.ok is True


def test_a_cancelled_job_keeps_no_result(jobs_server):
    receipt = server.pyexec_run_background(SLOW)
    server.pyexec_job_cancel(receipt.job_id)

    result = server.pyexec_job_result(receipt.job_id)

    assert result.ok is False
    assert result.error.type == "CancelledError"
    assert "incomplete" in result.error.message


# --------------------------------------------------------------------------
# Reading a result that is not ready
# --------------------------------------------------------------------------

def test_fetching_a_running_job_is_refused_rather_than_empty(jobs_server):
    receipt = server.pyexec_run_background(SLOW)

    with pytest.raises(server.ToolError, match="still running"):
        server.pyexec_job_result(receipt.job_id)


def test_a_running_job_reports_no_outcome_rather_than_failure(jobs_server):
    receipt = server.pyexec_run_background(SLOW)

    status = server.pyexec_job_status(receipt.job_id).jobs[0]

    # None, not False: a caller reads False as "it failed" and would say so.
    assert status.ok is None
    assert status.state == "running"
    assert status.finished_at is None


def test_an_unknown_job_id_says_the_records_are_memory_only(make_settings, tmp_path):
    server.use_settings(make_settings(tmp_path))

    with pytest.raises(server.ToolError, match="memory only"):
        server.pyexec_job_result("job-nosuchid")

    with pytest.raises(server.ToolError, match="No such job"):
        server.pyexec_job_cancel("job-nosuchid")


def test_listing_with_no_argument_shows_what_is_known(jobs_server):
    first = server.pyexec_run_background("1")
    second = server.pyexec_run_background("2")

    listing = server.pyexec_job_status()

    assert listing.count == 2
    assert {job.job_id for job in listing.jobs} == {first.job_id, second.job_id}
    # Newest first, which is the order a reader wants.
    assert listing.jobs[0].job_id == second.job_id
    assert listing.limit == jobs_server.max_background_jobs


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------

def test_the_concurrency_limit_refuses_rather_than_queues(jobs_server):
    for _ in range(jobs_server.max_background_jobs):
        assert server.pyexec_run_background(SLOW).ok is True

    refused = server.pyexec_run_background(SLOW)

    assert refused.ok is False
    assert refused.job_id == ""
    assert "already running" in refused.reason
    # The summary states the number, so the caller can decide what to cancel.
    assert str(jobs_server.max_background_jobs) in refused.reason


def test_a_refused_submission_starts_nothing(jobs_server):
    for _ in range(jobs_server.max_background_jobs):
        server.pyexec_run_background(SLOW)

    before = len(server.job_manager().all())
    server.pyexec_run_background(SLOW)

    assert len(server.job_manager().all()) == before


def test_a_blocked_import_is_refused_at_submit(make_settings, workspace):
    server.use_settings(
        make_settings(
            workspace,
            allowed_modules=frozenset({"math"}),
            max_background_jobs=1,
        )
    )

    receipt = server.pyexec_run_background("import socket")

    assert receipt.ok is False
    assert "GuardrailError" in receipt.reason
    # Nothing was registered, so the slot is still free for a legal submission.
    assert server.job_manager().running() == []
    assert server.pyexec_run_background("import math").ok is True


def test_an_artifact_outside_the_workspace_is_refused(jobs_server):
    receipt = server.pyexec_run_background("1", artifacts=["../../etc/passwd"])

    assert receipt.ok is False
    assert "GuardrailError" in receipt.reason
    assert server.job_manager().running() == []


def test_a_missing_script_is_refused(jobs_server):
    receipt = server.pyexec_run_script_background("no-such-file.py")

    assert receipt.ok is False
    assert "FileNotFoundError" in receipt.reason
    assert server.job_manager().running() == []


def test_an_interpreter_that_cannot_start_is_reported_at_submit(make_settings, workspace):
    server.use_settings(
        make_settings(workspace, python_executable="definitely-not-a-python")
    )

    receipt = server.pyexec_run_background("1")

    assert receipt.ok is False
    assert "SessionError" in receipt.reason


# --------------------------------------------------------------------------
# Artifacts
# --------------------------------------------------------------------------

def test_an_artifact_that_lands_gets_a_link_in_the_notification(
    jobs_server, sent, workspace
):
    receipt = server.pyexec_run_background(
        "import pathlib\npathlib.Path('out.csv').write_bytes(b'a,b\\n')",
        artifacts=["out.csv"],
    )

    assert wait_for(lambda: len(sent) == 1)
    assert "/download/out.csv" in sent[0]
    assert receipt.job_id in sent[0]

    assert wait_for(
        lambda: server.pyexec_job_result(receipt.job_id).artifacts != []
    )
    link = server.pyexec_job_result(receipt.job_id).artifacts[0]
    assert link.path == "out.csv"
    assert link.size_bytes == 4


def test_an_artifact_that_never_appears_is_reported_not_fatal(jobs_server, workspace):
    receipt = server.pyexec_run_background("print('nothing written')", artifacts=["ghost.csv"])
    assert wait_for(lambda: server.pyexec_job_status(receipt.job_id).jobs[0].ok is True)

    status = server.pyexec_job_status(receipt.job_id).jobs[0]
    result = server.pyexec_job_result(receipt.job_id)

    # Declaring a file is a statement of intent, and the job failing to produce
    # it is worth saying -- but it is not the job failing.
    assert status.missing_artifacts == ["ghost.csv"]
    assert result.missing_artifacts == ["ghost.csv"]
    assert result.artifacts == []
    assert result.ok is True
    assert "nothing written" in result.stdout


def test_no_address_means_no_links_and_no_false_missing_claim(make_settings, workspace):
    server.use_settings(make_settings(workspace, base_url=""))
    receipt = server.pyexec_run_background("1", artifacts=["a.txt"])
    assert wait_for(lambda: server.pyexec_job_status(receipt.job_id).jobs[0].ok is True)

    status = server.pyexec_job_status(receipt.job_id).jobs[0]

    # The file really is absent here, but the server has no way to tell a
    # person where it would be even if it existed -- so it says nothing.
    assert status.missing_artifacts == []


# --------------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------------

def test_a_background_job_ignores_the_duration_threshold(make_settings, workspace, sent):
    server.use_settings(
        make_settings(
            workspace,
            telegram_bot_token=BOT_TOKEN,
            telegram_chat_id=CHAT_ID,
            telegram_min_duration=3600.0,
        )
    )

    server.pyexec_run_background("'quick'")

    # The act of backgrounding something is itself the request to be told when
    # it lands, so the threshold a blocking run answers to does not apply.
    assert wait_for(lambda: len(sent) == 1)
    assert "finished" in sent[0]


def test_the_receipt_says_whether_anyone_will_be_told(make_settings, workspace):
    server.use_settings(make_settings(workspace, telegram_min_duration=3600.0))

    receipt = server.pyexec_run_background("1")

    # Nothing is configured, so this job has no reporter at all. The receipt has
    # to say so, because the alternative is a caller who assumes one exists.
    assert receipt.notifies is False


def test_a_failed_delivery_does_not_touch_the_job(jobs_server, monkeypatch, caplog):
    def explode(self, text):
        raise NotificationError("chat not found")

    monkeypatch.setattr(TelegramNotifier, "send", explode)

    receipt = server.pyexec_run_background("print('still fine')")
    assert wait_for(lambda: server.pyexec_job_status(receipt.job_id).jobs[0].ok is True)

    result = server.pyexec_job_result(receipt.job_id)

    assert result.ok is True
    assert "still fine" in result.stdout


def test_the_notification_carries_the_output_and_the_id(jobs_server, sent):
    receipt = server.pyexec_run_background("print('row 4000 processed')")

    assert wait_for(lambda: len(sent) == 1)
    assert "pyexec_run_background finished" in sent[0]
    assert receipt.job_id in sent[0]
    assert "row 4000 processed" in sent[0]


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------

def test_a_finished_job_is_forgotten_once_history_is_full(make_settings, tmp_path):
    made = JobManager(make_settings(tmp_path, job_history=1))
    try:
        first = made.submit(tool="t", subject="", code="1", timeout=10.0)
        assert wait_for(lambda: made.get(first.id).state != "running")
        second = made.submit(tool="t", subject="", code="2", timeout=10.0)
        assert wait_for(lambda: made.get(second.id).state != "running")

        assert made.get(first.id) is None
        assert made.get(second.id) is not None
    finally:
        made.shutdown()


def test_a_running_job_is_never_forgotten(make_settings, tmp_path):
    made = JobManager(make_settings(tmp_path, job_history=0))
    try:
        first = made.submit(tool="t", subject="", code=SLOW, timeout=30.0)
        second = made.submit(tool="t", subject="", code=SLOW, timeout=30.0)

        # Zero history means finished jobs are dropped at once, but neither of
        # these is finished -- and evicting a job someone can still cancel or
        # read would be a bug, not a limit.
        assert made.get(first.id) is not None
        assert made.get(second.id) is not None
    finally:
        made.shutdown()


def test_the_manager_refuses_past_its_limit(make_settings, tmp_path):
    made = JobManager(make_settings(tmp_path, max_background_jobs=1))
    try:
        made.submit(tool="t", subject="", code=SLOW, timeout=30.0)

        with pytest.raises(JobLimitReached):
            made.submit(tool="t", subject="", code="1", timeout=30.0)
    finally:
        made.shutdown()


def test_a_callback_that_explodes_leaves_the_job_finished(make_settings, tmp_path):
    def explode(job, result, duration_s):
        raise RuntimeError("the notifier is on fire")

    made = JobManager(make_settings(tmp_path), on_finish=explode)
    try:
        job = made.submit(tool="t", subject="", code="1", timeout=10.0)

        assert wait_for(lambda: made.get(job.id).state == "done")
        assert made.get(job.id).result.ok is True
        assert made.get(job.id).notified is False
    finally:
        made.shutdown()


def test_the_callback_sees_the_result_and_the_duration(make_settings, tmp_path):
    seen: list[tuple] = []

    def record(job, result, duration_s):
        seen.append((job.id, result.ok, duration_s))
        return True

    made = JobManager(make_settings(tmp_path), on_finish=record)
    try:
        job = made.submit(tool="t", subject="", code="'hello'", timeout=10.0)

        assert wait_for(lambda: seen)
        assert seen[0][0] == job.id
        assert seen[0][1] is True
        assert seen[0][2] >= 0.0
        assert made.get(job.id).notified is True
    finally:
        made.shutdown()


def test_shutdown_kills_running_jobs_without_announcing_them(make_settings, tmp_path):
    announced: list[str] = []
    made = JobManager(
        make_settings(tmp_path), on_finish=lambda job, result, d: announced.append(job.id)
    )
    job = made.submit(tool="t", subject="", code=SLOW, timeout=30.0)

    made.shutdown()

    assert wait_for(lambda: made.get(job.id).state == "cancelled")
    # A server on its way out has nobody left to tell.
    assert announced == []
    assert made.get(job.id).result.error["type"] == "CancelledError"


# --------------------------------------------------------------------------
# What the message looks like
# --------------------------------------------------------------------------

def test_the_message_names_the_job_and_its_links():
    message = format_message(
        ExecutionSummary(
            tool="pyexec_run_background",
            subject="nightly sweep",
            ok=True,
            duration_s=90.0,
            excerpt="4000 rows",
            machine="workstation",
            job_id="job-1a2b3c4d",
            links=("http://host/download/sweep.csv?sig=abc",),
        )
    )

    assert "workstation · job-1a2b3c4d · nightly sweep" in message
    assert "🔗 http://host/download/sweep.csv?sig=abc" in message


def test_links_survive_an_excerpt_that_would_have_pushed_them_out():
    message = format_message(
        ExecutionSummary(
            tool="pyexec_run_background",
            ok=True,
            duration_s=1.0,
            excerpt="x" * 10_000,
            links=("http://host/download/out.csv?sig=abc",),
        )
    )

    # The clip cuts from the tail, which is exactly where the links are -- so a
    # chatty job could otherwise lose the one thing the message was for.
    assert "http://host/download/out.csv?sig=abc" in message
    assert "[message truncated]" in message


def test_links_are_dropped_whole_when_there_are_too_many():
    links = tuple(f"http://host/download/f{index}.csv?sig={'a' * 400}" for index in range(20))

    message = format_message(
        ExecutionSummary(tool="pyexec_run_background", duration_s=1.0, links=links)
    )

    assert len(message) <= MAX_MESSAGE_CHARS
    # Never half a URL, and never silence about what was left out.
    assert "more artifact(s)" in message
    for line in message.splitlines():
        if line.startswith("🔗 http"):
            assert line.split()[1] in links


def test_no_job_id_means_no_extra_field_in_the_header():
    message = format_message(
        ExecutionSummary(tool="pyexec_run", duration_s=1.0, machine="workstation")
    )

    assert message.splitlines()[1] == "workstation"
