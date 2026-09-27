"""A minimal ``.env`` reader.

Every knob this server has is an environment variable, so a ``.env`` sitting
next to the project is a natural place to keep them -- but Python does not read
one on its own, and a token that looks configured while the process sees
nothing is exactly the kind of silent gap that ends in a confusing startup
failure.

Deliberately hand-rolled instead of depending on ``python-dotenv``: the format
we need is a flat list of ``KEY=VALUE`` lines, and keeping it in-tree means a
fresh install has one less moving part. The supported subset is documented in
:func:`parse`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

#: Consulted when no path is given; also settable via ``PYEXEC_ENV_FILE``.
DEFAULT_ENV_FILENAME = ".env"

_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ESCAPE_RE = re.compile(r"\\(.)")
_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "'": "'"}


def _unquote(value: str) -> str:
    """Strip a matching pair of surrounding quotes and resolve escapes.

    Only double-quoted values get escape processing, matching common ``.env``
    behaviour; single quotes are taken verbatim so a Windows path or a regex
    survives intact.
    """
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        inner = value[1:-1]
        if value[0] == '"':
            # One regex pass rather than chained replaces, so that ``\\n``
            # (escaped backslash, then "n") does not collapse into a newline.
            return _ESCAPE_RE.sub(lambda m: _ESCAPES.get(m.group(1), m.group(1)), inner)
        return inner
    return value


def parse(text: str) -> dict[str, str]:
    """Parse ``.env`` content into a mapping.

    Understood: blank lines, ``#`` comments, an optional ``export`` prefix,
    surrounding single or double quotes, a trailing `` #comment`` on an
    unquoted value, and backslash escapes inside double quotes.

    Ignored: anything that is not ``KEY=VALUE`` with a conventional name.
    """
    result: dict[str, str] = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()

        key, separator, value = line.partition("=")
        if not separator:
            continue

        key = key.strip()
        if not _KEY_RE.fullmatch(key):
            continue

        value = value.strip()
        if value[:1] in ("'", '"'):
            # Quoted: ``#`` is data, not the start of a comment.
            result[key] = _unquote(value)
        else:
            # Unquoted: a ``#`` introduced by whitespace starts a comment.
            result[key] = re.split(r"\s#", value, maxsplit=1)[0].rstrip()

    return result


def load(
    path: str | os.PathLike[str],
    *,
    override: bool = False,
    required: bool = False,
) -> list[str]:
    """Merge *path* into ``os.environ``; return the names that were applied.

    Only names are returned, never values, so the result is safe to log.

    ``override`` defaults to False so that the real environment -- and the CLI
    flags layered on top of it -- win over the file. A ``.env`` that silently
    overrode an explicitly exported token would be a debugging nightmare.

    Raises:
        ValueError: if *required* and the file is missing, unreadable, or
            malformed. A silently ignored ``--env-file`` is worse than a crash.
    """
    path = Path(path)

    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        if required:
            raise ValueError(f"env file {str(path)!r} does not exist") from None
        return []
    except OSError as exc:
        raise ValueError(f"env file {str(path)!r} could not be read: {exc}") from exc

    applied: list[str] = []
    for key, value in parse(text).items():
        if not override and key in os.environ:
            continue
        os.environ[key] = value
        applied.append(key)
    return applied
