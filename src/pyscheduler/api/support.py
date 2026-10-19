"""Shared helpers for the API routes: errors, parameters, bodies and the caller.

Every handler is wrapped by ``guarded``, the API's safety net. Whatever
goes wrong inside, the caller gets a small JSON answer:

- ``ApiError`` (a bad parameter, an unknown run) -> its status;
- ``ActionError`` from the engine -> its status (400, 404, 409 or 503);
- pyweb's ``HTTPError`` (a body that is not JSON) -> its status;
- anything else -> 500 ``{"error": "internal error"}``, with the
  traceback in the log and never in the answer.
"""

import functools
import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import date

from pynet.messaging import JsonValue
from pyweb.messages import HTTPError, HttpRequest, HttpResponse, json

from pyscheduler.api.auth import Principal, Role
from pyscheduler.engine.action_support import ActionError
from pyscheduler.values import check_name, parse_date

__all__ = [
    "ApiError",
    "Handler",
    "actor_of",
    "body_date",
    "guarded",
    "json_body",
    "not_found",
    "query_date",
    "query_int",
    "query_name",
    "role_of",
]

logger = logging.getLogger("pyscheduler.api")

type JsonObject = dict[str, JsonValue]
type Handler = Callable[..., Awaitable[JsonObject]]

BAD_REQUEST = 400
UNAUTHORISED = 401
NOT_FOUND = 404
INTERNAL_ERROR = 500
UNAVAILABLE = 503
FAILED_PREFIX = "scheduler failed"
FAILED_MESSAGE = "scheduler failed; see the scheduler log"
"""What callers see when the engine has failed stop (the cause stays in the log)."""


class ApiError(Exception):
    """Raised by a handler to answer ``{"error": message, ...extra}`` with ``status``."""

    def __init__(self, status: int, message: str, **extra: JsonValue) -> None:
        """Remember the status, the message and any extra fields of the answer."""
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra

    def response(self) -> HttpResponse:
        """Build the JSON answer."""
        return json({"error": self.message, **self.extra}, self.status)


def guarded(handler: Handler) -> Callable[..., Awaitable[HttpResponse]]:
    """Wrap an async handler so it always answers JSON, even when it fails."""

    @functools.wraps(handler)
    async def call(request: HttpRequest, **params: object) -> HttpResponse:
        """Run the handler and turn its result or its error into a response."""
        try:
            return json(await handler(request, **params))
        except ApiError as exc:
            return exc.response()
        except ActionError as exc:
            message = str(exc)
            if exc.status == UNAVAILABLE and message.startswith(FAILED_PREFIX):
                logger.warning("refused %s %s: %s", request.method, request.path, message)
                message = FAILED_MESSAGE
            return json({"error": message}, exc.status)
        except HTTPError as exc:
            return json({"error": exc.message or str(exc)}, exc.status)
        except Exception:
            logger.exception("API error in %s %s", request.method, request.path)
            return json({"error": "internal error"}, INTERNAL_ERROR)

    return call


def _principal(request: HttpRequest) -> Principal:
    """Return the caller the access layer vouched for.

    Raises:
        ApiError: 401 ``unauthorised`` if ``request.user`` is not a
            ``Principal`` (fail closed: never guess a caller).

    """
    user = request.user
    if not isinstance(user, Principal):
        raise ApiError(UNAUTHORISED, "unauthorised")
    return user


def role_of(request: HttpRequest) -> Role:
    """Return the caller's role (set by the access layer).

    Raises:
        ApiError: 401 if the access layer set no ``Principal``.

    """
    return _principal(request).role


def actor_of(request: HttpRequest) -> str:
    """Return the caller's name (a token's or a user's; set by the access layer).

    Raises:
        ApiError: 401 if the access layer set no ``Principal``.

    """
    return _principal(request).name


def query_date(request: HttpRequest, name: str) -> date | None:
    """Return the ``YYYY-MM-DD`` query parameter ``name``, or ``None`` when absent.

    Raises:
        ApiError: 400 if it is not a date.

    """
    text = request.query.get(name)
    if text is None:
        return None
    try:
        return parse_date(text)
    except ValueError as exc:
        raise ApiError(BAD_REQUEST, f"{name}: {exc}") from None


def query_int(request: HttpRequest, name: str, *, default: int, low: int, high: int) -> int:
    """Return the whole-number query parameter ``name`` (``default`` when absent).

    Raises:
        ApiError: 400 if it is not a number from ``low`` to ``high``.

    """
    text = request.query.get(name)
    if text is None:
        return default
    digits = text.isascii() and text.isdigit() and len(text) <= len(str(high))
    if not digits or not low <= int(text) <= high:
        raise ApiError(BAD_REQUEST, f"{name} must be a whole number from {low} to {high}")
    return int(text)


def query_name(request: HttpRequest, name: str) -> str | None:
    """Return the job-name query parameter ``name``, or ``None`` when absent.

    Raises:
        ApiError: 400 if it is not a valid name.

    """
    text = request.query.get(name)
    if text is None:
        return None
    try:
        check_name(text)
    except ValueError as exc:
        raise ApiError(BAD_REQUEST, f"{name}: {exc}") from None
    return text


def json_body(request: HttpRequest, allowed: tuple[str, ...]) -> Mapping[str, JsonValue]:
    """Return the request's JSON object body (``{}`` when there is no body).

    Raises:
        ApiError: 400 if the body is not a JSON object or has a field not
            in ``allowed``.
        HTTPError: 400 if the body is not valid JSON.

    """
    if not request.body.strip():
        return {}
    value = request.json()
    if not isinstance(value, dict):
        raise ApiError(BAD_REQUEST, "the body must be a JSON object")
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ApiError(BAD_REQUEST, f"unknown field {unknown[0]!r}")
    return value


def body_date(body: Mapping[str, JsonValue], name: str) -> date | None:
    """Return the ``YYYY-MM-DD`` field ``name`` of a body, or ``None`` when absent.

    Raises:
        ApiError: 400 if it is not a date string.

    """
    value = body.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError(BAD_REQUEST, f"{name} must be a date string")
    try:
        return parse_date(value)
    except ValueError as exc:
        raise ApiError(BAD_REQUEST, f"{name}: {exc}") from None


def not_found(message: str) -> ApiError:
    """Return the 404 error for ``message``."""
    return ApiError(NOT_FOUND, message)
