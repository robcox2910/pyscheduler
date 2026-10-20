"""A small HTTP client for the scheduler's JSON API.

The API is the scheduler's front desk; ``ApiClient`` is the phone you
ring it with. Each method is one call to one endpoint. It speaks plain
JSON over stdlib ``urllib``, so it needs no event loop: a script, the
``pysched`` CLI or a test thread can all use it.

Example::

    client = ApiClient("http://127.0.0.1:8470", token=None)
    run = client.trigger("extract")["run"]
    print(client.run(run["run_id"])["run"]["state"])
"""

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, cast

from pynet.messaging import JsonValue

__all__ = [
    "BAD_ANSWER",
    "DEFAULT_TIMEOUT",
    "DEFAULT_URL",
    "RUN_ACTIONS",
    "UNREACHABLE",
    "ApiClient",
    "ApiError",
    "safe_url",
]

DEFAULT_URL = "http://127.0.0.1:8470"
"""Where the client looks for the scheduler when no URL is given."""

DEFAULT_TIMEOUT = 30.0
"""How many seconds a call may take before it gives up."""

type JsonObject = dict[str, Any]

UNREACHABLE = 0
"""The ``ApiError`` status when the scheduler cannot be reached (or did not answer in time)."""

BAD_ANSWER = -1
"""The ``ApiError`` status when the answer is not HTTP with a JSON object."""

REDIRECT_LOW = 300
REDIRECT_HIGH = 400

RUN_ACTIONS = (
    "rerun",
    "hold",
    "release",
    "kill",
    "force-start",
    "skip",
    "mark-success",
    "mark-failed",
)
"""The actions ``run_action`` accepts, as the API names them."""


class ApiError(Exception):
    """Raised when the scheduler answers with an error, or cannot be reached.

    Attributes:
        status: The HTTP status; ``UNREACHABLE`` (0) when the scheduler could
            not be reached or did not answer in time, ``BAD_ANSWER`` (-1) when
            its answer made no sense.
        message: The ``error`` text of the answer.
        problems: Config problems, for a reload or validate that failed.

    """

    def __init__(self, status: int, message: str, problems: list[JsonObject] | None = None) -> None:
        """Remember the status, the message and any problems."""
        super().__init__(message if status <= UNREACHABLE else f"{status}: {message}")
        self.status = status
        self.message = message
        self.problems = problems or []


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect, so the bearer token is never sent anywhere else."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        """Refuse every redirect (the 3xx answer then becomes an ``HTTPError``)."""
        del args, kwargs


_OPENER = urllib.request.build_opener(_NoRedirects)
"""The opener every call uses: urllib's usual handlers, minus redirects."""


def safe_url(url: str) -> str:
    """Return ``url`` without any ``user:password@`` part (for messages)."""
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.rpartition("@")[2]
    return urllib.parse.urlunsplit(parts._replace(netloc=host))


def _http_error(exc: urllib.error.HTTPError) -> ApiError:
    """Turn an error status (or a refused redirect) into an ``ApiError``."""
    try:
        with exc:
            answer = _decode(exc.read(), strict=False)
    except OSError, http.client.HTTPException:
        answer = {}
    if REDIRECT_LOW <= exc.code < REDIRECT_HIGH:
        target = safe_url(exc.headers.get("Location") or "elsewhere")
        msg = f"the scheduler redirected to {target}; not followed (the token stays here)"
        return ApiError(exc.code, msg)
    message = str(answer.get("error") or exc.reason or "error")
    problems = answer.get("problems")
    listed = cast("list[JsonObject]", problems) if isinstance(problems, list) else None
    return ApiError(exc.code, message, listed)


def _quote(segment: str) -> str:
    """Return ``segment`` escaped as one URL path segment (``/`` included)."""
    return urllib.parse.quote(segment, safe="")


