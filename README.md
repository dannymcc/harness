# Wilman

An AI maintainer harness built on the Claude Agent SDK. Wilman watches the
GitHub repos you point it at, triages issues, reviews pull requests, fixes
what it safely can, keeps the docs honest, and batches everything into
sensible versioned releases — with you approving the important bits from a
phone-friendly dashboard.

Named after Andy Wilman: the producer who managed May (and the rest of them).

## The org chart

Wilman runs a small agent organisation:

| Role | Scope | Job |
|---|---|---|
| **CTO** | all harnesses | Reviews every project each sweep, writes the overview status report, escalates anything stuck, failing repeatedly, or burning cost. |
| **Team Lead** | one per harness | Reads the project state each cycle and plans the work: what to triage, fix, review, and what to deliberately skip. |
| **ICs** | task-based | Do the actual work: triage an issue, write a fix, review a PR, draft a release. |

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
  7), Wilman drafts a release: version bump, changelog, README/docs check,
  then a `dev → main` PR. You approve; it merges and tags; your CI does the
  rest. One fix never means one release.
- **Docs** — fix sessions must update README/docs when user-visible behaviour
  changes, and every release drafting pass re-checks them.
- **Stall handling** — if the Claude API rate-limits or your account hits its
  usage cap, Wilman pauses all agent work, records why, and resumes
  automatically the moment the limit resets (parsed from the error where
  possible, exponential backoff otherwise). In-flight fixes resume their
  session rather than starting over.

## Policies

Per-harness, editable live in the GUI. `auto` means Wilman acts on its own
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
cd /home/danny/docker/wilman
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
wilman/
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
