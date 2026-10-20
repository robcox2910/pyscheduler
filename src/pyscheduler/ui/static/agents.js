// The agents page: when an agent changes (an `agent` live event), fetch
// its row again (drawn and escaped by the server) and put it in place,
// in name order. Only the newest fetch for an agent is used, so a slow
// old answer never overwrites a newer one.

import { fragment, onLive } from "./common.js";

/** The agents table, or null on other pages. */
const table = document.getElementById("agents");

const rowFetches = new Map(); // agent name -> number of the newest fetch for it
let fetchNumber = 0;

/** Return the row of agent `name`, or null. */
function rowOf(name) {
  return table.tBodies[0].querySelector(`tr[data-agent="${CSS.escape(name)}"]`);
}

/** Put `row` in name order in the table body, replacing its old copy. */
function place(row) {
  const body = table.tBodies[0];
  const old = rowOf(row.dataset.agent);
  if (old) {
    old.remove();
  }
  const next = Array.from(body.rows).find((other) => row.dataset.agent < other.dataset.agent);
  body.insertBefore(row, next || null);
  const empty = document.getElementById("agents-empty");
  if (empty) {
    empty.hidden = true;
  }
}

/** Fetch agent `name`'s row again and put it in place. */
async function refreshRow(name) {
  fetchNumber += 1;
  const mine = fetchNumber;
  rowFetches.set(name, mine);
  let found;
  try {
    found = await fragment(`/ui/fragments/agent-row/${encodeURIComponent(name)}`, "tr[data-agent]");
  } catch {
    return;
  }
  if (rowFetches.get(name) !== mine) {
    return;
  }
  rowFetches.delete(name);
  if (found.element && found.element.dataset.agent === name) {
    place(found.element);
  }
}

if (table) {
  // Registered at load, so no event after the page was drawn is missed.
  onLive("agent", (data) => refreshRow(String(data.name)));
}
