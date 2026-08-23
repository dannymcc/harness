# Harness

[!["Buy Me A Coffee"](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://buymeacoffee.com/d3hkz6gwle)

**An AI maintainer for your GitHub repos.** Harness watches the repositories
you point it at, triages issues, reviews pull requests, fixes what it safely
can, keeps the docs honest, and batches everything into sensible versioned
releases — with you approving the important bits from a phone-friendly
dashboard.

Built on the [Claude Agent SDK](https://code.claude.com/docs/en/agent-sdk).
Self-hosted: one Docker container, SQLite, no external services beyond
GitHub, Claude, and (optionally) [ntfy](https://ntfy.sh).

<p align="center">
  <img src="screenshots/overview-mobile.png" alt="Overview dashboard" width="30%">
  <img src="screenshots/project-mobile.png" alt="Project board" width="30%">
</p>

## The section

Harness runs your repos with a small agent organisation, staffed from Spooks:

| Agent | Role | Job |
|---|---|---|
| **Harry** | head of section | Hourly stand-up across every desk. Runs the section through the team leads: blockers become directives, questions come to him first and he escalates to you only what's genuinely yours, staffing requests land on his desk and he decides against spend. |
| **Tom, Adam, Ros…** | team leads (one per repo) | Own execution: plan each cycle, action Harry's directives, assign work, request staffing when the backlog outgrows the desk. |
| **Ruth** | analyst | Triages issues against the actual code; reviews PRs for *value* (is this worth having?) as well as quality. |
| **Malcolm** + hires | engineers | Fix bugs and small features in parallel git worktrees, with tests. |
| **Colin** | operations | Runs the release cycle: version bump, CHANGELOG, credited release notes, GitHub Release. |
| **Tariq** | admin | Hourly housekeeping: prunes state, condenses each desk's rolling memory, keeps token usage down. |
| **Zaf** | security | On-demand security review of a repo, triggered from the dashboard. |

**Judgement is agent work; actions are not.** Every push, merge, comment and
release is executed by deterministic code behind per-repo policies (`auto`
vs `approve`), and the test suite is always re-run by the harness itself
before anything lands — an agent claiming success is never taken on trust.
Agents are tool-blocked from `git push`, `gh`, and the network, not just
told to behave.

## What it does

- **Issues** — investigated against the code. Valid, safely-fixable ones are
  fixed in an isolated git worktree with tests; the harness re-runs the
  suite, then lands the branch on your dev branch (rebasing and re-testing
  if dev moved). Everything else gets a drafted reply and waits for you.
- **Pull requests** — merged onto dev locally, tested, then reviewed for
  value and quality. Verdicts: merge / needs work / reject, each with a
  courteous drafted review. Nothing merges without passing tests; by default
  nothing merges without your click.
- **Batched releases** — fixes and merges queue on dev. At a threshold (N
  changes or age), Colin drafts the release: version bump, CHANGELOG,
  README check, credited notes, then a dev → main PR. You approve; it
  merges, tags, and publishes the GitHub Release. One fix never means one
  release.
- **Operator-in-the-loop** — any agent can ask you a question when a
  decision isn't theirs. Harry rules on what's in the section's remit;
  what he escalates reaches your queue and (via ntfy) your phone — with
  one-tap answer buttons when the question has discrete options. You can
  also issue standing directions, desk-wide or per-item.
- **Mid-run control** — live transcripts for every agent run, a Stop
  button, and a circuit breaker that holds any item after two consecutive
  failures instead of burning retries.
- **Desk memory** — agents bank one-line learnings, recalled into future
  prompts and condensed hourly, so judgement stays consistent without
  prompts growing.
- **Resilience** — API rate/usage limits pause all agent work and resume
  automatically when the limit resets. A worker heartbeat backs `/health`,
  the container healthcheck, and GUI warnings. Active-hours policy keeps
  the section inside your working day if you want it to.

## Quick start

You need Docker, a GitHub token with `repo` scope, and Claude access —
either a [Claude subscription](https://claude.com) (run `claude setup-token`
anywhere Claude Code is installed) or an Anthropic API key.

```bash
git clone https://github.com/dannymcc/harness.git && cd harness
cp .env.example .env    # fill in CLAUDE_CODE_OAUTH_TOKEN (or an API key),
                        # GH_TOKEN, and HARNESS_OPERATOR_NAME (your name)
docker compose up -d
```

Open http://localhost:8300, click **+ add**, and point it at a repo. New
harnesses start conservative: fixes run automatically but land only on your
dev branch; merges, public comments, and releases all wait for your
approval until you loosen the policies in Settings.

**Keep it private.** The dashboard has no authentication — it approves
merges and releases, so treat it like a shell. Bind it to loopback (the
default compose does), and reach it over a tailnet/VPN or behind an
authenticating reverse proxy. Never expose it to the public internet.

### Notifications

Set `HARNESS_NTFY_TOPIC` (and `HARNESS_PUBLIC_URL`, reachable from your
phone) to get pushes for escalated questions, release proposals, held
items, and usage-limit pauses — with one-tap answer buttons. On the public
ntfy.sh server the topic name is effectively a password; pick something
unguessable. The dashboard also installs as a PWA.

## Configuration

Environment (see `.env.example`): `HARNESS_MODEL` (default `claude-opus-5`),
`HARNESS_ADMIN_MODEL` (cheap model for housekeeping), poll interval, per-run
budget cap, timezone, ntfy settings.

Per-repo policies, editable live in Settings:

| Policy | Default |
|---|---|
| fix issues (and land on dev) | auto |
| merge community PRs | approve |
| merge dependabot PRs | approve |
| post comments/reviews publicly | approve |
| cut releases | approve |
| release batch size / max age | 3 changes / 7 days |
| active hours | always |

Repo expectations (all configurable per harness): a dev branch flowing to a
main branch by PR, a version string in a file, and a test command. The
defaults match a Flask-style project (`config.py`, pytest).

Costs shown in the GUI are the SDK's API-equivalent estimates (`≈US$`) — on
a subscription plan nothing is billed per token; read them as a burn meter.

## Development

```bash
python -m venv venv && . venv/bin/activate
pip install -r requirements.txt
export CLAUDE_CODE_OAUTH_TOKEN=... GH_TOKEN=...
python run.py            # GUI + worker on :8300
```

Run the tests with `python -m pytest -q`. State lives in `data/` (SQLite,
clones, worktrees, per-run transcripts). Deleting `data/repos` or
`data/worktrees` is always safe — they're rebuilt.

Harness can maintain itself — add this repo as a harness with version file
`harness/config.py`, version pattern `VERSION\s*=\s*"(?P<version>[^"]+)"`,
and test command `python -m pytest -x -q`. We do.

## Licence

[MIT](LICENSE). Named for the thing that keeps a working animal pointed in
the right direction while it does the pulling.
