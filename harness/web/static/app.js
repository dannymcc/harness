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
    if (!res.ok && !res.redirected) throw new Error(String(res.status));

    const textInput = form.querySelector('input[name="text"]');
    if (textInput) {
      // "Direct the team" style form: confirm in place and show the
      // direction in the standing list immediately.
      const list = document.getElementById("directions-list");
      if (list && textInput.value && !form.querySelector('input[name="item_key"]')) {
        const li = document.createElement("li");
        const ts = document.createElement("span");
        ts.className = "ts";
        ts.textContent = "just now";
        li.append(ts, " " + textInput.value);
        list.prepend(li);
        while (list.children.length > 3) list.lastChild.remove();
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
