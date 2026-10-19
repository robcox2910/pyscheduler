// Shared helpers for the pyscheduler web UI, the live job board, the job and
// run pages and the live log viewer. Server
// text goes into the page with textContent only. The one exception is HTML
// fragments from our own /ui/fragments/... routes (drawn and escaped by the
// server), parsed in a <template> and swapped in whole. Event data is never
// put into HTML: it only says which fragment to fetch.

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

// -- action buttons --------------------------------------------------------

/**
 * Run a `data-action` button: confirm, then POST /api/runs/<data-run>/<action>.
 * A rerun also asks whether to rerun the downstream runs. The API's error
 * text is shown in `#board-message` (textContent only).
 */
async function runAction(button) {
  if (!(await confirmThen(button))) {
    return;
  }
  const action = button.dataset.action;
  const body = {};
  if (action === "rerun") {
    body.downstream = window.confirm("Also rerun the downstream runs?");
  }
  const url = `/api/runs/${encodeURIComponent(button.dataset.run)}/${encodeURIComponent(action)}`;
  button.disabled = true;
  let result;
  try {
    result = await post(url, body);
  } catch {
    result = { ok: false, status: 0, data: null };
  }
  button.disabled = false;
  if (result.ok) {
    say("");
    if (board) {
      refreshRow(button.dataset.run);
    } else {
      window.location.reload();
    }
  } else {
    const error = result.data && result.data.error;
    say(error ? String(error) : `${action} failed (${result.status || "network error"})`);
  }
}

/** Show `text` in the page's message line (hidden when empty). */
function say(text) {
  const line = document.getElementById("board-message");
  if (line) {
    line.textContent = text;
    line.hidden = !text;
  }
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action][data-run]");
  if (button) {
    event.preventDefault();
    runAction(button);
  }
});

// -- the live board --------------------------------------------------------

const board = document.getElementById("board");
const COUNTS_DELAY_MS = 250;
const rowFetches = new Map(); // run id -> number of the newest fetch for it
let fetchNumber = 0;
let countsTimer = null;

/** Return the board's query (its filters, with the shown date) for a fragment. */
function boardQuery() {
  const query = new URLSearchParams(window.location.search);
  query.set("date", board.dataset.date);
  return query.toString();
}

/**
 * Fetch a fragment of our own server-drawn (and escaped) HTML and return
 * its first element matching `selector`, or null (404, a redirect to the
 * login page, or anything else unexpected). Throws on a network error.
 */
async function fragment(url, selector) {
  const response = await fetch(url, { credentials: "same-origin" });
  if (!response.ok || response.redirected) {
    return { status: response.status, element: null };
  }
  const holder = document.createElement("template");
  holder.innerHTML = await response.text();
  return { status: response.status, element: holder.content.querySelector(selector) };
}

/** Return whether row `a` sorts before row `b` (as the server orders them). */
function before(a, b) {
  const keys = ["pin", "at", "job", "runId"];
  for (const key of keys) {
    if (a.dataset[key] !== b.dataset[key]) {
      return a.dataset[key] < b.dataset[key];
    }
  }
  return false;
}

/** Put `row` in its place in the table body, replacing its old copy. */
function place(row) {
  const body = board.tBodies[0];
  const old = body.querySelector(`tr[data-run-id="${CSS.escape(row.dataset.runId)}"]`);
  if (old) {
    old.remove();
  }
  const next = Array.from(body.rows).find((other) => before(row, other));
  body.insertBefore(row, next || null);
}

/** Show or hide the "no runs" line to match the table. */
function markEmpty() {
  const empty = document.getElementById("board-empty");
  if (empty) {
    empty.hidden = board.tBodies[0].rows.length > 0;
  }
}

/**
 * Fetch run `runId`'s row again and put it in place; remove the row when
 * the run is no longer on this board (404). Only the newest fetch for a
 * run is used, so a slow old answer never overwrites a newer one.
 */
async function refreshRow(runId) {
  fetchNumber += 1;
  const mine = fetchNumber;
  rowFetches.set(runId, mine);
  let found;
  try {
    found = await fragment(
      `/ui/fragments/board-row/${encodeURIComponent(runId)}?${boardQuery()}`,
      "tr[data-run-id]",
    );
  } catch {
    return;
  }
  if (rowFetches.get(runId) !== mine) {
    return;
  }
  rowFetches.delete(runId);
  if (found.element && found.element.dataset.runId === runId) {
    place(found.element);
  } else if (found.status === 404) {
    const old = board.tBodies[0].querySelector(`tr[data-run-id="${CSS.escape(runId)}"]`);
    if (old) {
      old.remove();
    }
  }
  markEmpty();
}

