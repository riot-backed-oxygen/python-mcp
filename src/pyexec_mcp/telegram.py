"""Telegram notifications for runs long enough that their caller walked away.

A tool call returns only when it is finished, so a script that takes ten minutes
leaves nothing to look at until it lands. With a bot token and a chat id
configured, this module posts to that chat when a long run finishes, carrying
the outcome and a tail of what it printed.

Two rules shape everything here:

* **A notification can never change the result of the run it describes.**
  Delivery happens after the result is built, on a background thread, and every
  failure is logged and swallowed. The chat message is a convenience; the return
  value is the contract.
* **The token never reaches the log.** Messages report the chat id and the fact
  that notifications are on, and nothing that would let a reader post as the bot.

The Bot API is called with :mod:`urllib.request` rather than a client library,
for the same reason :mod:`pyexec_mcp.dotenv` is hand-rolled: the one request
this needs is a form POST, and a fresh install is better off without another
moving part.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .config import Settings

logger = logging.getLogger(__name__)

#: Where the Bot API lives; only the path varies per bot.
API_ROOT = "https://api.telegram.org"

#: Ceiling on a message body. Telegram rejects anything past 4096 characters
#: with a 400, and the margin keeps the header and the truncation note from
#: pushing a long excerpt over that edge.
MAX_MESSAGE_CHARS = 3500

#: Appended when the body had to be cut. A named constant because
#: :func:`_with_links` budgets for its length when deciding how much excerpt
#: fits alongside the links -- an inline literal there would drift.
CLIP_NOTE = "\n... [message truncated]"

#: How much of the run's output travels with the message: enough to see the last
#: lines of progress or the exception, not enough to be a second copy of the
#: context the caller already has.
EXCERPT_CHARS = 900

#: Seconds to wait on the Bot API. Short, because this shares a process with a
#: server that may be shutting down, and a notification is never worth a stall.
SEND_TIMEOUT = 10.0


class NotificationError(RuntimeError):
    """The message could not be delivered.

    Raised by :meth:`TelegramNotifier.send`, which the explicit
    ``pyexec_notify`` tool surfaces to its caller. Automatic notifications go
    through :meth:`TelegramNotifier.notify`, which logs this instead.
    """


@dataclass(frozen=True)
class ExecutionSummary:
    """What a finished run is worth telling someone about."""

    #: Tool that ran, e.g. ``pyexec_run_script``.
    tool: str
    #: What it ran against -- a script path, when there is one.
    subject: str = ""
    ok: bool = True
    #: Wall-clock seconds the caller waited, which is what makes a run "long".
    duration_s: float = 0.0
    #: ``"ValueError: bad input"``, set when the run failed.
    error: str = ""
    #: Tail of the run's output, already clipped by the caller.
    excerpt: str = ""
    truncated: bool = False
    #: Hostname, so a chat fed by several machines can tell them apart.
    machine: str = ""
    #: Set for a background job, so the message names the thing to go and read
    #: the result from rather than the tool call that has long since returned.
    job_id: str = ""
    #: Download URLs for the run's artifacts, in the order they were asked for.
    #: Built by the caller, which is where the signing key lives.
    links: tuple[str, ...] = ()


def api_url(bot_token: str) -> str:
    """The ``sendMessage`` endpoint for *bot_token*."""
    return f"{API_ROOT}/bot{bot_token}/sendMessage"


def format_message(summary: ExecutionSummary) -> str:
    """Render *summary* as the message body.

    Plain text, no ``parse_mode``: output from arbitrary code routinely contains
    the characters Markdown and HTML give meaning to, and escaping it all to
    keep Telegram from rejecting the message buys nothing a reader wants.
    """
    verb = "finished" if summary.ok else "FAILED"
    lines = [f"{'✅' if summary.ok else '❌'} {summary.tool} {verb} in {_duration(summary.duration_s)}"]

    where = [
        part for part in (summary.machine, summary.job_id, summary.subject) if part
    ]
    if where:
        lines.append(" · ".join(where))

    if summary.error:
        lines.append(summary.error)
    if summary.excerpt:
        lines.append("")
        lines.append(summary.excerpt)
    if summary.truncated:
        lines.append("")
        lines.append("(output was truncated by the server's output cap)")

    body = "\n".join(lines)
    if summary.links:
        return _with_links(body, summary.links)
    return _clip(body, MAX_MESSAGE_CHARS)


def _with_links(body: str, links: tuple[str, ...]) -> str:
    """Append download links, clipping the excerpt and dropping whole links.

    Links go last because they are what a reader taps, and the excerpt above
    them is the part they can scroll past -- so when the two will not both fit,
    the excerpt is what gives way. Cutting the tail instead, which is what
    :func:`_clip` does on its own, would take away the file from a message whose
    only point was the file.

    Whole links are dropped rather than trailing characters, never half a URL,
    and the count of what was left out is said rather than silently swallowed.
    One link is always kept: a message too long for Telegram is rejected, and a
    description of a file with no way to reach it is not worth the rejection.
    """
    kept = len(links)
    while True:
        tail = _render_links("", links, kept)
        room = MAX_MESSAGE_CHARS - len(tail) - len(CLIP_NOTE)
        rendered = _render_links(_clip(body, max(0, room)), links, kept)
        if len(rendered) <= MAX_MESSAGE_CHARS or kept == 1:
            return rendered
        kept -= 1


def _render_links(body: str, links: tuple[str, ...], kept: int) -> str:
    shown = [f"🔗 {link}" for link in links[:kept]]
    if kept < len(links):
        shown.append(f"🔗 ... and {len(links) - kept} more artifact(s)")
    return f"{body}\n\n" + "\n".join(shown)


def _duration(seconds: float) -> str:
    """Seconds in the shape a person reads at a glance."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes}m{rest:02d}s"


