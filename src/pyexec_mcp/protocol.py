"""Line framing shared by the session manager and the execution worker.

One request or response is one newline-terminated line of JSON. Responses carry
a magic prefix salted with a per-session nonce, so code running inside the
worker cannot print a line that the parent would mistake for a real response.
"""

from __future__ import annotations

import json
from typing import Any

FRAME_PREFIX = "@@PYEXEC@@"


def frame_line(nonce: str, payload: dict[str, Any]) -> str:
    """Encode one response frame as a newline-terminated line.

    Serialised with ``ensure_ascii=True`` on purpose: the result is pure ASCII
    and therefore survives any console encoding the host happens to use.
    """
    return f"{FRAME_PREFIX}{nonce}@@{json.dumps(payload)}\n"


def split_frame(nonce: str, line: bytes) -> tuple[bytes, dict[str, Any] | None]:
    """Split one stdout line into ``(leading_raw_output, frame_payload)``.

    The magic prefix is located anywhere in the line rather than only at the
    start, so program output that was written without a trailing newline is
    preserved as raw output instead of corrupting the frame. A ``None`` payload
    means the line carried no frame at all.
    """
    prefix = f"{FRAME_PREFIX}{nonce}@@".encode("utf-8")
    index = line.find(prefix)
    if index == -1:
        return line, None

    try:
        payload = json.loads(line[index + len(prefix) :].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return line, None

    return line[:index], payload
