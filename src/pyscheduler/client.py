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

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, cast

from pynet.messaging import JsonValue

__all__ = ["DEFAULT_TIMEOUT", "DEFAULT_URL", "ApiClient", "ApiError"]

DEFAULT_URL = "http://127.0.0.1:8470"
"""Where the client looks for the scheduler when no URL is given."""

DEFAULT_TIMEOUT = 30.0
"""How many seconds a call may take before it gives up."""

type JsonObject = dict[str, Any]

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
        status: The HTTP status (0 when the scheduler could not be reached).
        message: The ``error`` text of the answer.
        problems: Config problems, for a reload or validate that failed.

    """

    def __init__(self, status: int, message: str, problems: list[JsonObject] | None = None) -> None:
        """Remember the status, the message and any problems."""
        super().__init__(message if status == 0 else f"{status}: {message}")
        self.status = status
        self.message = message
        self.problems = problems or []


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
                ``problems``), or status 0 if the scheduler cannot be reached.

        """
        target = self.url + path
        params = {k: str(v) for k, v in (query or {}).items() if v is not None}
        if params:
            target += "?" + urllib.parse.urlencode(params)
        if not target.startswith(("http://", "https://")):
            raise ApiError(0, f"the scheduler URL must start with http:// or https://: {self.url}")
        headers = {"Accept": "application/json"}
        data = None
        if body is not None or method != "GET":
            data = json.dumps(body or {}).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(target, data=data, headers=headers, method=method)  # noqa: S310 -- checked above
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                return _decode(response.read())
        except urllib.error.HTTPError as exc:
            with exc:
                answer = _decode(exc.read(), strict=False)
            message = str(answer.get("error") or exc.reason or "error")
            problems = answer.get("problems")
            listed = cast("list[JsonObject]", problems) if isinstance(problems, list) else None
            raise ApiError(exc.code, message, listed) from None
        except (urllib.error.URLError, OSError) as exc:
            del exc
            raise ApiError(0, f"cannot reach scheduler at {self.url}") from None

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
        """Return ``{"run", "attempts", "events"}`` for one run."""
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
        raise ApiError(0, "the scheduler did not answer with a JSON object")
    return {}