def _clip(text: str, limit: int) -> str:
    """Truncate *text* to *limit* characters, saying that we did."""
    if len(text) <= limit:
        return text
    return text[:limit] + CLIP_NOTE


def machine_name() -> str:
    """This host's name, or ``""`` if the OS will not say.

    Resolved once -- it cannot change while the process lives, and the send path
    should not pay a syscall to repeat it.
    """
    global _MACHINE
    if _MACHINE is None:
        try:
            _MACHINE = socket.gethostname()
        except OSError:  # pragma: no cover - defensive
            _MACHINE = ""
    return _MACHINE


_MACHINE: str | None = None


class TelegramNotifier:
    """Posts run summaries to one chat.

    Cheap to construct -- it holds configuration, not connections -- so it is
    built per settings object rather than cached against a live one.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def enabled(self) -> bool:
        """Whether a bot token and a chat id are both configured."""
        return bool(
            self.settings.telegram_bot_token and self.settings.telegram_chat_id
        )

    def should_notify(self, duration_s: float, *, always: bool = False) -> bool:
        """Whether a run of *duration_s* seconds clears the configured bar.

        The bar exists so a chat does not fill with notifications for runs that
        were never worth walking away from; ``PYEXEC_TELEGRAM_MIN_DURATION=0``
        sends one for every run. *always* skips the bar, which is what a
        background job asks for: its caller has explicitly stopped watching, so
        the message is the only report rather than a duplicate of a result
        already in hand.
        """
        if not self.enabled:
            return False
        return always or duration_s >= self.settings.telegram_min_duration

    def notify(
        self, summary: ExecutionSummary, *, always: bool = False
    ) -> threading.Thread | None:
        """Announce *summary* if it clears the bar, in the background.

        Args:
            summary: What to say.
            always: Send regardless of the configured duration threshold. Set
                by background jobs, where the act of backgrounding something is
                itself the request to be told when it lands -- a caller who
                took the trouble to hand a job off should not then have to
                guess whether it was long enough to hear about.

        Returns the thread doing the sending, so a caller that needs the message
        out before it returns -- tests, a shutting-down server -- can join it.
        ``None`` means nothing was sent, either because notifications are off,
        because the run was too short, or because delivery was already tried and
        failed on a thread that has since finished.
        """
        if not self.should_notify(summary.duration_s, always=always):
            return None

        thread = threading.Thread(
            target=self._deliver,
            args=(summary,),
            name="pyexec-telegram",
            daemon=True,
        )
        thread.start()
        return thread

    def _deliver(self, summary: ExecutionSummary) -> None:
        """Send, and let nothing escape into the thread that runs it."""
        try:
            self.send(format_message(summary))
        except Exception:  # noqa: BLE001 - a convenience must not kill the server
            logger.exception("Could not deliver the Telegram notification")

    def send(self, text: str) -> None:
        """Post *text* to the configured chat, blocking until it lands.

        Raises:
            NotificationError: if notifications are not configured, the API is
                unreachable, or Telegram refused the message. The reason is
                Telegram's own when it gave one, since "chat not found" and
                "bot was blocked" are fixed in different places.
        """
        if not self.enabled:
            raise NotificationError(
                "Telegram notifications are not configured. Set "
                "PYEXEC_TELEGRAM_BOT_TOKEN and PYEXEC_TELEGRAM_CHAT_ID and "
                "restart the server."
            )

        body = urllib.parse.urlencode(
            {
                "chat_id": self.settings.telegram_chat_id,
                "text": text,
                "disable_web_page_preview": "true",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            api_url(self.settings.telegram_bot_token or ""),
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )

        try:
            with self._opener().open(request, timeout=SEND_TIMEOUT) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise NotificationError(
                f"Telegram rejected the message (HTTP {exc.code}): "
                f"{_error_detail(exc)}"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise NotificationError(
                f"Could not reach the Telegram API: {exc}. A network that "
                "cannot route to api.telegram.org needs PYEXEC_TELEGRAM_PROXY "
                "pointing at a proxy that can."
            ) from exc

        # A 200 does not mean delivered: the Bot API answers 200 with
        # {"ok": false, ...} for a chat id the bot cannot post to.
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            raise NotificationError(
                f"Telegram sent a response that is not JSON: {raw[:200]!r}"
            ) from exc
        if not payload.get("ok"):
            raise NotificationError(
                "Telegram refused the message: "
                f"{payload.get('description') or payload!r}"
            )

    def _opener(self) -> urllib.request.OpenerDirector:
        """The opener used to reach the Bot API.

        With no ``PYEXEC_TELEGRAM_PROXY`` this is the stock opener, which honours
        ``HTTPS_PROXY`` from the environment -- usually the right answer, since a
        machine that needs a proxy to reach Telegram already has one configured.
        Setting the variable to the empty string forces a direct connection for
        the case where that environment proxy is meant for other traffic only.
        """
        proxy = self.settings.telegram_proxy
        if proxy is None:
            return urllib.request.build_opener()
        if not proxy:
            return urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )


def _error_detail(exc: urllib.error.HTTPError) -> str:
    """Telegram's own explanation of a rejection, when it sent one."""
    try:
        payload = json.loads(exc.read())
    except (ValueError, OSError):
        return exc.reason if isinstance(exc.reason, str) else "no detail given"
    return str(payload.get("description") or payload)


#: Cached notifier, paired with the settings object it was built from. Settings
#: are resolved once and swapped wholesale (see ``server.use_settings``), so
#: identity is the right test -- a new configuration gets a new notifier.
_CACHED: tuple[Settings, TelegramNotifier] | None = None


def notifier(settings: Settings) -> TelegramNotifier:
    """The notifier for *settings*."""
    global _CACHED
    if _CACHED is None or _CACHED[0] is not settings:
        _CACHED = (settings, TelegramNotifier(settings))
    return _CACHED[1]


def notify(
    settings: Settings, summary: ExecutionSummary, *, always: bool = False
) -> threading.Thread | None:
    """Announce *summary* under *settings*, if it is worth announcing.

    The one call the execution tools make; see
    :meth:`TelegramNotifier.notify` for what the return value and *always* mean.
    """
    return notifier(settings).notify(summary, always=always)


__all__ = [
    "API_ROOT",
    "CLIP_NOTE",
    "EXCERPT_CHARS",
    "MAX_MESSAGE_CHARS",
    "SEND_TIMEOUT",
    "ExecutionSummary",
    "NotificationError",
    "TelegramNotifier",
    "api_url",
    "format_message",
    "machine_name",
    "notifier",
    "notify",
]