class ApiClient:
    """Call the scheduler's API at ``url``, with a bearer ``token`` if given."""

    def __init__(self, url: str, token: str | None, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        """Remember where the scheduler is and how to prove who we are."""
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    # -- plumbing --------------------------------------------------------

    def call(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str | int | None] | None = None,
        body: dict[str, JsonValue] | None = None,
    ) -> JsonObject:
        """Send one request and return the JSON object it answers with.

        Raises:
            ApiError: For an error status (with the answer's ``error`` and
                ``problems``), a refused redirect (its 3xx status),
                ``UNREACHABLE`` if the scheduler cannot be reached or times
                out, or ``BAD_ANSWER`` if the answer is not a JSON object.

        """
        target = self.url + path
        params = {k: str(v) for k, v in (query or {}).items() if v is not None}
        if params:
            target += "?" + urllib.parse.urlencode(params)
        if not target.startswith(("http://", "https://")):
            msg = f"the scheduler URL must start with http:// or https://: {safe_url(self.url)}"
            raise ApiError(UNREACHABLE, msg)
        headers = {"Accept": "application/json"}
        data = None
        if body is not None or method != "GET":
            data = json.dumps(body or {}).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(target, data=data, headers=headers, method=method)  # noqa: S310 -- checked above
        where = safe_url(self.url)
        try:
            with _OPENER.open(request, timeout=self.timeout) as response:
                return _decode(response.read())
        except urllib.error.HTTPError as exc:
            raise _http_error(exc) from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise self._timed_out(where) from None
            raise ApiError(UNREACHABLE, f"cannot reach scheduler at {where}") from None
        except TimeoutError:
            raise self._timed_out(where) from None
        except http.client.HTTPException as exc:
            msg = f"bad answer from scheduler at {where}: {type(exc).__name__}"
            raise ApiError(BAD_ANSWER, msg) from None
        except OSError:
            raise ApiError(UNREACHABLE, f"cannot reach scheduler at {where}") from None

    def _timed_out(self, where: str) -> ApiError:
        """Return the error for a call that took longer than ``timeout``."""
        return ApiError(
            UNREACHABLE, f"scheduler at {where} did not answer within {self.timeout:g}s"
        )

    # -- reads -----------------------------------------------------------

    def health(self) -> JsonObject:
        """Return ``{"status": "ok"}`` (or raise 503 if the engine failed)."""
        return self.call("GET", "/api/health")

    def jobs(self) -> JsonObject:
        """Return ``{"jobs": [...]}``: every job's summary."""
        return self.call("GET", "/api/jobs")

    def job(self, name: str) -> JsonObject:
        """Return one job's summary, definition, next firings and recent runs."""
        return self.call("GET", f"/api/jobs/{_quote(name)}")

    def runs(
        self,
        *,
        run_date: str | None = None,
        job: str | None = None,
        state: str | None = None,
        limit: int | None = None,
    ) -> JsonObject:
        """Return ``{"runs": [...]}``, filtered by run date, job and state."""
        query: dict[str, str | int | None] = {
            "run_date": run_date,
            "job": job,
            "state": state,
            "limit": limit,
        }
        return self.call("GET", "/api/runs", query=query)

    def run(self, run_id: str) -> JsonObject:
        """Return ``{"run", "attempts", "events", "alerts"}`` for one run."""
        return self.call("GET", f"/api/runs/{_quote(run_id)}")

    def log(
        self,
        run_id: str,
        *,
        stream: str = "stdout",
        tail: int | None = None,
        attempt: int | None = None,
    ) -> JsonObject:
        """Return ``{"lines", "path", "truncated"}``: the end of an attempt's output."""
        query: dict[str, str | int | None] = {"stream": stream, "tail": tail, "attempt": attempt}
        return self.call("GET", f"/api/runs/{_quote(run_id)}/log", query=query)

    def forecast(self, start: str | None = None, end: str | None = None) -> JsonObject:
        """Return ``{"entries": [...]}``: the predicted runs between two ISO instants."""
        return self.call("GET", "/api/forecast", query={"from": start, "to": end})

    def alerts(
        self,
        *,
        kind: str | None = None,
        job: str | None = None,
        since: str | None = None,
        limit: int | None = None,
    ) -> JsonObject:
        """Return ``{"alerts": [...]}``, newest first, each with its ``deliveries``."""
        query: dict[str, str | int | None] = {
            "kind": kind,
            "job": job,
            "since": since,
            "limit": limit,
        }
        return self.call("GET", "/api/alerts", query=query)

    def dead_alerts(self, limit: int | None = None) -> JsonObject:
        """Return ``{"deliveries": [...]}``: the dead deliveries, each with its ``alert``."""
        return self.call("GET", "/api/alerts/dead", query={"limit": limit})

    def agents(self) -> JsonObject:
        """Return ``{"agents": [...]}``: each agent's status, tags, load and last report."""
        return self.call("GET", "/api/agents")

    def validate(self) -> JsonObject:
        """Check the config files on the scheduler's disk; return ``{"ok", "warnings"}``."""
        return self.call("GET", "/api/config/validate")

    # -- actions ---------------------------------------------------------

    def trigger(self, job: str, *, run_date: str | None = None) -> JsonObject:
        """Create a manual run of ``job``; return ``{"run": ...}``."""
        body: dict[str, JsonValue] = {} if run_date is None else {"run_date": run_date}
        return self.call("POST", f"/api/jobs/{_quote(job)}/trigger", body=body)

    def backfill(self, job: str, *, start: str, end: str) -> JsonObject:
        """Create runs of ``job`` for every business day ``start`` .. ``end``."""
        return self.call(
            "POST", f"/api/jobs/{_quote(job)}/backfill", body={"from": start, "to": end}
        )

    def run_action(self, run_id: str, action: str, *, downstream: bool = False) -> JsonObject:
        """Run one operator action (see ``RUN_ACTIONS``) on a run.

        ``downstream`` only applies to ``rerun``.

        Raises:
            ValueError: If ``action`` is not one of ``RUN_ACTIONS``.

        """
        if action not in RUN_ACTIONS:
            msg = f"unknown run action {action!r}"
            raise ValueError(msg)
        body: dict[str, JsonValue] = {"downstream": True} if downstream else {}
        return self.call("POST", f"/api/runs/{_quote(run_id)}/{action}", body=body)

    def replay(self, alert_id: str, *, notifier: str | None = None) -> JsonObject:
        """Send an alert's dead deliveries again; return ``{"deliveries": [...]}``."""
        body: dict[str, JsonValue] = {} if notifier is None else {"notifier": notifier}
        return self.call("POST", f"/api/alerts/{_quote(alert_id)}/replay", body=body)

    def reload(self) -> JsonObject:
        """Ask the scheduler to reload its config files; return ``{"ok", "warnings"}``."""
        return self.call("POST", "/api/reload")


def _decode(raw: bytes, *, strict: bool = True) -> JsonObject:
    """Return the JSON object in ``raw``.

    With ``strict`` off, anything that is not a JSON object becomes ``{}``
    (an error page from a proxy, say).

    Raises:
        ApiError: In strict mode, if the answer is not a JSON object.

    """
    try:
        value: object = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except ValueError:
        value = None
    if isinstance(value, dict):
        return cast("JsonObject", value)
    if strict:
        raise ApiError(BAD_ANSWER, "the scheduler did not answer with a JSON object")
    return {}