/** Fetch the counts bar again, soon (several changes in a burst cost one fetch). */
function refreshCounts() {
  if (countsTimer !== null) {
    return;
  }
  countsTimer = setTimeout(async () => {
    countsTimer = null;
    let found;
    try {
      found = await fragment(`/ui/fragments/board-counts?${boardQuery()}`, "#board-counts");
    } catch {
      return;
    }
    const old = document.getElementById("board-counts");
    if (found.element && old) {
      old.replaceWith(found.element);
    }
  }, COUNTS_DELAY_MS);
}

if (board) {
  // Registered at load, so no event after the page was drawn is missed.
  // Replays may repeat a change the page already shows: refetching is harmless.
  onLive("run", (data) => {
    if (data.run_date === board.dataset.date) {
      refreshRow(String(data.run_id));
      refreshCounts();
    }
  });
  onLive("attempt", (data) => {
    const runId = String(data.run_id);
    if (board.tBodies[0].querySelector(`tr[data-run-id="${CSS.escape(runId)}"]`)) {
      refreshRow(runId);
    }
  });
}

// -- job page: trigger and backfill ---------------------------------------

/** Return the value of the date input `id` ("" when empty or missing). */
function dateValue(id) {
  const input = document.getElementById(id);
  return input ? input.value : "";
}

/**
 * Run a `data-job-action` button: confirm, then POST the trigger (an
 * optional run date) or the backfill (`from` and `to`, both needed) to the
 * API. The API's error text is shown in `#board-message`.
 */
async function jobAction(button) {
  const box = button.closest("[data-job]");
  const kind = button.dataset.jobAction;
  const body = {};
  if (kind === "trigger") {
    const day = dateValue("trigger-date");
    if (day) {
      body.run_date = day;
    }
  } else {
    body.from = dateValue("backfill-from");
    body.to = dateValue("backfill-to");
    if (!body.from || !body.to) {
      say("Pick both a from and a to date to backfill.");
      return;
    }
  }
  if (!(await confirmThen(button))) {
    return;
  }
  button.disabled = true;
  let result;
  try {
    result = await post(`/api/jobs/${encodeURIComponent(box.dataset.job)}/${kind}`, body);
  } catch {
    result = { ok: false, status: 0, data: null };
  }
  button.disabled = false;
  if (result.ok) {
    window.location.reload();
  } else {
    const error = result.data && result.data.error;
    say(error ? String(error) : `${kind} failed (${result.status || "network error"})`);
  }
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-job-action]");
  if (button) {
    event.preventDefault();
    jobAction(button);
  }
});

// -- run page: live log and live refresh ----------------------------------

const logBox = document.getElementById("log");
const runPage = document.getElementById("run-page");
const RELOAD_MS = 500;
let reloadTimer = null;

/** Reload the run page soon (a burst of changes costs one reload). */
function reloadSoon() {
  if (reloadTimer === null) {
    reloadTimer = setTimeout(() => window.location.reload(), RELOAD_MS);
  }
}

if (runPage) {
  // Registered at load, so no event after the page was drawn is missed.
  onLive("run", (data) => {
    if (String(data.run_id) === runPage.dataset.runId) {
      reloadSoon();
    }
  });
  onLive("attempt", (data) => {
    if (
      String(data.run_id) === runPage.dataset.runId &&
      Number(data.attempt) > Number(runPage.dataset.attempts)
    ) {
      reloadSoon();
    }
  });
}

