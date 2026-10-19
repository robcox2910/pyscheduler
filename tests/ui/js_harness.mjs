// A tiny stand-in browser for the page script's shared module, run by
// tests/ui/test_static_modules.py with node. It checks what the page does
// when the session is gone (ruling FR1):
//
//   node js_harness.mjs <path to common.js (or an old app.js)> <case>
//
// <case> is "closed" (the live feed closed for good), "fragment" (a
// fragment answered 401) or "post" (an action answered 401). It prints
// one JSON line: {"reloads": <page reloads>, "notice": <last text shown>}.
//
// Four more cases use a fake clock (timers run at once and are recorded,
// Date.now() is ours) and a scripted /api/health:
//
//   "restart"  the port answers 503, 503, then 200: the page reloads once,
//              after waiting 1 s then 2 s. Prints {"reloads", "delays", "notice"}.
//   "restart-net"  a network error, then 200: the same, one wait.
//   "board-early"  run events for a later date arrive while health still says
//              today (no reload; one check per minute), then health moves on
//              (reload). Prints {"reloads", "checks"} after each step.
//   "board-trailing"  events held back by the minute's throttle leave exactly one
//              trailing check, which finds the new day.
//   "board-hang"  a health check that never answers is cut off by its timeout, so the
//              next one can run. "restart-hang": the same for the restart poller.
//   "board-inflight"  two events arrive while one check is still pending: one check.

const target = process.argv[2];
const which = process.argv[3];
let reloads = 0;
const notices = [];

class FakeElement {
  constructor(tag) {
    this.tag = tag;
    this.dataset = {};
    this.attributes = {};
    this._text = "";
    this.id = "";
    this.className = "";
  }
  set textContent(value) {
    this._text = String(value);
    notices.push(this._text);
  }
  get textContent() {
    return this._text;
  }
  setAttribute(name, value) {
    this.attributes[name] = value;
  }
  prepend() {}
  addEventListener() {}
  querySelector() {
    return null;
  }
}

const body = new FakeElement("body");
body.dataset.liveAfter = "boot-1";
const main = new FakeElement("main");
main.prepend = (child) => {
  byId[child.id] = child;
};
const byId = {};

globalThis.document = {
  body,
  querySelector: (selector) => (selector === "main" ? main : null),
  getElementById: (id) => byId[id] || null,
  createElement: (tag) => new FakeElement(tag),
  addEventListener() {},
};
globalThis.window = {
  location: {
    search: "",
    reload() {
      reloads += 1;
    },
  },
  confirm: () => true,
  sessionStorage: { getItem: () => null, setItem() {} },
};

const sources = [];
class FakeEventSource {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 2;
  constructor(url) {
    this.url = url;
    this.readyState = FakeEventSource.CONNECTING;
    this.listeners = {};
    sources.push(this);
  }
  addEventListener(kind, handler) {
    (this.listeners[kind] ||= []).push(handler);
  }
  fire(kind, data = {}) {
    for (const handler of this.listeners[kind] || []) {
      handler({ data: JSON.stringify(data) });
    }
  }
  close() {
    this.readyState = FakeEventSource.CLOSED;
  }
}
globalThis.EventSource = FakeEventSource;

const answer = 401;
const healthScript = []; // what /api/health answers next: a status, {today}, or "net"
let healthCalls = 0;
let healthGate = null; // a promise the next health answer waits for
globalThis.fetch = async (url, options = {}) => {
  if (url === "/api/health") {
    healthCalls += 1;
    if (healthGate) {
      await healthGate;
    }
    const next = healthScript.length > 1 ? healthScript.shift() : healthScript[0];
    if (next === "hang") {
      return new Promise((_, reject) => {
        options.signal?.addEventListener("abort", () => reject(new TypeError("timed out")));
      });
    }
    if (next === "net") {
      throw new TypeError("network error");
    }
    const status = typeof next === "number" ? next : 200;
    return {
      ok: status === 200,
      status,
      redirected: false,
      json: async () => (typeof next === "object" ? next : { error: "scheduler is starting" }),
      text: async () => "",
    };
  }
  return {
    ok: answer >= 200 && answer < 300,
    status: answer,
    redirected: false,
    json: async () => ({ error: "unauthorised" }),
    text: async () => "",
  };
};

