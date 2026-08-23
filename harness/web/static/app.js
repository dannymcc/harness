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
      // "Direct the team" style form: clear and confirm in place.
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


// Render ISO-UTC timestamps in the viewer's own locale and timezone.
// Server keeps emitting ISO strings; presentation is the browser's job.
document.addEventListener("DOMContentLoaded", () => {
  const ISO = /\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\b/g;
  const now = new Date();
  const fmt = (d) => {
    const opts = { day: "numeric", month: "short",
                   hour: "2-digit", minute: "2-digit" };
    if (d.getFullYear() !== now.getFullYear()) opts.year = "numeric";
    return d.toLocaleString(undefined, opts);
  };
  document.querySelectorAll(".ts").forEach((el) => {
    for (const node of el.childNodes) {
      if (node.nodeType === Node.TEXT_NODE && ISO.test(node.nodeValue)) {
        node.nodeValue = node.nodeValue.replace(ISO, (m) => fmt(new Date(m)));
      }
      ISO.lastIndex = 0;
    }
  });
});
