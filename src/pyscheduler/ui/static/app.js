// The pyscheduler web UI's one page script. Each page loads only this
// file (<script type="module" src="/static/app.js">); it pulls in the
// modules below from /static/ too, so `script-src 'self'` is all it needs.
// Every module looks for its own part of the page and does nothing on
// pages without it.
//
// - common.js: CSRF, JSON posts, "sure?" buttons, the live feed, fragments,
//   and reloading when the session is gone;
// - board.js: the live job board;
// - actions.js: run and job action buttons;
// - log.js: the run page and its live log viewer;
// - admin.js: config reload and dead-letter replay.

import "./common.js";
import "./board.js";
import "./actions.js";
import "./log.js";
import "./admin.js";
