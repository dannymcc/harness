// A reply from the server shown under a composer; "" clears it.
function say(form, message) {
  let box = form.nextElementSibling;
  if (!box || !box.classList.contains("compose-reply")) {
    if (!message) return;
    box = document.createElement("pre");
    box.className = "compose-reply";
    form.after(box);
  }
  box.textContent = message;
  box.hidden = !message;
}


// Progressive enhancement: submit decision forms in the background and
// update the page in place, instead of a full reload. Everything still
// works with JS disabled — these are ordinary POST forms underneath.
document.addEventListener("submit", async (e) => {
  const form = e.target;
  const inline = form.matches(".answer-form, .actions form");
  if (!inline) return; // policy selects etc. keep normal navigation

  e.preventDefault();
  const submitter = e.submitter;
  const action = (submitter && submitter.getAttribute("formaction")) || form.action;
  const buttons = form.querySelectorAll("button");
  buttons.forEach((b) => (b.disabled = true));

  try {
    const res = await fetch(action, { method: "POST", body: new FormData(form) });
    const textInput = form.querySelector('input[name="text"]');

    // A plain-text answer — a slash command that could not act, the
    // cheatsheet, a refused cross-site POST — is for the operator to read,
    // so put it under the box rather than reloading it away.
    const plain = (res.headers.get("content-type") || "").startsWith("text/plain");
    if (textInput && (plain || !res.ok)) {
      say(form, plain ? await res.text() : `Sorry — that failed (${res.status}).`);
      buttons.forEach((b) => (b.disabled = false));
      return;
    }
    if (!res.ok && !res.redirected) throw new Error(String(res.status));
    if (textInput) say(form, "");

    // Only the composer takes slash commands — a steer starting with "/" is
    // just text for the agent.
    const composer = new URL(form.action).pathname.endsWith("/tell");
    if (textInput && composer && textInput.value.trim().startsWith("/")) {
      // A command acted: show what it did, wherever its route landed.
      const to = res.redirected ? new URL(res.url) : null;
      if (to && to.pathname !== location.pathname) location.href = to.href;
      else location.reload();
      return;
    }
    if (textInput) {
      // "Direct the team" style form: confirm in place and show the
      // direction in the standing list immediately.
      const list = document.getElementById("directions-list");
      if (list && textInput.value && !form.querySelector('input[name="item_key"]')) {
        const li = document.createElement("li");
        const ts = document.createElement("span");
        ts.className = "ts";
        ts.textContent = "just now";
        const rep = document.createElement("div");
        rep.className = "direction-reply pending";
        rep.textContent = "Harry is on it…";
        li.append(ts, " " + textInput.value, rep);
        list.prepend(li);
        while (list.children.length > 3) list.lastChild.remove();
        pollDirections(list);
      }
      textInput.value = "";
      const btn = submitter || buttons[0];
      if (btn) {
        const label = btn.textContent;
        btn.textContent = "Sent ✓";
        setTimeout(() => { btn.textContent = label; btn.disabled = false; }, 1500);
      }
      buttons.forEach((b) => (b.disabled = false));
      return;
    }

    const row = form.closest(".question, .item-row");
    if (row) {
      row.style.transition = "opacity .25s";
      row.style.opacity = "0.25";
      setTimeout(() => {
        const section = row.closest("section");
        row.remove();
        // Update "(N)" in the section heading, remove section when empty.
        if (section) {
          const remaining = section.querySelectorAll(".question, .item-row").length;
          const h2 = section.querySelector("h2");
          if (h2) {
            for (const node of h2.childNodes) {
              if (node.nodeType === Node.TEXT_NODE && /\(\d+\)/.test(node.nodeValue)) {
                node.nodeValue = node.nodeValue.replace(/\(\d+\)/, `(${remaining})`);
              }
            }
          }
          if (remaining === 0) section.remove();
        }
      }, 250);
    } else {
      location.reload();
    }
  } catch {
    location.reload();
  }
});


// Slash commands: show the cheatsheet while the composer holds one, so the
// operator can see what is on offer without sending anything.
document.addEventListener("input", (e) => {
  const input = e.target;
  if (!input.matches('.answer-form input[name="text"]')) return;
  const sheet = input.form.parentElement.querySelector(".cheatsheet");
  if (sheet) sheet.hidden = !input.value.trim().startsWith("/");
});


// Timestamps: relative ("4 min ago"), live-updating, with the absolute
// local time on hover. Server keeps emitting ISO-UTC; presentation is the
// browser's job.
const ISO_RE = /\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\b/g;

function relativeTime(d, now) {
  const s = Math.floor((now - d) / 1000);
  if (s < 45) return "just now";
  if (s < 90) return "1 min ago";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return h === 1 ? "1 hr ago" : `${h} hrs ago`;
  const days = Math.floor(h / 24);
  if (days < 7) return days === 1 ? "yesterday" : `${days} days ago`;
  const opts = { day: "numeric", month: "short" };
  if (d.getFullYear() !== new Date(now).getFullYear()) opts.year = "numeric";
  return d.toLocaleDateString(undefined, opts);
}

document.addEventListener("DOMContentLoaded", () => {
  const now = Date.now();
  document.querySelectorAll(".ts").forEach((el) => {
    for (const node of [...el.childNodes]) {
      if (node.nodeType !== Node.TEXT_NODE || !ISO_RE.test(node.nodeValue)) {
        ISO_RE.lastIndex = 0;
        continue;
      }
      ISO_RE.lastIndex = 0;
      const frag = document.createDocumentFragment();
      let last = 0;
      for (const m of node.nodeValue.matchAll(ISO_RE)) {
        frag.append(node.nodeValue.slice(last, m.index));
        const d = new Date(m[0]);
        const t = document.createElement("time");
        t.dateTime = m[0];
        t.title = d.toLocaleString();
        t.textContent = relativeTime(d, now);
        frag.append(t);
        last = m.index + m[0].length;
      }
      frag.append(node.nodeValue.slice(last));
      node.replaceWith(frag);
    }
  });

  setInterval(() => {
    const tick = Date.now();
    document.querySelectorAll(".ts time[datetime]").forEach((t) => {
      t.textContent = relativeTime(new Date(t.dateTime), tick);
    });
  }, 30_000);
});


