// The live job board: when a run changes, fetch its row (drawn by the
// server) and put it in its place; refresh the counts bar too.

import { fragment, onLive } from "./common.js";

/** The board's table, or null on other pages. */
export const board = document.getElementById("board");

const COUNTS_DELAY_MS = 250;
const RELOAD_MS = 500;
const CHECK_EVERY_MS = 60000;
const rowFetches = new Map(); // run id -> number of the newest fetch for it
let fetchNumber = 0;
let countsTimer = null;
let countsNumber = 0; // the number of the newest counts fetch
let reloadTimer = null;
let checking = false; // a health check is in flight
let lastCheck = -Infinity; // when the last one started

/** Return the board's query (its filters, with the shown date) for a fragment. */
function boardQuery() {
  const query = new URLSearchParams(window.location.search);
  query.set("date", board.dataset.date);
  return query.toString();
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

/** Return the row of run `runId` on the board, or null. */
function rowOf(runId) {
  return board.tBodies[0].querySelector(`tr[data-run-id="${CSS.escape(runId)}"]`);
}

/** Put `row` in its place in the table body, replacing its old copy. */
function place(row) {
  const body = board.tBodies[0];
  const old = rowOf(row.dataset.runId);
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
export async function refreshRow(runId) {
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
    const old = rowOf(runId);
    if (old) {
      old.remove();
    }
  }
  markEmpty();
}

/**
 * Fetch the counts bar again, soon (several changes in a burst cost one
 * fetch). Fetches are numbered, and only the newest answer is used, so a
 * slow old answer never overwrites a newer one.
 */
function refreshCounts() {
  if (countsTimer !== null) {
    return;
  }
  countsTimer = setTimeout(async () => {
    countsTimer = null;
    countsNumber += 1;
    const mine = countsNumber;
    let found;
    try {
      found = await fragment(`/ui/fragments/board-counts?${boardQuery()}`, "#board-counts");
    } catch {
      return;
    }
    const old = document.getElementById("board-counts");
    if (mine === countsNumber && found.element && old) {
      old.replaceWith(found.element);
    }
  }, COUNTS_DELAY_MS);
}

/** Reload the board soon (a burst of reasons costs one reload). */
function reloadSoon() {
  if (reloadTimer === null) {
    reloadTimer = setTimeout(() => window.location.reload(), RELOAD_MS);
  }
}

/**
 * A board opened without `?date=` shows "today". A run for a later date
 * may only be a job in another zone or a future-dated trigger, so ask the
 * scheduler (`/api/health` says its today) and reload only when the day
 * has really moved on. One check at a time, and at most one a minute.
 */
async function moveOn() {
  const now = Date.now();
  if (checking || now - lastCheck < CHECK_EVERY_MS) {
    return;
  }
  checking = true;
  lastCheck = now;
  try {
    const response = await fetch("/api/health", { credentials: "same-origin" });
    const health = response.ok ? await response.json() : null;
    if (health && health.today > board.dataset.date) {
      reloadSoon();
    }
  } catch {
    // no answer: the next later-dated run asks again
  } finally {
    checking = false;
  }
}

if (board) {
  const followsToday = !new URLSearchParams(window.location.search).has("date");
  // Registered at load, so no event after the page was drawn is missed.
  // Replays may repeat a change the page already shows: refetching is harmless.
  onLive("run", (data) => {
    if (data.run_date === board.dataset.date) {
      refreshRow(String(data.run_id));
      refreshCounts();
    } else if (followsToday && data.run_date > board.dataset.date) {
      moveOn();
    }
  });
  onLive("attempt", (data) => {
    const runId = String(data.run_id);
    if (rowOf(runId)) {
      refreshRow(runId);
    }
  });
  // A reload can change jobs, tags and criticality: redraw the whole board.
  onLive("reload", reloadSoon);
}
