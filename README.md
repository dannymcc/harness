# Harness

An AI maintainer harness built on the Claude Agent SDK. Harness watches the
GitHub repos you point it at, triages issues, reviews pull requests, fixes
what it safely can, keeps the docs honest, and batches everything into
sensible versioned releases — with you approving the important bits from a
phone-friendly dashboard.

<p align="center">
  <img src="screenshots/overview-mobile.png" alt="Overview dashboard" width="30%">
  <img src="screenshots/project-mobile.png" alt="Project harness view" width="30%">
</p>

## The section

Harness runs a small agent organisation, staffed from Spooks:

| Agent | Role | Scope | Job |
|---|---|---|---|
| **Harry** | head of section (CTO) | all harnesses | Takes an hourly stand-up across every desk: one line per project on whether it's moving, blockers called out, stuck work requeued. He makes the decisions — questions from the team come to him first, and he answers what's in the section's remit, escalating to you only what's genuinely yours. He also runs staffing: hires extra engineers onto busy desks (real fix capacity, max 2 per desk) and stands down people who never see work. |
| **Tom, Adam, Ros, Lucas…** | team leads | one per harness | Read the project state each cycle and plan the work: what to triage, fix, review, and what to deliberately skip. |
| **Ruth** | analyst (IC) | task-based | Triages issues and reviews pull requests. |
| **Malcolm** | technical (IC) | task-based | Writes the fixes, with tests. |
| **Colin** | operations (IC) | task-based | Runs the release cycle: version bump, CHANGELOG.md, release notes with contributor credits, docs check. The pipeline then opens the dev→main PR and, on approval, merges, tags and publishes the GitHub Release. |
| **Zaf** | security (IC) | manually triggered | Read-only security review of a harness's codebase from the project page: auth, injection, uploads, secrets, deployment config. Findings ranked by severity; serious ones raised as warnings. |
| **Tariq** | admin | all harnesses | Hourly housekeeping to minimise token usage: prunes old events/runs/logs/sessions (free, deterministic) and maintains 200-word rolling desk notes per project on a cheap model, which stand in for raw history in every lead/CTO prompt. |

Judgement is agent work; **actions are not**. Every push, merge, comment and
release is executed by deterministic code behind policy gates, and the test
suite is always re-run by the harness itself before anything leaves the
building — an IC claiming success is never taken on trust. Agents are
blocked (by tool policy, not just prompt) from running `git push`, `gh`, or
anything network-facing.

## What it does

- **Issues** — investigates each new issue against the actual code. Valid,
  safely-fixable bugs get fixed on a work branch with tests; the harness
  re-runs the suite and only then pushes to `dev`. Everything else gets a
  drafted reply and waits for you.
- **Pull requests** — merges the PR onto `dev` locally, runs the tests, then
  reviews for *value* (is this worth having?) as well as quality. Verdicts:
  merge / needs work / reject, each with a drafted, courteous review. Nothing
  merges without passing tests, and by default nothing merges without your
  click.
- **Releases are batched.** Fixes and merges queue on `dev`. When the queue
  reaches N changes (default 3) or the oldest change is D days old (default
  7), Harness drafts a release: version bump, changelog, README/docs check,
  then a `dev → main` PR. You approve; it merges and tags; your CI does the
  rest. One fix never means one release.
- **Docs** — fix sessions must update README/docs when user-visible behaviour
  changes, and every release drafting pass re-checks them.
- **Mid-run control** — every agent run has a live transcript page and a
  Stop button; a circuit breaker holds any item after two consecutive
  failed runs rather than retrying forever.
- **Desk memory** — agents bank one-line learnings as they work (shared by
  role: analyst, engineering, lead, ops, security). Memories are recalled
  into every relevant prompt and condensed hourly by Tariq, so judgement
  stays consistent across cycles without prompts growing.
- **Parallel engineers** — each fix runs in its own git worktree, and the
  desk's engineers (Malcolm plus anyone Harry hires) work concurrently as
  one wave per cycle. Landing on dev is serialised: rebase and re-test when
  dev has moved, conflicts held for a human.
- **Housekeeping** — every hour Tariq compacts state: old events and runs
  fold into aggregates, finished items lose their stored diffs and session
  ids, stale transcripts and SDK session files are deleted, and per-project
  desk notes are refreshed (only when there is enough new activity to be
  worth the call — a quiet harness costs nothing to keep tidy).
- **Danny-in-the-loop** — any agent can file a question when a decision
  isn't theirs to make. Harry rules on them at stand-up; only what he
  escalates reaches your queue, where you answer inline (answers flow back
  into every relevant prompt). You can also issue standing directions —
  desk-wide or about a single item — to respond to any report or feedback.
- **Heartbeat** — the worker maintains a heartbeat (touched on every agent
  message); `/health` returns 503 if the worker dies or wedges, the GUI
  shows a warning banner, and the container healthcheck picks it up.
- **Stall handling** — if the Claude API rate-limits or your account hits its
  usage cap, Harness pauses all agent work, records why, and resumes
  automatically the moment the limit resets (parsed from the error where
  possible, exponential backoff otherwise). In-flight fixes resume their
  session rather than starting over.

Costs shown in the GUI are the SDK's API-equivalent estimates (`≈US$`) —
on a subscription plan nothing is billed per token; treat them as a relative
burn meter for your plan's usage limits.

## Policies

Per-harness, editable live in the GUI. `auto` means Harness acts on its own
verdict; `approve` means it prepares everything and waits for your click.

| Policy | Default |
|---|---|
| fix issues (and push to dev) | auto |
| merge community PRs | approve |
| merge dependabot PRs | approve |
| post comments/reviews publicly | approve |
| cut releases | approve |
| release batch size / max age | 3 changes / 7 days |

## Running it

### Locally

```bash
python -m venv venv && . venv/bin/activate
pip install -r requirements.txt
export CLAUDE_CODE_OAUTH_TOKEN=...   # `claude setup-token`, uses your Claude account
export GH_TOKEN=...                  # PAT with repo scope
python run.py                        # GUI on :8300
```

Add your first harness at `/add` (for May: repo `dannymcc/may`, the defaults
match its layout).

### On hyperion

```bash
cd /home/danny/docker/harness
cp .env.example .env   # fill in CLAUDE_CODE_OAUTH_TOKEN + GH_TOKEN
docker compose up -d
tailscale serve --bg --https=443 http://127.0.0.1:8300   # tailnet-only
```

The GUI is mobile-friendly — pin it to your phone's home screen. It has **no
auth of its own**: keep it tailnet-only (never Funnel). If you want a second
layer, put Caddy in front with `forward_auth` to Pocket ID; no app changes
needed.

## Layout

```
harness/
├── config.py     # env-driven global config
├── db.py         # SQLite state: projects, items, runs, releases, reports
├── gh.py         # gh CLI + git wrappers (all GitHub access)
├── repo.py       # per-project clone, branches, deterministic test runs
├── agents.py     # Agent SDK sessions: IC tasks, team lead, CTO
├── pipeline.py   # orchestration + policy gates + release batching
├── worker.py     # background cycle loop, stall-aware scheduling
└── web/          # FastAPI GUI (overview → project → item)
```

State lives in `data/` (SQLite DB, clones, per-run agent logs). Deleting
`data/repos` is always safe — clones are rebuilt on demand.
