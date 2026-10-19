// A tiny stand-in browser for the page script's shared module, run by
// tests/ui/test_static_modules.py with node. It checks what the page does
// when the session is gone (ruling FR1):
//
//   node js_harness.mjs <path to common.js (or an old app.js)> <case>
//
// <case> is "closed" (the live feed closed for good), "fragment" (a
// fragment answered 401) or "post" (an action answered 401). It prints
// one JSON line: {"reloads": <page reloads>, "notice": <last text shown>}.

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
  fire(kind) {
    for (const handler of this.listeners[kind] || []) {
      handler({ data: "{}" });
    }
  }
  close() {
    this.readyState = FakeEventSource.CLOSED;
  }
}
globalThis.EventSource = FakeEventSource;

const answer = 401;
globalThis.fetch = async () => ({
  ok: answer >= 200 && answer < 300,
  status: answer,
  redirected: false,
  json: async () => ({ error: "unauthorised" }),
  text: async () => "",
});

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
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
await wait(1700);
console.log(JSON.stringify(seen()));
