// The admin page: the reload button and the dead-letter tray's replay
// buttons. Answers are shown with textContent only.

import { confirmThen, fragment, post } from "./common.js";

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
  if (result.status === 401) {
    return; // post() is already reloading the page
  }
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
  } else if (result.status !== 401) {
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
