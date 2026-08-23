# Changelog

All notable changes to Harness are recorded here. The format is
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.0] - 2026-08-23

Less talking, more landing. Yesterday's numbers: 62% of spend was the section
talking to itself (plans, triage, stand-up) and seven restarts orphaned 30
runs. This release goes at both.

### Added

- **Graceful drain on SIGTERM.** A deploy, `docker compose down` or a
  watchtower update no longer kills agents mid-run. The first SIGTERM asks
  the worker to finish what is in flight and start nothing new (`run_agent`
  refuses with the same "pause, resume later" path as API limits), keeps
  the GUI up with a banner, and exits once drained or after
  `HARNESS_DRAIN_TIMEOUT_S` (default 25 minutes). A second SIGTERM exits
  at once. The compose file sets `stop_grace_period: 30m` to match; give
  watchtower `WATCHTOWER_TIMEOUT=30m`.
- `HARNESS_TRIAGE_MODEL` (default `claude-sonnet-5`): Ruth's triage and PR
  review run on a mid-tier model. The reproduction test is proven against
  the code either way and the engineer on the main model has the final say.

### Changed

- **Leads plan only when the desk actually changes.** "Backlog exceeds
  engineers" re-planned every sweep, because a retry, a failed attempt or a
  restart requeue bumps `updated_at`; it now fires only when an approved
  item appears that the lead has not ordered before. A forced cycle (Run
  now, an approval, an answer) no longer makes every lead re-plan — it syncs
  and starts ready work.

## [0.6.1] - 2026-08-23

### Fixed

- Runs orphaned by a restart no longer count towards the circuit breaker.
  Restart recovery closes in-flight runs with `ok=0`, so two deploys in a
  row tripped the breaker on whatever happened to be running — the item was
  held and the operator paged ("two consecutive failed runs") for a failure
  that was the process being killed, not the agent's. `consecutive_failures`
  now skips those runs; real failures either side of an orphan still count.

## [0.6.0] - 2026-08-23

### Added

- **Item threads.** Every issue and PR now has one running conversation —
  Ruth's findings and plan, Harry's rulings, the operator's directions, the
  engineer's notes and test results. Each agent that touches the item reads
  the whole thread before starting and appends to it as it goes, so context
  hands over intact rather than living in scattered columns. The thread is
  shown on the item page.
- **The section talks while it works.** Every agent session gets two
  in-process tools: `ask_harry`, which files a question and returns Harry's
  ruling inside the same run (escalating to the operator only where he
  would), and `note`, which appends to the item thread. Agents are still
  barred from the network and GitHub; both tools are deterministic code on
  the harness side.
- **Steer a running agent.** The run page has a "Tell <agent>" box while a
  run is live; the message is delivered into the conversation on the next
  turn, echoed into the console and mirrored to the item thread. Stop now
  interrupts the session cleanly rather than abandoning it.
- **Reproduction tests from triage.** When Ruth finds a fixable bug she
  writes a test that fails on the current code. The harness places it in
  the engineer's worktree, proves it fails before the fix, and tells the
  engineer to make it pass without weakening it.
- **Daily budget governor.** A per-desk `daily_budget_usd` policy (default
  $30) stops a desk starting new agent work once it has spent that much in
  24 hours — the guard that makes "run until the board is clear" safe to
  leave unattended.
- Optional sandbox/env isolation hook for agent sessions.

### Changed

- Agent sessions run on the SDK client rather than one-shot `query`, which
  is what makes steering and interruption possible.
- The idle poll is now 5 minutes (was 30) and a desk with work it can carry
  on with re-wakes in seconds, so the section keeps going without anyone's
  click. A wake with nothing new runs no agents.
- Operator answers and directions on an item are recorded in its thread.

## [0.5.0] - 2026-08-23

### Added