if (logBox) {
  const text = document.getElementById("log-text");
  const status = document.getElementById("log-status");
  const picker = document.getElementById("log-attempt");
  const tabs = Array.from(logBox.querySelectorAll("button[data-stream]"));
  let stream = "stdout";
  let source = null;
  let fresh = true; // the next `lines` replace the text (a (re)connect sends the tail again)

  /** Close the open log stream, if any. */
  const closeLog = () => {
    if (source !== null) {
      source.close();
      source = null;
    }
  };

  /** Show a status line under the tabs. */
  const tell = (message) => {
    status.textContent = message;
  };

  /** Add `lines` to the text (or replace it when `fresh`), keeping the view at the end if it was. */
  const show = (lines) => {
    const atEnd = text.scrollTop + text.clientHeight >= text.scrollHeight - 4;
    const chunk = lines.length ? lines.join("\n") + "\n" : "";
    if (fresh) {
      text.textContent = chunk;
      fresh = false;
    } else {
      text.append(document.createTextNode(chunk));
    }
    if (atEnd) {
      text.scrollTop = text.scrollHeight;
    }
  };

  /** (Re)open the log of the picked attempt and stream. */
  const openLog = () => {
    closeLog();
    text.textContent = "";
    fresh = true;
    const chosen = picker.selectedOptions[0];
    if (stream === "stderr" && chosen && chosen.dataset.merged === "1") {
      tell("stderr is merged into stdout for this attempt.");
      return;
    }
    tell("Connecting...");
    const query = new URLSearchParams({ stream, attempt: picker.value });
    const opened = new EventSource(
      `/events/log/${encodeURIComponent(logBox.dataset.run)}?${query.toString()}`,
    );
    source = opened;
    opened.addEventListener("open", () => {
      fresh = true;
      tell("Live.");
    });
    opened.addEventListener("lines", (event) => show(JSON.parse(event.data)));
    opened.addEventListener("reset", () => {
      text.textContent = "";
      fresh = false;
    });
    opened.addEventListener("end", () => {
      opened.close();
      if (source === opened) {
        source = null;
      }
      tell("The attempt has ended.");
    });
    opened.addEventListener("error", () => {
      if (opened.readyState === EventSource.CLOSED) {
        tell("There is no log for this attempt and stream.");
      } else {
        tell("Reconnecting...");
      }
    });
  };

  for (const tab of tabs) {
    tab.addEventListener("click", () => {
      stream = tab.dataset.stream;
      for (const other of tabs) {
        other.setAttribute("aria-pressed", String(other === tab));
      }
      openLog();
    });
  }
  picker.addEventListener("change", openLog);
  openLog();
}

// -- admin page: reload and dead-letter replay -----------------------------

const adminMessage = document.getElementById("admin-message");

/** Show `text` in the admin page's message line (hidden when empty). */
function adminSay(text) {
  if (adminMessage) {
    adminMessage.textContent = text;
    adminMessage.hidden = !text;
  }
}

/** Return one problem from the reload answer as a line of text. */
function problemLine(problem) {
  const where = problem.line ? `${problem.file}:${problem.line}` : String(problem.file);
  return `${where}: ${problem.message}`;
}

/** POST /api/reload and show the problems it returns, or "reloaded". */
async function reloadConfig(button) {
  button.disabled = true;
  let result;
  try {
    result = await post("/api/reload", {});
  } catch {
    result = { ok: false, status: 0, data: null };
  }
  button.disabled = false;
  const data = result.data || {};
  const listed = Array.isArray(data.problems) ? data.problems : data.warnings || [];
  const lines = listed.map(problemLine);
  if (result.ok) {
    adminSay(["reloaded", ...lines].join("\n"));
  } else if (lines.length) {
    adminSay(lines.join("\n"));
  } else {
    adminSay(data.error ? String(data.error) : `reload failed (${result.status || "network error"})`);
  }
}

/** Fetch the dead-letter tray again and swap it in. */
async function refreshDeadLetters() {
  let found;
  try {
    found = await fragment("/ui/fragments/dead-letters", "#dead-letters");
  } catch {
    return;
  }
  const old = document.getElementById("dead-letters");
  if (found.element && old) {
    old.replaceWith(found.element);
  }
}

/** POST a replay of one dead delivery, then redraw the tray. */
async function replayAlert(button) {
  if (!(await confirmThen(button))) {
    return;
  }
  button.disabled = true;
  let result;
  try {
    result = await post(`/api/alerts/${encodeURIComponent(button.dataset.replay)}/replay`, {
      notifier: button.dataset.notifier,
    });
  } catch {
    result = { ok: false, status: 0, data: null };
  }
  button.disabled = false;
  if (result.ok) {
    adminSay("");
    await refreshDeadLetters();
  } else {
    const error = result.data && result.data.error;
    adminSay(error ? String(error) : `replay failed (${result.status || "network error"})`);
  }
}

document.addEventListener("click", (event) => {
  const reload = event.target.closest("button#reload-config");
  if (reload) {
    event.preventDefault();
    reloadConfig(reload);
    return;
  }
  const replay = event.target.closest("button[data-replay]");
  if (replay) {
    event.preventDefault();
    replayAlert(replay);
  }
});
