"""The log letters: asking an agent for part of a job's log, and its answer.

The scheduler cannot open files on an agent's disk, so it asks, like a
librarian phoning another branch: "read me the last 200 lines of this
book" (``tail``) or "send me the pages from here on" (``read``). The
answer carries the lines, or the raw bytes in base64, or says the book is
not there (``missing``).
"""

import base64
import binascii
from collections.abc import Mapping
from dataclasses import dataclass

from pynet.messaging import JsonValue

from pyscheduler.agents.messages import MALFORMED_PAYLOAD, count_of, key_payload, parse_key, text_of
from pyscheduler.agents.signing import Rejected

__all__ = [
    "LOG_MODES",
    "LOG_STREAMS",
    "MAX_READ_BYTES",
    "MAX_TAIL_LINES",
    "LogReply",
    "LogRequest",
    "log_reply_payload",
    "log_request_payload",
    "parse_log_reply",
    "parse_log_request",
]

LOG_STREAMS = ("stdout", "stderr")
LOG_MODES = ("tail", "read")
MAX_TAIL_LINES = 5000
"""The most lines a ``tail`` log request may ask for."""
MAX_READ_BYTES = 1024 * 1024
"""The most bytes a ``read`` log request may ask for (a reply must fit in one frame)."""


def _flag(payload: Mapping[str, JsonValue], key: str) -> bool:
    """Return the boolean ``key`` (missing is false) or raise ``Rejected``."""
    value = payload.get(key, False)
    if not isinstance(value, bool):
        raise Rejected(MALFORMED_PAYLOAD)
    return value


@dataclass(frozen=True)
class LogRequest:
    """A request for part of an attempt's log (P4-R9).

    ``mode`` ``tail`` wants the last ``count`` lines; ``read`` wants up to
    ``limit`` bytes from ``offset``.
    """

    request_id: str
    run_id: str
    attempt: int
    stream: str
    mode: str
    count: int = 0
    offset: int = 0
    limit: int = 0


def log_request_payload(request: LogRequest) -> dict[str, JsonValue]:
    """Return the payload of a ``log_request``."""
    payload: dict[str, JsonValue] = {
        "request_id": request.request_id,
        **key_payload(request.run_id, request.attempt),
        "stream": request.stream,
        "mode": request.mode,
    }
    if request.mode == "tail":
        payload["count"] = request.count
    else:
        payload |= {"offset": request.offset, "limit": request.limit}
    return payload


def parse_log_request(payload: Mapping[str, JsonValue]) -> LogRequest:
    """Return the ``LogRequest`` in ``payload`` or raise ``Rejected``; sizes are capped."""
    run_id, attempt = parse_key(payload)
    request_id, stream, mode = (text_of(payload, k) for k in ("request_id", "stream", "mode"))
    if stream not in LOG_STREAMS or mode not in LOG_MODES:
        raise Rejected(MALFORMED_PAYLOAD)
    if mode == "tail":
        count = min(count_of(payload, "count", low=1), MAX_TAIL_LINES)
        return LogRequest(request_id, run_id, attempt, stream, mode, count=count)
    offset = count_of(payload, "offset")
    limit = min(count_of(payload, "limit", low=1), MAX_READ_BYTES)
    return LogRequest(request_id, run_id, attempt, stream, mode, offset=offset, limit=limit)


@dataclass(frozen=True)
class LogReply:
    """The answer to a ``LogRequest``.

    Attributes:
        request_id: The request this answers.
        lines: For ``tail``: the lines found.
        truncated: For ``tail``: whether earlier text was left out.
        data: For ``read``: the bytes found.
        offset: For ``read``: the offset just after ``data``.
        shrank: For ``read``: the file was shorter than the offset (rotated) and was read from 0.
        missing: There is no such log (or attempt) on the agent.

    """

    request_id: str
    lines: list[str] | None = None
    truncated: bool = False
    data: bytes | None = None
    offset: int = 0
    shrank: bool = False
    missing: bool = False


def log_reply_payload(reply: LogReply) -> dict[str, JsonValue]:
    """Return the payload of a ``log_reply``; bytes travel as base64."""
    payload: dict[str, JsonValue] = {"request_id": reply.request_id, "missing": reply.missing}
    if reply.lines is not None:
        payload |= {"lines": list(reply.lines), "truncated": reply.truncated}
    if reply.data is not None:
        payload |= {
            "data_b64": base64.b64encode(reply.data).decode("ascii"),
            "offset": reply.offset,
            "shrank": reply.shrank,
        }
    return payload


def parse_log_reply(payload: Mapping[str, JsonValue]) -> LogReply:
    """Return the ``LogReply`` in ``payload`` or raise ``Rejected``."""
    request_id = text_of(payload, "request_id")
    lines, data = payload.get("lines"), None
    if lines is not None and (
        not isinstance(lines, list) or not all(isinstance(x, str) for x in lines)
    ):
        raise Rejected(MALFORMED_PAYLOAD)
    if "data_b64" in payload:
        try:
            data = base64.b64decode(text_of(payload, "data_b64"), validate=True)
        except binascii.Error:
            raise Rejected(MALFORMED_PAYLOAD) from None
    return LogReply(
        request_id,
        lines=None if lines is None else [str(x) for x in lines],
        truncated=_flag(payload, "truncated"),
        data=data,
        offset=count_of(payload, "offset") if data is not None else 0,
        shrank=_flag(payload, "shrank"),
        missing=_flag(payload, "missing"),
    )
