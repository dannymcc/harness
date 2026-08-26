# Changelog

All notable changes to Harness are recorded here. The format is
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.22.0] - 2026-08-26

### Changed

- **What the section cannot do as it stands now goes to Harry, not the
  operator.** A triage verdict of not fixable, an engineer declining the
  work, a run that reported success and changed nothing, a second red test
  run, and a review that is not an auto-merge all used to land the item in
  `waiting_human` — the operator's queue — with nobody in the section having
  ruled on it. Each now holds the item (`held`, the status the circuit
  breaker already used) with a question to Harry on the item's record,
  carrying the verdict, the decline reason or the failing output, and the
  options the harness acts on: **Fix** (or **Merge**, for a PR) / **Skip** /
  **Won't fix**. Harry's ruling moves the item there and then — back into
  the flow, parked, or closed out — through the same routing table the
  operator's answers already use. A held item is not work: nothing
  re-dispatches it and nothing wakes the worker for it until the ruling
  lands. The breaker's semantics apply throughout: one ruling per item, and
  an item that comes back held after Harry has ruled goes to the operator
  instead of round again. That is also the cap on the refusal loop — an
  engineer declining the same item is Harry's call the first time and the
  operator's the second, and never re-approved by the harness in between.
  The operator's own approve, reject and answer buttons still work on a
  held item, and their say-so forgives the trip count as before.

- Two things stay the operator's by design. Ruth's triage schema gains a
  `needs_operator` flag, off by default and reserved for a decision that is
  the maintainer's — product direction, a breaking change, something
  outside the codebase — which still parks the item for them directly; her
  prompt no longer says to "leave it for the maintainer" otherwise. And a
  policy of `fix_issues: approve` or `merge_prs: approve` means the start is
  the operator's click by their own setting, so Harry's "fix" or "merge" on
  such an item lands it on their desk as a recommendation rather than
  starting the work over their gate.

