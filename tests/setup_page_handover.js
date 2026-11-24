// Runs the port-80 Wi-Fi page's own script against a fake page and a
// scripted unit, and walks the handover the bench hit on 2026-11-17.
// Driven by tests/test_setup_handover.py: argv[2] is a JSON file with
// {copy, script, hidden_ids, poll_timeout_ms, give_up_ms}. Prints "ok", or
// exits 1 with the first failed check.
"use strict";
const fs = require("fs");
const vm = require("vm");

const page = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const C = page.copy;
const fmt = (t, ssid) => t.split("{ssid}").join(ssid || "");

class El {
  constructor(tag, id) {
    this.tagName = tag;
    this.id = id || "";
    this.children = [];
    this.listeners = {};
    this.textContent = "";
    this.hidden = false;
    this.checked = false;
    this.disabled = false;
    this.value = "";
    this.type = "";
    this.name = "";
    this.className = "";
  }
  appendChild(child) {
    this.children.push(child);
    child.parentNode = this;
    return child;
  }
  removeChild(child) {
    this.children = this.children.filter((c) => c !== child);
    return child;
  }
  get firstChild() {
    return this.children[0] || null;
  }
  addEventListener(type, fn) {
    (this.listeners[type] = this.listeners[type] || []).push(fn);
  }
  fire(type, ev) {
    for (const fn of this.listeners[type] || []) fn(ev || { preventDefault() {} });
  }
  inputs() {
    const out = [];
    const walk = (el) => {
      for (const c of el.children) {
        if (c.tagName === "input") out.push(c);
        walk(c);
      }
    };
    walk(this);
    return out;
  }
  querySelectorAll(sel) {
    const all = this.inputs();
    return sel.includes(":checked") ? all.filter((i) => i.checked) : all;
  }
  querySelector(sel) {
    return this.querySelectorAll(sel)[0] || null;
  }
}

function build() {
  const els = {};
  const document = {
    getElementById(id) {
      if (!els[id]) {
        els[id] = new El(id === "copy" ? "script" : "div", id);
        if (page.hidden_ids.includes(id)) els[id].hidden = true;
        if (id === "copy") els[id].textContent = JSON.stringify(C);
      }
      return els[id];
    },
    createElement(tag) {
      return new El(tag);
    },
  };
  let now = 1_000_000;
  const timers = [];
  const intervals = [];
  const unit = { state: { state: "idle" }, reachable: true, hang: false, join: null };
  const fetches = [];
  function fetch(path, opts) {
    opts = opts || {};
    fetches.push(path);
    const json = (body) => Promise.resolve({ json: () => Promise.resolve(body) });
    if (!unit.reachable) return Promise.reject(new TypeError("Load failed"));
    if (unit.hang) {
      return new Promise((_res, rej) => {
        if (opts.signal) opts.signal.addEventListener("abort", () => rej(new Error("aborted")));
      });
    }
    if (path === "/api/networks") {
      return json({ networks: [{ ssid: "SpectrumSetup-9206", joinable: true, bar_text: "|||" }] });
    }
    if (path === "/api/state") return json(unit.state);
    if (path === "/api/join") return json(unit.join);
    return json({});
  }
  const ctx = {
    document,
    window: { AbortController },
    AbortController,
    fetch,
    JSON,
    console,
    Date: { now: () => now },
    setTimeout(fn, ms) {
      const t = { fn, at: now + ms, done: false };
      timers.push(t);
      return t;
    },
    clearTimeout(t) {
      if (t) t.done = true;
    },
    setInterval(fn, ms) {
      intervals.push({ fn, ms });
      return intervals.length;
    },
  };
  vm.createContext(ctx);
  vm.runInContext(page.script, ctx);
  return {
    el: (id) => document.getElementById(id),
    unit,
    fetches,
    intervals,
    advance(ms) {
      now += ms;
      for (const t of timers) {
        if (!t.done && t.at <= now) {
          t.done = true;
          t.fn();
        }
      }
    },
    poll() {
      intervals[0].fn();
    },
  };
}

const settle = () => new Promise((r) => setImmediate(r));
async function flush() {
  for (let i = 0; i < 6; i++) await settle();
}

function check(cond, what) {
  if (!cond) {
    console.error("FAILED: " + what);
    process.exit(1);
  }
}

const SSID = "SpectrumSetup-9206";
const already = C.busy.split("{ssid}")[0].trim();

async function connect(p) {
  await flush();
  const radio = p.el("networks").querySelector("input");
  check(radio && radio.value === SSID, "the network is listed");
  radio.checked = true;
  p.el("password").value = "hunter22";
  p.unit.join = { state: "joining", ssid: SSID };
  p.unit.state = { state: "joining", ssid: SSID, at: "t1" };
  p.el("form").fire("submit");
  await flush();
}