- The nav now marks the project you are looking at (#3). The link carries
  `aria-current="page"` and accent styling, worked out once when the page is
  rendered, so it holds on the project board, its settings, an item and a run
  page alike — a run with no project of its own highlights nothing. A disabled
  project still reads as dim while it is the one in view.

### Changed

- The **+ add** control in the nav is now a plain **+** icon (#3), with an
  `aria-label` and a hover title so it still announces itself, sized to a 44px
  tap target. On a narrow phone the nav was mostly taken up by the word "add".
- The README's install steps say "click **+** in the nav" to match.
- The screenshots in the README and on the site are hand-made and now trail
  the app twice over: they show the dark palette, which stopped being the
  default in 0.4.0, and the old **+ add** control. There is no screenshot
  tooling in the repo to regenerate them from.

## [0.4.0] - 2026-08-23

### Added

- An explicit **Dark** / **Light** button in the nav, and the dashboard now
  opens light for everyone rather than following the operating system (#2).
  The choice is kept in a `theme` cookie for a year, so it is remembered per
  browser. `data-theme` is rendered on the server, so a page arrives already
  in the right palette — no flash on load — and the toggle is a plain form
  post, so it works with JavaScript off. The `theme-color` meta follows the
  chosen theme too. The `/theme` post only redirects back to a referer on
  this host.

### Changed

- The README gained a short section on the palette and the toggle. The
  screenshots in the README and on the site still show the dark palette,
  which is now opt-in rather than what a new install looks like.

## [0.3.1] - 2026-08-23

### Fixed

- The footer version was hand-maintained and could name a build that wasn't
  running (#1). It is derived now: the number from `VERSION` in
  `harness/config.py`, the SHA from `HARNESS_GIT_SHA`, then the stamp the
  image build writes to `harness/_build_sha`, then `git HEAD` of the deployed
  checkout — each step guarded and timeout-bounded, since config is imported
  everywhere and this resolves once at import. When nothing can identify the
  build the footer says `(unknown build)` rather than implying a commit it
  doesn't know. The code for this shipped in the 0.2.0 image but was missed
  from that release's notes; it is recorded here.
- The README described the footer as `v0.1.0 (abc1234)` — the very thing #1
  was about, a version number kept by hand and left behind. The examples no
  longer carry a number.
- The README placed **Release now** on the project page only; since 0.3.0 it
  is on every card on the overview too.

## [0.3.0] - 2026-08-23

### Added

- **Release now** on every card on the overview, so a release can be forced
  from the page the desks are watched on rather than one level down. The
  override itself is not new — the request has always been honoured ahead of
  `release_min_changes` and `release_max_age_days` — but it was only reachable
  from the project page.
- A `release requested` pill on the overview card, so a press is acknowledged
  and the button cannot be fired twice by accident.

## [0.2.0] - 2026-08-23

### Added

- **Release now** on the project page cuts a release without waiting for the
  batch thresholds — including when the only changes on dev landed outside
  the harness, which previously did nothing at all.
- **Merge now** on an unreviewed pull request, for when you already know the
  answer and do not need Ruth's. The harness still merges it onto dev in its
  own clone and runs the suite before landing it, and still refuses drafts.
- Projects running `cut_release: auto` are marked as such on the overview and
  the project page, so hands-off repos are visible at a glance.

### Fixed

- The live console showed nothing for the whole of a run, under the words
  "Streaming — output appears as the agent works", which read as a stalled
  agent. A run's `log_path`, `turns` and `cost_usd` were only written when it
  finished, so the tail endpoint reported `live: false` and the poller gave
  up. Progress is recorded as it happens now.
- An auto release sat in `proposed` while it was being finalised, so the GUI
  offered a Merge & tag button for a release already on its way out; a click
  would have tried to merge a merged PR. It is marked `merging` first.
- An approved PR was merged on the strength of a test run from review time,
  which may have been several dev commits earlier. Every merge now re-runs
  the suite against the actual merge result.
- An auto release no longer counts toward the "needs your decision" tally on
  the overview, because it does not need one.

## [0.1.0] - 2026-08-22

First tagged release.