- **Harry's stand-up question is filed once, on the thing it is about.** It
  was filed with no project and no item, so the dedupe never matched, the
  operator's answer routed nowhere, and the same question came back
  rephrased every hour — ten times in two days on one desk. The question
  now names its desk and item from the text (the desk Harry mentions, the
  issue and PR numbers he gives; one item key goes on the record and the
  rest are listed in the text), so the answer moves the item. It is deduped
  by that item — or by the desk, for a question naming no item — for a day
  regardless of wording: one already in front of the operator means nothing
  is filed, and one they have answered within the day is not asked again;
  an event records their ruling instead and the next stand-up digest
  carries it back to Harry as binding. The stand-up and rulings schemas
  gain a required `outside_remit_reason` ("why this is the operator's, not
  yours") that is attached to the question as *Why this is yours*; a
  stand-up question without one is dropped with an event, on the principle
  that a call Harry can make is a directive he did not issue. Both prompts
  now say so in as many words.

- The stand-up digest no longer describes `waiting_human` items as "waiting
  on operator" for Harry to name as blockers: they read as the operator's
  call and not his blocker (or as parked by his own ruling, where that is
  what happened), and the prompt's "work waiting on the operator for days"
  blocker is replaced by "an item held for a ruling you have not given".
  The digest also lists the operator's answers to Harry's own questions
  from the last day.

- The overview and project pages put only escalated questions in the
  operator's action list. Harry's inbox stays visible, folded away with a
  count and read-only — no answer buttons, since answering over his head was
  exactly what put un-escalated questions in front of the operator. The
  project page lists held items under **With Harry** rather than under
  **Awaiting your decision**; their item pages keep the approve button.

- The IC schemas' `question_for_human` no longer says "needing the
  operator's decision": it is a decision from Harry, who escalates to the
  operator only what is genuinely theirs. Harry's own schemas keep the
  operator wording.

## [0.21.3] - 2026-08-26

### Fixed

- **The database schema and migrations now run once per process, not once
  per connection.** Every call to `db.conn()` replayed the whole `CREATE
  TABLE` script and then attempted all ten migrations in turn, relying on
  each one failing with "duplicate column name" to know it had already been
  applied. That is a lot of wasted work on a hot path — the desk opens a
  connection for nearly every read — and it made the errors indistinguishable
  from real ones. The schema pass and the migration walk are now guarded by
  a set of database paths this process has already prepared, so they happen
  once for a given database and are skipped thereafter. The guard is keyed on
  the path rather than a bare flag, so a test suite that points at a fresh
  database per test still gets it migrated; the `journal_mode=WAL` pragma
  stays per-connection, where it belongs; and the path is only recorded after
  a clean pass, so a genuine failure is retried on the next connection rather
  than silently skipped for the life of the process.

## [0.21.2] - 2026-08-26

### Fixed

- **The lead's filing cap now measures backlog rather than filing rate.** The
  limit on how many tracking issues a team lead may open counted every one the
  desk had filed in the last 24 hours, whatever had become of it. An issue
  filed, triaged, fixed and closed before lunch still held its slot for the
  rest of the day, so a desk that worked through its own queue quickly was
  penalised exactly as though the queue were untouched. The cap now counts
  only the lead's filings that are still open and unworked, with no time
  window at all: work something through and its slot comes back. The ceiling
  is unchanged at six per desk, and the note in the log when a filing is
  dropped now gives the real reason.

## [0.21.1] - 2026-08-26

### Fixed

- **The test suite passes on a machine with no global git identity.** The
  two safety-branch tests commit inside fresh clones, which have no
  repo-local `user.name` or `user.email`; on the CI runner that failed with
  exit 128 and had blocked every image build since 0.20.3. The tests now set
  their author and committer through the environment.

## [0.21.0] - 2026-08-26

### Added

- **A project can now release on a clock rather than on a count.** Releases
  have always been set off by the two thresholds — so many queued changes, or
  the oldest queued change turning so many days old — which suits a repo with
  a steady flow of work and suits a quiet one much less well. A new per-repo
  policy, `release schedule`, picks the trigger: `changes` is the pair of
  thresholds and remains the default, so no existing project moves; `daily`,
  `weekly` or `monthly` cuts at most one release a window instead, carrying
  everything queued since the last one, with the two thresholds ignored. The
  window is timed from the last release that actually went out rather than
  from the oldest queued item, so the cut point does not drift with when work
  happened to land. A window with nothing in it passes quietly. A window
  missed because the desk was off, outside its active hours or sitting on red
  tests gives exactly one catch-up release on the next eligible cycle, not a
  run of back-dated ones. **Release now** and Harry's own release proposal
  ignore the cadence and cut immediately either way (issue #69).

- Settings carries the new key with plain-English help and greys out the count
  and age boxes, saying why, when a time schedule is picked; the project page
  and the desk digest describe whichever trigger is live rather than always
  quoting the thresholds. An unrecognised value reads as the default trigger
  rather than as no trigger at all, so a typo in the policy cannot stop
  releases dead.

## [0.20.3] - 2026-08-25

### Fixed

- **The desk's daily cap on tracking issues is now six, and a dropped issue is
  named.** A team lead may open tracking issues from its plan, bounded so a
  planning run cannot flood the repo, but the bound was tuned too low at three
  a day and the per-call slice hard-coded the same number separately, so the
  effective headroom stayed at three even once the constant moved. Both now
  read the one constant, `TRACKING_ISSUES_PER_DAY`, raised to six. When the cap
  does bite, the warn event said only that the lead "wanted to open another
  tracking issue" — what it wanted to file was lost, and the line read as
  noise. The event now quotes the dropped title and says plainly that it was
  not filed, so you can file it yourself or wait for tomorrow. Ordering is
  unchanged: the loop walks the plan's issues in order and stops at the cap, so
  the first-listed issue is the one that survives the last slot (issue #65).

- The operator-facing description of the file-issues policy in Settings still
  said "capped at three a day"; it now matches the code.

## [0.20.2] - 2026-08-25

### Fixed

- **A retried fix no longer throws away what the last attempt wrote.** Every
  dispatch cuts the fix branch fresh from `origin/dev` — `add_worktree` uses
  `git worktree add -B` and clears the old worktree with `--force` — so when a
  fix went back for another go, the previous attempt's commits, and any
  uncommitted work sitting in its worktree, went under the reset without a
  word. `add_worktree` now looks before it resets. Uncommitted changes in the
  outgoing worktree are committed as work-in-progress first; then, if the
  branch tip holds anything `origin/dev` does not already contain, that tip is
  kept on a local `harness/issue-N-attempt-<n>` branch. The sentence saying
  where the work went comes back alongside the worktree and `fix_item` writes
  it to the item thread, so the retry names its predecessor's branch instead of
  leaving you to go looking. Work `origin/dev` already has is not given a ref —
  resetting to it loses nothing — and a tip already sitting on an earlier
  attempt branch is not copied again. Anything other than a clean zero from the
  ahead-count, a git error included, counts as work worth keeping: guessing
  wrong in that direction is what loses commits. A tip that cannot be saved
  raises rather than let the reset go ahead (issue #63).

## [0.20.1] - 2026-08-25

### Fixed

- **A fix that cannot land on dev is now parked on its own remote branch
  instead of being thrown away.** A fix only reached your remote by landing on
  dev. If it could not — the rebase onto a moved dev conflicted, the rebased
  code went red, the push failed for some other reason, or dev kept moving
  through all three attempts — the item was set back to `approved` and retried
  next cycle, and the next dispatch recreated the worktree from `origin/dev`.
  The commit that had already passed the harness's own test run existed only
  on the box, and then it didn't. Every give-up path in
  `repo.push_worktree_to_dev` now force-pushes the commit to
  `harness/issue-N` on your remote first, and the failure reported against the
  item names the branch it went to; the same text is written to the item
  thread, so it survives the truncation on the item's error field. If even
  that push is refused, a second warn event says so plainly — that is the one
  case where the work really is only on this box, and it no longer hides
  inside a truncated error string. A clean land is unchanged: it still goes
  straight to dev and still removes the worktree (issue #64).

## [0.20.0] - 2026-08-25

### Added

- **The section can now close out an item that is already done, instead of
  looping it past you forever.** Work that landed some other way — shipped in
  an earlier release, fixed by hand, superseded — had no way off the board:
  the only terminal presses were **Reject**, which says we are not doing the
  work, and a release sweep. So a finished issue stayed open on GitHub, came
  back through `sync()`, sat in the lead's state digest, and the next plan put
  an engineer back on work that was already done. There is now a close-out
  verb: `pipeline.close_item(project, kind, number, reason)` marks the item
  `closed`, clears its error and session, and — for an issue still open on
  GitHub — closes the issue with a short comment naming the reason, recording
  `gh_state='closed'` locally so it stops reappearing. If the `gh` call is
  refused the board still moves and a warn event says GitHub didn't take. A
  pull request is closed on our board only; closing someone else's PR isn't
  ours to do. You can reach it two ways: a **Close as done** button on the
  item page, next to **Reject** and labelled with the difference, or by
  telling Harry ("close #302, it shipped in v0.38.1") — `close_item` is in his
  directive vocabulary, with the close-is-not-reject distinction spelled out
  in the prompt, so every directive action remains something the GUI could
  already do (issue #60).

## [0.19.0] - 2026-08-25

### Added

- **Every desk now runs on its own clock, so what you do on one repo no longer
  waits on another.** The worker woke the whole section at once: one sweep
  gathered every desk and only came round again when the slowest desk's cycle
  ended, so approving a release on an idle desk could sit there while another
  desk's engineers worked through a wave. Each desk now has its own wake loop —
  its own event, its own interval — inside one long-lived worker loop, with a
  single loop beside them for the section's shared business: Harry's directives
  and rulings, housekeeping, the stand-up clock, and starting a loop for a desk
  newly added or switched on. A click on a desk wakes that desk alone, so the
  merge starts on the press rather than at the end of someone else's wave;
  **Run cycle now** and clearing an API-limit pause still wake everything, as
  before. A desk's loop is its own lock, so two triggers can never put two
  waves through one worktree pool — anything arriving mid-cycle is served on
  the next pass instead. Forcing a cycle is now recorded per desk
  (`force_cycle.{project}`), and a desk that crashes says which desk it was
  (issue #54).

## [0.18.2] - 2026-08-25

### Fixed

- **Answering a question now moves the item it is about, instead of the same
  question coming back unfixed.** An answer was filed against the question and
  put on the item's thread, but nothing acted on it: an issue answered "Fix"
  stayed in `waiting_human`, so the next cycle asked again and the fix never
  started. An answer about an issue or a PR is now an instruction. A fixed
  wording table (`db.ANSWER_ACTIONS`) maps "Fix" — and "go ahead", "do it",
  "merge" — to sign-off, "Skip" to leaving it with you, and "Won't fix" to
  closing it out; nothing is inferred later, and hovering an answer button
  says what it will do. Anything else you type is a message rather than a
  decision, so the item goes back to whoever asked with your answer in front
  of them, rather than sitting unread. The routing runs on the click and again
  at the top of every cycle, which also re-enters items stranded by the old
  behaviour. An answer is a desk event, so the wave runs on the cycle it
  triggered; while it stands, the same question about the same item cannot be
  put to you again for a week — the asker is given your answer instead. An
  item sent back to an agent has its breaker history reset, so old failures
  cannot hold it before the fresh attempt has run (issue #48).

## [0.18.1] - 2026-08-25

### Fixed

- **A refused merge now tells you why, instead of the Merge & tag button
  appearing to do nothing.** When GitHub declined the dev → main merge —
  branch protection, a required check still red, a token without the scope —
  the release was left at `merging` for good: the card showed "this takes
  about a minute, reload for the result" with no button and no cause, and the
  only trace of the refusal was one line in the event log, until a restart
  swept the release up. The failure now puts the release back to `proposed`
  with GitHub's own message stored against it, which the project page shows
  as a warn banner beside the button; a later attempt that succeeds clears
  it. So the press is repeatable, the reason is on the page, and nothing has
  to be restarted to get the release moving again (issue #52).

## [0.18.0] - 2026-08-24

### Added

- **Each stand-up now carries back the blockers Harry named at the last one.**
  He called out blockers with a concrete next step every hour, but nothing fed
  them back, so a blocker raised for the fifth time read as a fresh one and
  could sit there indefinitely. Every desk's blockers are now kept until the
  next stand-up, which reports them back marked *changed* or *unchanged*, with
  how many stand-ups running they have been raised. A blocker that names an
  item is judged on that item alone — its status moving, or runs on it since —
  so a busy desk cannot make a stalled blocker look like progress; one naming
  no item falls back to the desk's own runs and events. The record is written
  after Harry's rulings and directives, so his own bookkeeping never reads as
  movement on the blocker he has just named. His prompt now says a blocker
  still unchanged after two namings must end in a directive, a staffing change
  or an escalation, rather than a third restatement (issue #51).

## [0.17.1] - 2026-08-24

### Fixed

- **A question of Harry's own now reaches you instead of sitting in nobody's
  hands.** His inbox excludes his own rows by construction, so a question he
  asked was one he could never rule on. The stand-up path worked around that
  by escalating just after filing, but the `ask_harry` tool — which every
  session gets, his own runs included — files directly and skipped the
  workaround: those questions stayed `open`, never ruled on, never escalated,
  never in front of the operator, and were logged as the self-dialogue "Harry
  has asked Harry: …". The rule now lives at the single point where questions
  are filed: anything asked by the head of section is filed escalated and
  logged as an escalation, so it goes to your queue and your phone whichever
  path asked it. The activity list drops the derived event as it already does
  for the other two shapes, so the question still appears once (issue #50).

## [0.17.0] - 2026-08-24

### Added

- **A held item now goes to Harry, not straight to you.** Two consecutive
  failed runs trip the circuit breaker, and the trip used to put the item in
  `waiting_human` and page the operator — which contradicts the section's own
  rule that questions go to the head of section first, and asked for a
  decision the section is perfectly able to make. Pressing Fix simply ran the
  same oversized job into the same `error_max_turns` wall and bounced back
  with nobody having asked why. The first trip now holds the item in a new
  `held` status and files a question to Harry carrying both failures' error
  kinds, with the three options he already had: retry it in a fresh session,
  split it — the right call when a run keeps running out of turns, which
  means the item is too big rather than broken, and the answer goes to the
  team lead as a directive — or escalate, which is the only one of the three
  that reaches your phone. Rulings are carried out through the existing
  `retry_item` and `tell_desk` primitives via a new optional `item_action`
  field on his decisions, so every branch is something the GUI could already
  do. The floor under it is a new `items.breaker_trips` column (schema-safe
  `ALTER`, default 0): Harry's retry deliberately keeps the count, so he gets
  one ruling per item and a second trip goes to `waiting_human` and your
  phone whatever he said. Your own approve or retry forgives the count — you
  have looked at the thing. A ruling that gives no direction, and an item
  that goes two ruling passes undecided, both land on your desk rather than
  sitting held with nobody acting (issue #49).

### Fixed

- **Approving a held item resets the circuit-breaker window.** The breaker
  counts trailing failed runs, so re-approving a held item re-tripped it on
  the same stale failures before a single new attempt had run: one desk took
  two overnight session-loss failures, three approvals, three pages and zero
  new runs. A deliberate approval now stamps `breaker_reset_at`
  (schema-safe `ALTER` with a default) — through the GUI approve route and
  through Harry's `approve_item` and `retry_item` — and the failure count
  ignores runs started at or before it. "Re-run from scratch" stamps it too,
  which it did not.
- **"Merge & tag" on a proposed release now merges and tags.** The release
  approve URL also matched the item route registered before it, so every
  press approved a nonexistent item of kind `release` and left the release
  untouched — four presses against a green PR, no merge, no feedback. The
  release routes register first now and the item route refuses kinds it does
  not own; a regression test posts to the URL and asserts the release
  actually finalises (issue #53).

## [0.16.1] - 2026-08-24

### Fixed

- **The read-only roles are now told how to spell a `git` call.** Their shell
  is an allowlist, and an allowlist rule is a prefix match against the literal
  command: `git status` runs, `git -C <path> status` and
  `cd <path> && git status` match nothing and are refused. The refusal the SDK
  returns is generic, so a session that reached for the habitual `git -C` form
  read it as a shell it no longer had, and at least one desk recorded
  "Bash access denied" as a standing blocker that was never there. Readonly
  sessions that actually carry a git allowlist now get a short note in their
  system prompt: the working directory is already the checkout, git is invoked
  bare from it, and a refusal of the `-C` or `cd` forms is a syntax miss to
  retry rather than a lost capability. The allowlist itself is unchanged —
  a rule admitting `git -C` would admit `git -C <path> push` with it — and the
  note stays out of sessions with no shell and out of the fix and release
  roles, which have a general one and may legitimately use both forms. Tests
  pin the guidance, the denied forms, and that the rules were not widened for
  it; SECURITY.md records the same (issue #46).

## [0.16.0] - 2026-08-24

### Added

- **The read-only roles can now inspect git properly.** Triage, review,
  planning and security review get no general shell — only the project's test
  command and a short allowlist of read-only `git`. That list was narrower
  than its own intent: it held `status`, `log`, `diff` and `show`, so a
  planning session could not run
  `git rev-list --left-right --count origin/main...origin/dev`, the ordinary
  way to ask whether dev and main have diverged, nor list branches, search a
  tree, or check which tag contains a commit. `rev-list`, `rev-parse`,
  `ls-files` and `grep` are now allowed outright: none has a destructive
  form, and none can move a ref or reach the network. `branch` and `tag` do
  have destructive forms, and an allowlist rule is a prefix match that cannot
  exclude a flag, so those two stop past the reading flag — `git branch
  --list` and `git tag --contains` only, which covers the read uses without
  admitting `git branch -D` alongside them. Tests pin the new rules, the read
  forms, and that the mutating forms still match nothing; SECURITY.md named
  the old four subcommands explicitly and has been brought back into line
  (issue #43).

## [0.15.2] - 2026-08-24

### Fixed

- **A run that ends without structured output is now recorded as a failure
  rather than crashing the item.** An agent session could finish cleanly by
  the CLI's reckoning and still never call the `StructuredOutput` tool, which
  left the result empty. `run_agent` reported that as a success anyway, and
  every caller reads a success as a promise that there is a result to read —
  so the empty one surfaced as a `TypeError` inside the fix step. The item was
  parked with the raw exception and logged as "could not start", which said
  nothing about the real cause, and two of those in a row tripped the circuit
  breaker. Such a run now finishes as failed with the cause named,
  "session ended without structured output", and the item parks for an
  ordinary retry through the handling that was already there (issue #42).

## [0.15.1] - 2026-08-24

### Fixed

- **Agent sessions now survive a container restart.** The Agent SDK writes its
  session transcripts under `~/.claude`, which in the container was the
  writable layer — so recreating the container threw them away and the next
  cycle's resume died on "No conversation found with session ID", costing a
  cycle and a circuit-breaker count for work that had already been done. At
  boot `~/.claude` is pointed at `data/claude-home`, inside the existing
  `./data` mount, so an interrupted fix picks up its session as intended. No
  compose change is needed, and a real `~/.claude` on a development machine is
  left alone (issue #27).
- A resume against a session that has genuinely gone starts fresh in the same
  run instead of failing the item and waiting for the next cycle. Other
  failures are unchanged — they still requeue as before.

## [0.15.0] - 2026-08-24

### Changed

- **Recent activity now sits behind a tab on the overview.** The full event
  list rendered inline on every load, pushing the repo cards and the section
  roster down the page for the sake of reference material. The default view is
  now the projects and the section; the log is one link away under **recent
  activity**. The tabs are plain links (`/?tab=activity`), so the URL is the
  only state — the view bookmarks, survives a reload, and an unknown value
  gives the default rather than an error. The events query only runs for the
  tab that shows them (issue #25).
- Total spend has left the activity heading for a headline line under the page
  title, so it is visible on whichever tab you are on.

## [0.14.1] - 2026-08-24

### Changed

- **The stand-up no longer takes the middle of the overview.** Harry's hourly
  stand-up card sat open between the roster and recent activity, repeating
  what those two already show and pushing the activity feed below the fold on
  a phone. It is now a disclosure, closed on load and summarised as "Harry —
  stand-up" with its timestamp; the prose is still the only place blockers are
  written up in full, so one tap gets it back. The open state is not
  remembered — expanding it is a per-visit action — and nothing renders at all
  when there is no report yet. Server-side behaviour is unchanged (issue #24).

### Removed

- A dead `harry_report` variable the project page was handed and never used.

## [0.14.0] - 2026-08-24

### Added

- **A live facts strip on the run page.** The numbers that say how a run is
  going — messages so far, model, elapsed time and cost — now sit in a sticky
  strip above the transcript and keep moving while the run does, so they are
  readable on a phone without scrolling back to the top. The tail endpoint the
  console already polls returns those facts with every chunk, including in the
  first seconds before a log file exists, so the strip fills in rather than
  waiting for a reload. Elapsed is the one number worked out in the browser;
  cost stays at the server's figure, which the SDK only reports once the run
  ends, so it reads ≈US$0.00 during the run rather than a guess. When the tail
  reports a finish the strip says so and stops the clock, but leaves the
  succeeded/failed verdict to the page reload a moment later rather than
  inventing one. The run list on the project page now shows the message count
  next to the cost as well (issue #20).

## [0.13.0] - 2026-08-24

### Added

- **Slash commands in the composer.** The box that tasks the section now does
  both jobs: plain prose is still a direction for Harry to judge, and text
  starting with `/` is carried out then and there. `/approve 4`, `/merge pr 8`,
  `/reject 4`, `/release`, `/tell Malcolm skip the probe`, `/stop 12`,
  `/budget 100`, `/policy fix_issues approve`, `/cycle`, `/p may` to jump to a
  desk, and `/?` for the list — which also appears under the box as soon as a
  `/` is typed. Every command dispatches to the same route function the
  buttons use, behind the same policy gate, so nothing here is a new outward
  action: it is a faster way to reach one from a phone. `/merge` is an alias
  of approve, since the approve route already sends an unreviewed PR to a
  tested merge. The desk is the page's on a project page, the composer's
  select on the overview, or named first (`/budget may 100`). A bare number is
  resolved against the desk's items rather than assumed to be an issue,
  because GitHub numbers issues and PRs from one sequence, and `/policy`
  refuses a key or value the settings page would not offer rather than storing
  it silently. Anything a command cannot do — an unknown verb, an agent with
  no run in flight, a release with nothing to cut — is answered in plain text
  under the box and changes nothing; the command itself is logged to the
  project events, so the feed shows what was typed as well as what it did
  (issue #23).

### Changed

- The name a run is shown under is now derived in one place — `config.persona()`
  for the role and task, `db.run_persona()` for a run row — rather than in the
  web layer alone, so `/tell malcolm` matches the name on the staff board and a
  rename lands once.

## [0.12.1] - 2026-08-24

### Added

- **Groundwork for the conversation-first dashboard — nothing on screen yet.**
  `db.stream()` merges the four lists the GUI currently keeps apart — project
  events, the operator's directions, questions on their way to Harry, and the
  item threads — into one chronological feed, newest first, filterable by
  project, kind and timestamp, with the payload an inline decision card will
  need to act on a row. The events that `add_direction` and `ask_question`
  write alongside their own row are dropped from the feed, so a transcript
  built on it won't say everything twice; the two message shapes are now
  module constants shared by the writers and the filter, rather than strings
  in two places waiting to drift apart. No template, route or web test is
  touched, and nothing calls it yet, so the dashboard is unchanged and there
  is no reason to hurry the upgrade. This is the first of four slices of
  issue #22, which stays open as the parent; the version is a patch because
  the release adds no behaviour the operator can see, whatever the parent
  item is labelled.

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
