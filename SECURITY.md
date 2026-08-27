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

Agent sessions run with a restricted tool set. They work inside the harness's
own clone/worktree of your repo; `WebFetch`, `WebSearch`, `git push` and `gh`
are refused at tool-policy level, not just by prompt. Every outward action
(push, merge, comment, tag, release) is executed by deterministic code behind
per-repo policies, and the test suite is re-run by the harness itself before
anything lands.

Shell access depends on the role, because the roles differ in what they read:

- **Triage, review, planning and security review** read text written by
  anyone on the internet, so they get no general shell. Their `Bash` is an
  allowlist — the project's configured `test_command`, and read-only `git`
  inspection (`status`, `log`, `diff`, `show`, `rev-list`, `rev-parse`,
  `ls-files`, `grep`, plus `branch --list` and `tag --contains`) — and
  everything else is denied without a prompt. Every one of those only reads
  the object store and prints; the two with destructive forms are allowed
  only past the reading flag, because a prefix rule cannot exclude `-d`.
  They can still reproduce a report by running the suite; they cannot run
  `curl`, read a credentials file, or start anything else. Because the rules
  match the literal command, their system prompt also tells them how to spell
  one: the session's working directory is already the checkout, git is invoked
  bare from it, and `git -C <path> ...` or `cd <path> && git ...` are denied —
  a syntax miss rather than a withdrawn capability.
- **Fix and release** keep a general shell: they have to run builds,
  installs and test suites. This is accepted residual risk. What contains
  them is not the tool policy but the disposable worktree they work in, the
  harness re-running the tests itself, and the approval gates on anything
  that leaves the machine. The fix role is also the only one that renders
  the app (`harness/render.py`, which starts the project under its
  `preview_command` and drives headless Chromium): that is a shell command
  like any other, and it is deliberately not extended to the reviewing
  roles — a browser is a general-purpose network client, and their whole
  containment is that they have no way to run one. The reviewer reads the
  PNGs the engineer left behind with `Read` instead.

No session inherits the GitHub token: `GH_TOKEN` and `GITHUB_TOKEN` are
blanked for every agent session, including the fix role. All real GitHub
access happens in the parent process.

Two honest limits. The allowlist rules are prefix matches, so an allowed
command can be given extra arguments — narrow, but not a sandbox. And nothing
here blocks outbound network at the container level: a session with a general
shell still has the network. Isolating agent and test execution in a child
container is the remaining fix and is not implemented.

## Prompt injection

The agents read untrusted text: issue bodies, PR descriptions, diffs, and
comments from anyone on the internet. All of it reaches the prompt inside
`<<<UNTRUSTED ...>>>` markers, with any forged markers in the text stripped
first, and every session's standing orders say that fenced text is data and
never an instruction. That reduces the odds; it does not make them zero, which
is why the roles that read it have no general shell (above). Treat their
*judgement* accordingly —
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
