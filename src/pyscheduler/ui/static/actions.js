// Operator buttons: run actions (board, job and run pages) and the job
// page's trigger and backfill. Each one confirms, then calls the JSON API.

import { board, refreshRow } from "./board.js";
import { confirmThen, post, say } from "./common.js";

/**
 * Ask whether a rerun should take the downstream runs along. Returns true
 * (rerun with downstream), false (rerun only this run) or null (cancel).
 * Two plain questions, so Cancel on the second one gives up.
 */
function askDownstream() {
  const withDownstream =
    "Rerun this run and its downstream runs?\n\nOK: rerun with downstream.\nCancel: choose again.";
  if (window.confirm(withDownstream)) {
    return true;
  }
  if (window.confirm("Rerun only this run?\n\nOK: rerun only this run.\nCancel: do nothing.")) {
    return false;
  }
  return null;
}

/** Show the API's error text for a failed `what` (textContent only). */
function sayFailed(what, result) {
  const error = result.data && result.data.error;
  say(error ? String(error) : `${what} failed (${result.status || "network error"})`);
}

/**
 * Run a `data-action` button: confirm, then POST /api/runs/<data-run>/<action>.
 * A rerun also asks whether to rerun the downstream runs (or to cancel).
 * The API's error text is shown in `#board-message` (textContent only).
 */
async function runAction(button) {
  if (!(await confirmThen(button))) {
    return;
  }
  const action = button.dataset.action;
  const body = {};
  if (action === "rerun") {
    const downstream = askDownstream();
    if (downstream === null) {
      return;
    }
    body.downstream = downstream;
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
  } else if (result.status !== 401) {
    sayFailed(action, result);
  }
}

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
  } else if (result.status !== 401) {
    sayFailed(kind, result);
  }
}

document.addEventListener("click", (event) => {
  const run = event.target.closest("button[data-action][data-run]");
  if (run) {
    event.preventDefault();
    runAction(run);
    return;
  }
  const job = event.target.closest("button[data-job-action]");
  if (job) {
    event.preventDefault();
    jobAction(job);
  }
});
