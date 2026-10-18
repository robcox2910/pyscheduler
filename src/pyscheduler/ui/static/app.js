// Shared helpers for the pyscheduler web UI. Server text goes into the page
// with textContent only, never innerHTML.

const CONFIRM_MS = 4000;

/** Return the CSRF token the server put in the page (empty in development mode). */
export function csrfToken() {
  const tag = document.querySelector('meta[name="csrf-token"]');
  return tag ? tag.content : "";
}

/**
 * POST `body` as JSON to `url`, sending the CSRF token when there is one.
 * Resolves to `{ok, status, data}`; `data` is the parsed JSON (or null).
 */
export async function post(url, body = {}) {
  const headers = { "Content-Type": "application/json" };
  const token = csrfToken();
  if (token) {
    headers["X-CSRF-Token"] = token;
  }
  const response = await fetch(url, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
    credentials: "same-origin",
  });
  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }
  return { ok: response.ok, status: response.status, data };
}

/**
 * Ask "sure?" on a button: the first call arms it and resolves false; a
 * second call within a few seconds resolves true. The label is restored
 * if nobody confirms.
 */
export function confirmThen(button) {
  if (button.dataset.armed === "1") {
    clearTimeout(Number(button.dataset.timer));
    disarm(button);
    return Promise.resolve(true);
  }
  button.dataset.armed = "1";
  button.dataset.label = button.textContent;
  button.textContent = "Sure?";
  button.dataset.timer = String(setTimeout(() => disarm(button), CONFIRM_MS));
  return Promise.resolve(false);
}

function disarm(button) {
  button.dataset.armed = "";
  button.textContent = button.dataset.label || button.textContent;
}

let liveSource = null;

/**
 * Call `handler(data)` for each live-feed event of type `kind` ("run",
 * "attempt", "reload"). The first call opens one shared EventSource on
 * `/events`, resuming after the id the page was drawn at (`data-live-after`
 * on `<body>`), so nothing that happened while the page loaded is missed.
 * On a reconnect the browser sends Last-Event-ID itself. A `reset` (the
 * feed cannot fill the gap) reloads the page. Returns false when the page
 * has no live feed.
 */
export function onLive(kind, handler) {
  const after = document.body.dataset.liveAfter;
  if (!after) {
    return false;
  }
  if (liveSource === null) {
    liveSource = new EventSource(`/events?after=${encodeURIComponent(after)}`);
    liveSource.addEventListener("reset", () => window.location.reload());
  }
  liveSource.addEventListener(kind, (event) => handler(JSON.parse(event.data)));
  return true;
}
