# Security

## Deployment model

The dashboard has **no authentication by design** — it approves merges,
releases, and public comments, so treat it like a shell on the machine it
runs on. Bind it to loopback (the shipped compose does) and reach it over a
private network: a tailnet/VPN, an SSH tunnel, or an authenticating reverse
proxy. Never expose it to the public internet.

Because there is no session to check, every state-changing request is instead
checked for where it came from: the app refuses any POST a browser marks as
`cross-site` or `same-site` (`Sec-Fetch-Site`), or whose `Origin` is not this
host, so a page on another site cannot drive the dashboard from the operator's
browser. Clients that send neither header — the ntfy notification action
buttons, `curl`, health checks — are deliberately still accepted; keeping them
working is the point of checking headers rather than issuing form tokens.

## What the agents can and cannot do

Agent sessions run with a restricted tool set: they can read and edit files
inside the harness's own clone/worktree of your repo and run commands there,
but `git push`, `gh`, and network-facing tools are blocked at tool-policy
level, not just by prompt. Every outward action (push, merge, comment, tag,
release) is executed by deterministic code behind per-repo policies, and the
test suite is re-run by the harness itself before anything lands.

## Prompt injection

The agents read untrusted text: issue bodies, PR descriptions, diffs, and
comments from anyone on the internet. Treat their *judgement* accordingly —
that is why the action layer is deterministic, why tests always re-run, why
merges/comments/releases default to requiring your approval, and why the
circuit breaker holds repeatedly-failing items. Keeping `merge_prs` and
`post_comments` on `approve` is the recommended posture for public repos.

## Running untrusted PR code

Reviewing a community PR means running the contributor's code: the harness
merges the PR onto your dev branch and runs the project's `setup_command` and
`test_command` over the result, because an agent's word that the suite passes
is worth nothing. Both commands come from the PR's own tree, so anyone who can
open a PR can choose what runs.

Two things contain that:

- **No credentials in scope.** Project-supplied commands get a built
  environment — `PATH`, locale, `TERM`, `TMPDIR` — and nothing else.
  The GitHub token and the Claude credentials are not inherited, and `HOME`
  points at scratch space, so `~/.config/gh/hosts.yml`, `~/.claude` and
  `~/.git-credentials` are not reachable either. Harness's own git and gh
  calls keep the real environment; nothing else does.
- **A disposable checkout.** PR code is tested in a throwaway clone under
  `data/pr-runs/`, with its own virtualenv and scratch `HOME`, all deleted
  when the review ends. It is not a worktree of harness's clone (a worktree
  shares `.git`), so the PR cannot reach the clone's object store, its hooks,
  or the venv the fix flow reuses.

**Residual risk: this is containment, not a sandbox.** The shipped image runs
the tests as the same user, in the same container, as the harness itself. A
hostile PR can still execute arbitrary code: it has the network, it can write
anywhere that user can write (including `data/`), and it can read anything on
that filesystem that is not a credential in the environment. Isolating the
test step in a child container is the remaining fix and is not implemented.

Until it is, treat the harness's container as the blast radius: give it a
fine-grained token, run it somewhere you would be content to rebuild, and keep
`merge_prs` and `post_comments` on `approve` for public repos.

## Credentials

The container needs a GitHub token (`repo` scope) and Claude credentials via
environment variables. They are never written to the database or logs. Scope
the GitHub token to the repositories you point Harness at; a fine-grained
PAT is ideal.

## Reporting

Please report vulnerabilities privately via GitHub security advisories
(Security → Report a vulnerability) rather than public issues.
