// Shared helpers for the pyscheduler web UI: the CSRF token, JSON posts,
// "sure?" buttons, the one live feed, fragments and what to do when the
// session is gone. Server text goes into the page with textContent only.
// The one exception is HTML fragments from our own /ui/fragments/...
// routes (drawn and escaped by the server), parsed in a <template> and
// swapped in whole. Event data is never put into HTML: it only says which
// fragment to fetch.

const CONFIRM_MS = 4000;
const GONE_RELOAD_MS = 1500;

/** The line shown when the session has expired (or the user was removed). */
export const SESSION_EXPIRED = "Your session has expired — please sign in again";

/** The line shown when the live feed has stopped for good. */
export const LIVE_STOPPED = "Live updates stopped — reloading…";

/** Return the CSRF token the server put in the page (empty in development mode). */
export function csrfToken() {
  const tag = document.querySelector('meta[name="csrf-token"]');
  return tag ? tag.content : "";
}

/** Return whether an answer's status means the session is gone (401) or refused (403). */
export function isGone(status) {
  return status === 401 || status === 403;
}

let leaving = false;

/**
 * Show `message` at the top of the page and reload it soon. A reload is a
 * page load (Accept: text/html), so an expired session lands on
 * /login?next=..., and a live session simply redraws. Only the first call
 * counts.
 */
export function reloadBecause(message) {
  if (leaving) {
    return;
  }
  leaving = true;
  let line = document.getElementById("session-notice");
  if (!line) {
    line = document.createElement("p");
    line.id = "session-notice";
    line.className = "session-notice";
    line.setAttribute("role", "alert");
    const main = document.querySelector("main") || document.body;
    main.prepend(line);
  }
  line.textContent = message;
  setTimeout(() => window.location.reload(), GONE_RELOAD_MS);
}

/**
 * POST `body` as JSON to `url`, sending the CSRF token when there is one.
 * Resolves to `{ok, status, data}`; `data` is the parsed JSON (or null).
 * A 401 (the session expired) says so and reloads the page.
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
  if (response.status === 401) {
    reloadBecause(SESSION_EXPIRED);
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

/** Show `text` in the page's message line (hidden when empty). */
export function say(text) {
  const line = document.getElementById("board-message");
  if (line) {
    line.textContent = text;
    line.hidden = !text;
  }
}

/**
 * Fetch a fragment of our own server-drawn (and escaped) HTML and return
 * `{status, element}`: its first element matching `selector`, or null
 * (404, or anything else unexpected). A 401 or 403 (or a redirect to the
 * login page) means the session is gone: the page says so and reloads.
 * Throws on a network error.
 */
export async function fragment(url, selector) {
  const response = await fetch(url, { credentials: "same-origin" });
  if (isGone(response.status) || response.redirected) {
    reloadBecause(SESSION_EXPIRED);
    return { status: response.status, element: null };
  }
  if (!response.ok) {
    return { status: response.status, element: null };
  }
  const holder = document.createElement("template");
  holder.innerHTML = await response.text();
  return { status: response.status, element: holder.content.querySelector(selector) };
}

let liveSource = null;

/**
 * Call `handler(data)` for each live-feed event of type `kind` ("run",
 * "attempt", "reload"). The first call opens one shared EventSource on
 * `/events`, resuming after the id the page was drawn at (`data-live-after`
 * on `<body>`), so nothing that happened while the page loaded is missed.
 * On a reconnect the browser sends Last-Event-ID itself. A `reset` (the
 * feed cannot fill the gap) reloads the page. If the browser gives up on
 * the feed for good (a reconnect was answered 401 because the session
 * expired, or anything else but the stream), the page says so and reloads,
 * rather than looking live while frozen. Returns false when the page has
 * no live feed.
 */
export function onLive(kind, handler) {
  const after = document.body.dataset.liveAfter;
  if (!after) {
    return false;
  }
  if (liveSource === null) {
    const source = new EventSource(`/events?after=${encodeURIComponent(after)}`);
    source.addEventListener("reset", () => window.location.reload());
    source.addEventListener("error", () => {
      if (source.readyState === EventSource.CLOSED) {
        reloadBecause(LIVE_STOPPED);
      }
    });
    liveSource = source;
  }
  liveSource.addEventListener(kind, (event) => handler(JSON.parse(event.data)));
  return true;
}