// Run facts strip: turns · cost · model · elapsed above the console, kept
// moving by the same poller that feeds the transcript. Elapsed is the only
// number the browser works out for itself; cost stays whatever the server
// says, which is nothing until the run ends and the SDK reports it.
function elapsedText(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  const pad = (n) => String(n).padStart(2, "0");
  if (s < 3600) return `${Math.floor(s / 60)}m ${pad(s % 60)}s`;
  return `${Math.floor(s / 3600)}h ${pad(Math.floor(s / 60) % 60)}m`;
}

const runFacts = {
  strip: null,
  started: null,
  finished: "",
  fact(key) {
    return this.strip && this.strip.querySelector(`[data-fact="${key}"]`);
  },
  show(key, text) {
    const el = this.fact(key);
    if (el) {
      el.textContent = text;
      el.hidden = false;
    }
  },
  paintElapsed() {
    if (!this.started) return;
    const end = this.finished ? new Date(this.finished) : new Date();
    this.show("elapsed",
              `${elapsedText(end - this.started)} ${this.finished ? "in total"
                                                                 : "elapsed"}`);
  },
  update(j) {
    if (!this.strip) return;
    if (typeof j.turns === "number") this.show("turns", `${j.turns} messages`);
    if (j.model) this.show("model", j.model);
    if (typeof j.cost_usd === "number") {
      this.show("cost", `≈US$${j.cost_usd.toLocaleString(undefined, {
        minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
    }
    if (j.finished_at && !this.finished) {
      this.finished = j.finished_at;
      this.show("finished", `finished ${j.finished_at}`);
      // The tail says the run is over but not how it went; the reload a
      // moment later brings back succeeded/failed. Don't guess in between.
      const status = this.fact("status");
      if (status) {
        const pill = document.createElement("span");
        pill.className = "pill";
        pill.textContent = "finished";
        status.replaceChildren(pill);
      }
    }
    this.paintElapsed();
  },
};

document.addEventListener("DOMContentLoaded", () => {
  runFacts.strip = document.getElementById("run-facts");
  if (!runFacts.strip) return;
  runFacts.started = runFacts.strip.dataset.started
    ? new Date(runFacts.strip.dataset.started) : null;
  runFacts.finished = runFacts.strip.dataset.finished || "";
  // The strip sticks below the nav, which wraps to two rows on a phone;
  // measure rather than guess at the fallback in :root.
  const nav = document.querySelector("nav");
  if (nav) {
    const setNavHeight = () => document.documentElement.style
      .setProperty("--nav-h", `${nav.offsetHeight}px`);
    setNavHeight();
    window.addEventListener("resize", setNavHeight);
  }
  runFacts.paintElapsed();
  if (!runFacts.finished) setInterval(() => runFacts.paintElapsed(), 1000);
});


// Live console: tail the run transcript while the run is in flight.
document.addEventListener("DOMContentLoaded", () => {
  const pre = document.querySelector("[data-tail-run]");
  if (!pre) return;
  const runId = pre.dataset.tailRun;
  let offset = new Blob([pre.textContent]).size;
  let stopped = false;
  const nearBottom = () =>
    pre.scrollHeight - pre.scrollTop - pre.clientHeight < 60;
  async function tick() {
    if (stopped) return;
    try {
      const res = await fetch(`/run/${runId}/tail?offset=${offset}`);
      if (res.ok) {
        const j = await res.json();
        runFacts.update(j);
        if (j.data) {
          const follow = nearBottom();
          pre.append(j.data);
          offset = j.offset;
          if (follow) pre.scrollTop = pre.scrollHeight;
        }
        if (!j.live) {
          stopped = true;
          setTimeout(() => location.reload(), 1500); // pick up final status
          return;
        }
      }
    } catch {}
    setTimeout(tick, 2500);
  }
  pre.scrollTop = pre.scrollHeight;
  tick();
});


// Poll for Harry's acknowledgements while any direction is pending.
let directionsPoller = null;
function pollDirections(list) {
  if (directionsPoller) return;
  const project = list.dataset.project;
  let tries = 0;
  directionsPoller = setInterval(async () => {
    tries += 1;
    try {
      const res = await fetch(`/p/${project}/directions.json`);
      if (!res.ok) return;
      const j = await res.json();
      const pending = j.directions.some((d) => d.pending);
      list.replaceChildren(...j.directions.map((d) => {
        const li = document.createElement("li");
        const ts = document.createElement("span");
        ts.className = "ts";
        ts.textContent = d.ts;
        li.append(ts, " " + d.text);
        const rep = document.createElement("div");
        rep.className = "direction-reply" + (d.pending ? " pending" : "");
        if (d.pending) rep.textContent = "Harry is on it…";
        else if (d.reply) { const b = document.createElement("b");
          b.textContent = "Harry: "; rep.append(b, d.reply); }
        if (d.pending || d.reply) li.append(rep);
        return li;
      }));
      if (!pending || tries > 60) {
        clearInterval(directionsPoller);
        directionsPoller = null;
      }
    } catch {}
  }, 4000);
}

document.addEventListener("DOMContentLoaded", () => {
  const list = document.getElementById("directions-list");
  if (list && list.querySelector(".direction-reply.pending")) {
    pollDirections(list);
  }
});
