# Changelog

All notable changes to Harness are recorded here. The format is
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.12.0] - 2026-08-24

### Added

- **The item thread can be narrowed to what binds.** An item's thread is the
  hand-off artefact every agent reads, and it gets long: a single run of test
  output could bury the ruling and the direction either side of it. The item
  page now carries a row of plain filter links above the thread — all,
  rulings, directions, findings/plans, notes, events/tests — and rulings and
  directions also sit pinned in a block at the top, newest first, whatever
  filter is picked, because they bind everyone working the item. Anything
  past twelve lines folds behind a "show N more lines" toggle, so long test
  output no longer swamps the page. The filter lives only in the URL, with no
  client-side memory, so a shared item link shows everyone the same thing and
  the back button behaves; an unrecognised `?kind=` falls back to showing
  everything. The "Thread (N)" count stays a total of the whole thread, and
  the text handed to agents in their prompts is unchanged — this is a reading
  aid for the operator, not a change to what agents are told (issue #21).

## [0.11.0] - 2026-08-24

### Added

- **A steer the session never took is handed back, not lost.** Anything
  typed into "Tell <agent>" while a run was live but never picked up by the
  session used to sit unread in the database and show on the finished run
  page among the steers that had landed, so a note written seconds before a
  stop looked delivered when it wasn't. The run page now lists those under
  **Undelivered**, with two ways out: **Send as direction** files the text
  as an operator direction on the run's item, pending until Harry actions it
  on the next cycle — so it can move the item on, not merely annotate it —
  and **Discard** drops it. Runs with no project have nothing to file
  against, so only Discard is offered. When a run ends holding undelivered
  steers, the project events carry a warning naming the count, so it is
  visible without opening the run. Either choice settles the steer for good:
  it can never be handed to a later session, and keeping one doesn't repeat
  the text in the item thread, where the steer box already mirrored it
  (issue #19).

## [0.10.0] - 2026-08-24

### Added

- **Say it now, or say it for afterwards.** The run page's "Tell <agent>"
  box has a second button. **Send** is unchanged — it goes into the live
  session on its agent's next message. **When they finish** files the same
  text as an operator direction on the run's item instead, so it lands in
  the item thread and whoever picks the item up next reads it as binding
  context, without interrupting a run that is mid-edit. Nothing reaches the
  live session, so a note filed while an agent is halfway through a change
  no longer risks derailing it. Runs with no item have nowhere to file a
  note, so the button is hidden for them. Anything queued this way since the
  run started shows on the run page under "Queued for after", marked pending
  or picked up, alongside the steers that were delivered (issue #18).

## [0.9.1] - 2026-08-23

### Changed

- **Coverage for "Release now" on the overview, not just the project page.**
  Issue #17 asked for the button to appear only when there is something to
  cut. That behaviour already shipped in 0.8.3 with issue #10, and re-reading
  the code confirmed every acceptance criterion was met — but the web tests
  only checked the project page, so the overview card could have regressed
  unnoticed. The queued-items and dev-ahead-of-main tests now assert the card
  as well, covering all three states on both views. No behaviour change, and
  no reason to hurry the upgrade (issue #17).

## [0.9.0] - 2026-08-23

### Changed

- **Auto release is named and explained where it is set.** The Settings row
  for `cut_release` read "cut release" with no further word on the page, so
  the one policy that lets a repo ship without you was also the least
  legible. It is now labelled **auto release** and carries a one-line hint
  saying what each mode does — auto drafts the release, runs the tests,
  merges to the main branch and tags it without asking; approve prepares the
  same release and waits for your click, and nothing ships on a failing
  suite either way. It sits under an "Auto release" heading with the two
  numbers that decide when it fires (batch size and max age), and the page
  now states outright that every policy is per project, so putting one repo
  on auto changes nothing for the rest. Every other policy row gained a hint
  in the same style. On the project page the pill reads "auto release"
  rather than a bare "auto", and the releases card gives the thresholds for
  that project. Stored policy keys and their defaults are unchanged — new
  projects still default to approve (issue #11).

## [0.8.3] - 2026-08-23

### Fixed

- **Release now is only offered when there is something to cut.** The button
  used to show on the project page and on every overview card regardless, so
  pressing it with dev matching main and nothing queued did nothing visible
  beyond a "nothing to release" line in the event log. Both views now ask
  `pipeline.anything_to_release()` — queued items, or dev ahead of main —
  which is the same question the release cycle itself asks, so the button and
  the cycle can no longer disagree. Where there is nothing to cut the project
  page says so plainly instead; the tooltips now describe what the press
  actually does (drafts and tests a release), and the batching hint stops
  mentioning a button that isn't there. The POST route re-checks before
  setting the flag, so a stale page can't sneak a request through, and
  `repo.dev_ahead_count()` — now on a page-render path — takes a 15-second
  timeout so a wedged git can't hold the dashboard open. The README says so
  (issue #10).

## [0.8.2] - 2026-08-23

### Security

- **Untrusted GitHub text is fenced, and the agents that read it lost their
  general shell.** Issue and PR titles, bodies, comments, diffs, test output
  and CI results now reach a prompt inside `<<<UNTRUSTED ...>>>` markers,
  with any forged markers stripped from the text first and lengths capped
  (the triage issue body was previously uncapped); every session's standing
  orders say that fenced text is data from the public internet and never an
  instruction. The roles that read that text — triage, review, lead planning
  and security review — no longer get bare `Bash`. Their shell is an
  allowlist: read-only `git status` / `log` / `diff` / `show`, plus the
  project's own `test_command` so an analyst can still reproduce a report.
  Anything else is denied without a prompt. Sessions with no project attached
  get no shell at all. `GH_TOKEN` and `GITHUB_TOKEN` are blanked for every
  session, the fix role included, so no agent inherits the GitHub token.
  Fix and release keep a general shell — they have to run builds and installs
  — and SECURITY.md now records that, and the fact that allowlist rules are
  prefix matches and outbound network is not blocked, as accepted residual
  risk rather than leaving it implied. The README, SECURITY.md and the site
  say what each role can run (issue #6).

## [0.8.1] - 2026-08-23

### Fixed

- **The light/dark toggle no longer wraps onto its own line on a phone.**
  At 600px and under the nav links become a full-width second row that
  scrolls sideways, so the brand and the toggle share the first row instead
  of the toggle being pushed below them (issue #26). The mobile screenshots
  in the README and on the site are hand-made and now show the old two-row
  nav as well as the pre-0.4.0 dark palette; there is still no screenshot
  tooling in the repo to regenerate them from.

### Security

- **State-changing requests from other sites are refused.** The dashboard
  has no authentication of its own, so any page open in the operator's
  browser could POST to `/add` — which stores setup and test commands the
  harness later runs as shell. A single `http` middleware now checks where
  a non-safe request came from: it is allowed only if `Sec-Fetch-Site` is
  `same-origin` or `none`, or, where that header is absent, the `Origin`
  host matches `Host`, `X-Forwarded-Host` or `HARNESS_PUBLIC_URL`;
  otherwise it gets a 403. `same-site` is refused along with `cross-site`,
  because on a tailnet another machine's page would otherwise qualify.
  Clients sending neither header — the ntfy action buttons, `curl`, health
  checks — are deliberately still accepted. Checking in middleware rather
  than with per-form tokens means routes added later are covered without
  anyone opting in, and the existing forms and `fetch()` calls needed no
  change. The README and SECURITY.md say so (issue #5).

## [0.8.0] - 2026-08-23

### Added

- **Desks run their cycles concurrently.** The sweep gathers every desk's
  cycle instead of walking them in sequence, so a release on one desk is
  never queued behind another desk's triage backlog. Directive and
  question processing are serialized behind per-event-loop locks (Harry
  never actions the same row twice), SQLite moves to WAL, and an API-limit
  pause or a drain still stops every desk at once.

### Security

- Project-supplied setup/test commands now run in an allowlisted
  environment with no GitHub or Claude credentials and a scratch HOME
  (issue #4, landed on dev by the section, folded into this release).

## [0.7.1] - 2026-08-23

### Changed

- Operator directions reach Harry within a minute of being sent, whatever
  the section is doing. An attendant runs alongside each engineer wave and
  actions pending directions every 20 seconds; the triage loop checks
  between items and the sweep checks before each desk. A direction typed
  mid-cycle used to wait for the whole sweep — 17 minutes at one point
  today.

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
