# Security

## Deployment model

The dashboard has **no authentication by design** — it approves merges,
releases, and public comments, so treat it like a shell on the machine it
runs on. Bind it to loopback (the shipped compose does) and reach it over a
private network: a tailnet/VPN, an SSH tunnel, or an authenticating reverse
proxy. Never expose it to the public internet.

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

## Credentials

The container needs a GitHub token (`repo` scope) and Claude credentials via
environment variables. They are never written to the database or logs. Scope
the GitHub token to the repositories you point Harness at; a fine-grained
PAT is ideal.

## Reporting

Please report vulnerabilities privately via GitHub security advisories
(Security → Report a vulnerability) rather than public issues.
