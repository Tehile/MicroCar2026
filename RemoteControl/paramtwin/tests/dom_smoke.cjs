/* Headless smoke test for report.html: stub just enough DOM to run the three
   inline scripts and check that the views actually populate.
   Usage: node tests/dom_smoke.cjs <path-to-report.html> */
const fs = require("fs");
const vm = require("vm");
const path = require("path");

const file = process.argv[2] || path.join(__dirname, "..", "out", "report.html");
const html = fs.readFileSync(file, "utf8");

function mkCtx(el) {
  return new Proxy({}, {
    get(t, k) {
      if (k === "canvas") return {};
      if (k in t) return t[k];
      return function () { if (el) el._ops = (el._ops || 0) + 1; };
    },
    set(t, k, v) { t[k] = v; return true; }
  });
}

function mkEl(tag, id) {
  const el = {
    tagName: (tag || "div").toUpperCase(),
    id: id || "",
    className: "",
    textContent: "",
    value: "",
    width: 0,
    height: 0,
    clientWidth: 640,
    clientHeight: 240,
    style: {},
    dataset: {},
    options: [],
    children: [],
    _html: "",
    _ops: 0,
    classList: { add() { }, remove() { }, toggle() { } },
    appendChild(c) {
      this.children.push(c);
      if (c && c.tagName === "OPTION") this.options.push(c);
      return c;
    },
    addEventListener() { },
    setAttribute() { },
    getAttribute() { return null; },
    getBoundingClientRect() { return { width: 640, height: 240, top: 0, left: 0 }; },
    getContext() { return mkCtx(this); }
  };
  Object.defineProperty(el, "innerHTML", {
    get() { return this._html; },
    set(v) { this._html = v; if (v === "") this.children.length = 0; }
  });
  return el;
}

const ids = new Set();
for (const m of html.matchAll(/id="([^"]+)"/g)) ids.add(m[1]);
const registry = {};
ids.forEach(function (id) { registry[id] = mkEl("div", id); });

const listeners = {};
const document = {
  documentElement: { getPropertyValue() { return ""; } },
  getElementById(id) {
    if (!registry[id]) registry[id] = mkEl("div", id);
    return registry[id];
  },
  createElement(tag) { return mkEl(tag); },
  createTextNode(text) { return { nodeType: 3, textContent: String(text) }; },
  querySelectorAll() { return []; },
  addEventListener(type, fn) { listeners[type] = fn; },
  body: mkEl("body")
};

const sandbox = {
  document: document,
  console: console,
  JSON: JSON,
  Math: Math,
  Number: Number,
  Array: Array,
  Object: Object,
  String: String,
  isNaN: isNaN,
  parseInt: parseInt,
  parseFloat: parseFloat,
  getComputedStyle() { return { getPropertyValue() { return ""; } }; },
  devicePixelRatio: 1,
  setTimeout(fn) { return 0; },
  clearTimeout() { },
  requestAnimationFrame(fn) { return 0; }
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
sandbox.addEventListener = function (type, fn) { listeners["win:" + type] = fn; };

const dataScript = html.match(/<script id="data" type="application\/json">([\s\S]*?)<\/script>/);
if (!dataScript) { console.error("no data script"); process.exit(2); }
registry["data"].textContent = dataScript[1];

const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const errors = [];
vm.createContext(sandbox);
scripts.forEach(function (src, i) {
  try {
    vm.runInContext(src, sandbox, { filename: "inline-" + i + ".js" });
  } catch (e) {
    errors.push("script " + i + ": " + e.message);
  }
});

if (listeners.DOMContentLoaded) {
  try {
    listeners.DOMContentLoaded();
  } catch (e) {
    errors.push("DOMContentLoaded: " + e.message);
  }
}

const out = {
  errors: errors,
  kpis: registry["kpi-grid"] ? registry["kpi-grid"].children.length : -1,
  warnings: registry["warn-list"] ? registry["warn-list"].children.length : -1,
  sweep_rows: registry["sweep-rows"] ? registry["sweep-rows"].children.length : -1,
  act_options: registry["act-sel"] ? registry["act-sel"].options.length : -1,
  // every chart must have issued draw calls; 0 means the view rendered blank
  canvas_ops: ["c-sweep", "c-vs", "c-vt", "c-pwm"].reduce(function (o, id) {
    o[id] = registry[id] ? registry[id]._ops : -1;
    return o;
  }, {}),
  meta: registry["meta-line"] ? registry["meta-line"].textContent : ""
};
process.stdout.write(JSON.stringify(out));
process.exit(errors.length ? 1 : 0);
