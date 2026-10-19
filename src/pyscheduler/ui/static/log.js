// The run page: reload it when its run changes, and the live log viewer
// (tabs for stdout and stderr, a picker for the attempt). Log lines go in
// with textContent only.

import { SESSION_EXPIRED, isGone, onLive, reloadBecause } from "./common.js";

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

/**
 * Return whether the session is still good, by asking for the run itself
 * (an EventSource cannot tell a 401 from a 404). A network error counts
 * as good: the browser will try again.
 */
async function sessionGood(runId) {
  try {
    const response = await fetch(`/api/runs/${encodeURIComponent(runId)}`, {
      credentials: "same-origin",
    });
    return !isGone(response.status);
  } catch {
    return true;
  }
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

  /** Say why the stream closed for good: the session is gone (reload) or there is no log. */
  const closedForGood = async (opened) => {
    if (!(await sessionGood(logBox.dataset.run))) {
      reloadBecause(SESSION_EXPIRED);
    } else if (source === opened) {
      tell("There is no log for this attempt and stream.");
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
        closedForGood(opened);
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