const delays = [];
const realSetTimeout = globalThis.setTimeout;
let clock = 1_000_000;
if (which !== "closed" && which !== "fragment" && which !== "post") {
  const later = []; // timers of a minute or more wait for the fake clock
  globalThis.setTimeout = (fn, ms) => {
    delays.push(ms);
    if (ms >= 30_000) {
      later.push({ fn, due: clock + ms });
      return later.length;
    }
    return realSetTimeout(fn, 0);
  };
  globalThis.advance = (ms) => {
    clock += ms;
    for (const timer of later.filter((t) => t.due <= clock)) {
      later.splice(later.indexOf(timer), 1);
      timer.fn();
    }
    return later.length;
  };
  AbortSignal.timeout = () => {
    const controller = new AbortController();
    realSetTimeout(() => controller.abort(), 5);
    return controller.signal;
  };
  Date.now = () => clock;
}
const wait = (ms) => new Promise((resolve) => realSetTimeout(resolve, ms));
const seen = () => ({ reloads, notice: notices.at(-1) || "" });
const shared = await import(target);

if (which === "closed") {
  shared.onLive("run", () => {});
  const source = sources[0];
  source.readyState = FakeEventSource.CLOSED;
  source.fire("error");
} else if (which === "fragment" && typeof shared.fragment === "function") {
  await shared.fragment("/ui/fragments/board-counts", "#board-counts");
} else if (which === "post") {
  await shared.post("/api/runs/x/kill", {});
}
else if (which === "restart" || which === "restart-net" || which === "restart-hang") {
  const first = { restart: [503, 503], "restart-net": ["net"], "restart-hang": ["hang"] }[which];
  healthScript.push(...first, { today: "x" });
  shared.reloadBecause("waiting");
  await wait(50);
  console.log(JSON.stringify({ ...seen(), delays }));
  process.exit(0);
} else if (which.startsWith("board")) {
  const table = new FakeElement("table");
  table.dataset.date = "2026-01-01";
  byId.board = table;
  const later = { run_date: "2026-01-02", run_id: 1 };
  await import(target.replace("common.js", "board.js"));
  const steps = [];
  const step = async () => {
    await wait(20);
    steps.push({ reloads, checks: healthCalls });
  };
  if (which === "board-early") {
    healthScript.push({ today: "2026-01-01" });
    sources[0].fire("run", later);
    await step();
    sources[0].fire("run", later); // within the minute: no new check
    await step();
    clock += 61_000;
    healthScript.length = 0;
    healthScript.push({ today: "2026-01-02" });
    sources[0].fire("run", later);
    await step();
  } else if (which === "board-trailing") {
    healthScript.push({ today: "2026-01-01" });
    sources[0].fire("run", later);
    await step();
    clock += 20_000;
    sources[0].fire("run", later); // held back: leaves a trailing check
    sources[0].fire("run", later); // still only one
    await step();
    steps.push({ timers: globalThis.advance(0) });
    healthScript.length = 0;
    healthScript.push({ today: "2026-01-02" });
    globalThis.advance(40_000);
    await step();
  } else if (which === "board-hang") {
    healthScript.push("hang");
    sources[0].fire("run", later);
    await wait(50); // the timeout cut the check off
    clock += 61_000;
    sources[0].fire("run", later);
    await step();
  } else {
    let release = () => {};
    healthGate = new Promise((resolve) => {
      release = resolve;
    });
    healthScript.push({ today: "2026-01-02" });
    sources[0].fire("run", later);
    sources[0].fire("run", later);
    await step();
    release();
    await step();
  }
  console.log(JSON.stringify(steps));
  process.exit(0);
}
await wait(1700);
console.log(JSON.stringify(seen()));