async function main() {
  check(page.poll_timeout_ms > 0 && page.give_up_ms > 0, "timings are filled in");

  // 1. Connect hides the form and shows the handover panel.
  let p = build();
  check(p.intervals.length === 1 && p.intervals[0].ms === 3000, "polls every 3 s");
  check(p.el("handover").hidden === true, "the panel starts hidden");
  await connect(p);
  check(p.el("form").hidden === true, "the form is hidden after Connect");
  check(p.el("intro").hidden === true, "and so is the pick-a-network intro");
  check(p.el("handover").hidden === false, "the handover panel shows");
  check(p.el("ho-title").textContent === fmt(C.handover_title, SSID), "the panel names the network");
  check(p.el("ho-now").textContent === "", "the title says it all while still on the hotspot");
  check(p.el("ho-spin").hidden === false, "the spinner shows the unit is working");
  check(p.el("status").textContent === "", "the status line is empty");

  // 2. A poll during our own join never says "already connecting".
  p.poll();
  await flush();
  check(p.el("ho-now").textContent === "", "still just connecting after a poll");
  check(!p.el("ho-now").textContent.includes(already), "its own join is not 'already connecting'");
  check(!p.el("status").textContent.includes(already), "no 'already connecting' line");

  // 3. The hotspot goes away: the poll fails and the panel says so.
  p.unit.reachable = false;
  p.advance(3000);
  p.poll();
  await flush();
  check(p.el("ho-now").textContent === fmt(C.handover_away, SSID), "BlueSignal Setup is off");
  check(p.el("form").hidden === true, "the form stays hidden while the unit tries");

  // 4. Past the give-up time the panel says the unit most likely joined.
  p.advance(page.give_up_ms);
  p.poll();
  await flush();
  check(p.el("ho-now").textContent === fmt(C.handover_done, SSID), "most likely on the network");
  check(p.el("ho-spin").hidden === true, "the spinner stops when the page stops waiting");

  // 5. The hotspot comes back with a failure: the form returns with the reason.
  p.unit.reachable = true;
  p.unit.state = { state: "failed", ssid: SSID, reason: "wrong_password", at: "t2" };
  p.poll();
  await flush();
  check(p.el("handover").hidden === true, "the panel closes on a failure");
  check(p.el("form").hidden === false, "the form is back");
  check(p.el("intro").hidden === false, "with its intro");
  check(p.el("status").textContent === C.wrong_password, "the reason is shown");
  check(p.el("networks").querySelector("input:checked").value === SSID, "the network is still picked");

  // 6. A poll that never answers counts as "off" after the poll timeout.
  p = build();
  await connect(p);
  p.unit.hang = true;
  p.poll();
  await flush();
  check(p.el("ho-now").textContent === "", "a slow poll is not news yet");
  const before = p.fetches.length;
  p.poll();
  check(p.fetches.length === before, "a second poll waits for the first");
  p.advance(page.poll_timeout_ms);
  await flush();
  check(p.el("ho-now").textContent === fmt(C.handover_away, SSID), "a hung poll reads as off");

  // 7. A page opened while a join runs shows the same panel.
  p = build();
  p.unit.state = { state: "joining", ssid: SSID, at: "t3" };
  await flush();
  check(p.el("handover").hidden === false, "a join already running shows the panel");
  check(p.el("form").hidden === true, "and hides the form");

  // 8. A join refused because one is running shows that join's panel.
  p = build();
  await flush();
  const radio = p.el("networks").querySelector("input");
  radio.checked = true;
  p.unit.join = { state: "busy", ssid: "Barn" };
  p.el("form").fire("submit");
  await flush();
  check(p.el("handover").hidden === false, "busy shows the panel");
  check(p.el("ho-title").textContent === fmt(C.handover_title, "Barn"), "for the running join");
  check(p.el("ho-now").textContent === fmt(C.busy, "Barn"), "and says it is already connecting");

  // 9. A joined result while the panel is up says so in the panel.
  p = build();
  await connect(p);
  p.unit.state = { state: "joined", ssid: SSID, at: "t4" };
  p.poll();
  await flush();
  check(p.el("ho-now").textContent === fmt(C.joined, SSID), "joined is shown in the panel");
  check(p.el("ho-spin").hidden === true, "no spinner once joined");

  console.log("ok");
}

main().catch((e) => {
  console.error(e && e.stack ? e.stack : String(e));
  process.exit(1);
});
