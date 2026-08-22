"""Claude Agent SDK sessions.

Three roles:
  * ICs        - task runs: triage an issue, fix a bug, review a PR, draft a
                 release. They work inside harness's clone of the project.
  * Team Lead  - one per project: reads the project's state and produces the
                 work plan for the cycle (what to do, in what order, and what
                 to skip).
  * CTO        - one across all projects: reviews every harness, escalates
                 stuck work, and writes the status report for the overview
                 dashboard.

Safety model: agents never push, merge, comment on GitHub, or tag. They only
read, edit files in harness's clone, and run tests. All outward actions are
performed deterministically by pipeline.py, subject to the per-project policy
gates. Enforced belt-and-braces: prompts say so, and disallowed_tools blocks
git push / gh / network use inside sessions.
"""
import json
import re
import time
from datetime import datetime, timezone, timedelta

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
)

from . import config, db

IC_TOOLS = ["Read", "Glob", "Grep", "Edit", "Write", "Bash", "TodoWrite"]
READONLY_TOOLS = ["Read", "Glob", "Grep", "Bash", "TodoWrite"]
BLOCKED = [
    "Bash(git push*)", "Bash(gh *)", "Bash(git remote*)",
    "WebFetch", "WebSearch", "Task",
]

BASE_RULES = """
You are part of Harness, an automated maintainer for open-source projects.
Ground rules, non-negotiable:
- You work only inside the provided checkout. NEVER run `git push`, `gh`,
  or anything that talks to GitHub or the network. The harness handles all
  of that after you finish.
- Be honest in your structured output. If you are not confident, say so;
  a wrong "success" is far worse than a "needs a human".
- British English, plain and understated, in anything user-facing.
- Never invent facts about the project. Read the code before concluding.
- Every schema has an optional question_for_danny field. Use it when you
  need a decision you cannot make yourself. It goes to Harry first, who
  either decides or escalates to Danny, the maintainer. One short, specific
  question; empty string otherwise. Never re-ask something already listed
  as waiting.
"""


# --- stall detection ---------------------------------------------------------

STALL_MARKERS = (
    "rate limit", "rate_limit", "usage limit", "usage_limit", "overloaded",
    "429", "insufficient credit", "credit balance", "quota", "529",
)


def _stall_reset_time(text: str) -> str:
    """Return an ISO time to resume at, parsed from the error if possible."""
    # Claude usage-limit errors often carry a unix reset timestamp.
    m = re.search(r"(?:resets? at\D*|\|)(\d{10})", text)
    if m:
        ts = datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
        if ts > datetime.now(timezone.utc):
            return ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    # Otherwise exponential backoff: 15m, 30m, 1h, 2h, 4h (cap).
    count = int(db.get_setting("backoff_count", "0"))
    delay = min(15 * (2 ** count), 240)
    db.set_setting("backoff_count", str(count + 1))
    ts = datetime.now(timezone.utc) + timedelta(minutes=delay)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_stall(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in STALL_MARKERS)


class AgentStalled(RuntimeError):
    """API rate/usage limit hit; work is paused and will resume later."""


# --- core runner -------------------------------------------------------------

async def run_agent(*, project_name: str, role: str, item_key: str, task: str,
                    prompt: str, cwd: str | None, schema: dict,
                    readonly: bool = False, resume: str | None = None,
                    model: str | None = None, persona: str = "") -> dict:
    """Run one agent session, log it, and return its structured output.

    Returns {"ok": bool, "output": dict|None, "session_id": str, "error": str}.
    Raises AgentStalled after registering a global pause on rate/usage limits.
    """
    if db.paused_until():
        raise AgentStalled("paused for API limits")

    mdl = model or config.MODEL
    if not persona:
        persona = (config.CTO_NAME if role == "cto"
                   else config.ADMIN_NAME if role == "admin"
                   else config.IC_NAMES.get(task, ""))
    run_id = db.start_run(project_name, role, item_key, task, mdl, persona)
    options = ClaudeAgentOptions(
        model=mdl,
        cwd=cwd,
        allowed_tools=READONLY_TOOLS if readonly else IC_TOOLS,
        disallowed_tools=BLOCKED,
        permission_mode="dontAsk",
        system_prompt=BASE_RULES,
        max_turns=config.MAX_TURNS,
        max_budget_usd=config.MAX_BUDGET_USD_PER_RUN,
        output_format={"type": "json_schema", "schema": schema},
        setting_sources=[],
        resume=resume,
    )

    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = config.LOG_DIR / f"run-{run_id}.log"
    session_id, cost, turns, result = "", 0.0, 0, None
    try:
        with open(log_path, "w") as log:
            log.write(f"# {task} {item_key} ({role})\n\n{prompt}\n\n---\n\n")
            async for message in query(prompt=prompt, options=options):
                db.touch_heartbeat()
                if isinstance(message, AssistantMessage):
                    turns += 1
                    for block in message.content:
                        text = getattr(block, "text", None)
                        if text:
                            log.write(text + "\n")
                elif isinstance(message, ResultMessage):
                    result = message
                    session_id = getattr(message, "session_id", "") or ""
                    cost = message.total_cost_usd or 0.0
    except Exception as e:  # SDK/process/transport failures
        err = f"{type(e).__name__}: {e}"
        db.finish_run(run_id, False, cost, turns, err, str(log_path))
        if _check_stall(err):
            db.pause_until(_stall_reset_time(err), err[:300])
            raise AgentStalled(err) from e
        return {"ok": False, "output": None, "session_id": session_id, "error": err}

    if result is None:
        db.finish_run(run_id, False, cost, turns, "no result message", str(log_path))
        return {"ok": False, "output": None, "session_id": session_id,
                "error": "session produced no result"}

    err_text = getattr(result, "result", "") or ""
    if result.subtype != "success":
        summary = f"{result.subtype}: {err_text[:200]}"
        db.finish_run(run_id, False, cost, turns, summary, str(log_path))
        if _check_stall(err_text):
            db.pause_until(_stall_reset_time(err_text), err_text[:300])
            raise AgentStalled(err_text)
        return {"ok": False, "output": None, "session_id": session_id,
                "error": summary}

    db.set_setting("backoff_count", "0")  # healthy run resets the backoff
    output = result.structured_output
    summary = ""
    if isinstance(output, dict):
        summary = str(output.get("summary", ""))[:300]
    db.finish_run(run_id, True, cost, turns, summary, str(log_path))
    return {"ok": True, "output": output, "session_id": session_id, "error": ""}


# --- IC schemas & prompts ----------------------------------------------------

TRIAGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "valid", "fixable", "summary", "plan",
                 "draft_comment"],
    "properties": {
        "verdict": {"type": "string",
                    "enum": ["bug", "feature", "question", "duplicate",
                             "invalid", "spam"]},
        "valid": {"type": "boolean",
                  "description": "Is this a genuine, reproducible/actionable report?"},
        "fixable": {"type": "boolean",
                    "description": "Could an automated fix be attempted safely?"},
        "summary": {"type": "string",
                    "description": "2-3 sentences: what this is and your assessment."},
        "question_for_danny": {"type": "string", "description": "Optional: one question needing Danny's decision, else empty."},
        "plan": {"type": "string",
                 "description": "If fixable: concrete fix plan with files. Else empty."},
        "draft_comment": {"type": "string",
                          "description": "Reply to post on the issue (may be empty)."},
    },
}

FIX_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["success", "summary", "docs_updated", "commit_message", "notes"],
    "properties": {
        "success": {"type": "boolean",
                    "description": "True only if the fix is complete and tests pass."},
        "summary": {"type": "string"},
        "question_for_danny": {"type": "string",
                               "description": "Optional: one question needing Danny's decision, else empty."},
        "docs_updated": {"type": "boolean",
                         "description": "Whether README/docs needed and got updates."},
        "commit_message": {"type": "string",
                           "description": "Conventional commit message for the change."},
        "notes": {"type": "string",
                  "description": "Anything a human reviewer should know."},
    },
}

REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["verdict", "valuable", "summary", "risks", "draft_review"],
    "properties": {
        "verdict": {"type": "string", "enum": ["merge", "needs_work", "reject"]},
        "valuable": {"type": "boolean",
                     "description": "Is this a worthwhile addition to the product?"},
        "summary": {"type": "string"},
        "question_for_danny": {"type": "string",
                               "description": "Optional: one question needing Danny's decision, else empty."},
        "risks": {"type": "string"},
        "draft_review": {"type": "string",
                         "description": "Polite review comment for the author."},
    },
}

RELEASE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["version", "notes_markdown", "summary"],
    "properties": {
        "version": {"type": "string",
                    "description": "New semver, e.g. 0.28.0 (no leading v)."},
        "notes_markdown": {"type": "string",
                           "description": "Release notes / changelog markdown."},
        "summary": {"type": "string"},
        "question_for_danny": {"type": "string",
                               "description": "Optional: one question needing Danny's decision, else empty."},
    },
}

PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["tasks", "summary"],
    "properties": {
        "summary": {"type": "string",
                    "description": "Team lead's read on the project this cycle."},
        "question_for_danny": {"type": "string",
                               "description": "Optional: one question needing Danny's decision, else empty."},
        "tasks": {
            "type": "array",
            "maxItems": 10,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["action", "kind", "number", "reason"],
                "properties": {
                    "action": {"type": "string",
                               "enum": ["triage", "fix", "review", "skip"]},
                    "kind": {"type": "string", "enum": ["issue", "pr"]},
                    "number": {"type": "integer"},
                    "reason": {"type": "string"},
                },
            },
        },
    },
}

STANDUP_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["standup_markdown", "blockers", "all_clear", "desks",
                 "summary"],
    "properties": {
        "summary": {"type": "string"},
        "question_for_danny": {"type": "string",
                               "description": "Optional: one question needing Danny's decision, else empty."},
        "desks": {
            "type": "array",
            "description": "One entry per project desk.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["project", "status_line", "moving"],
                "properties": {
                    "project": {"type": "string"},
                    "status_line": {"type": "string",
                                    "description": "Harry's one-line read on this desk right now."},
                    "moving": {"type": "boolean",
                               "description": "Is this desk making progress?"},
                },
            },
        },
        "all_clear": {"type": "boolean",
                      "description": "True if every desk is moving and nothing needs the maintainer."},
        "standup_markdown": {"type": "string",
                             "description": "Per-desk one-liners plus anything that needs attention."},
        "blockers": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["project", "message"],
                "properties": {
                    "project": {"type": "string"},
                    "message": {"type": "string"},
                },
            },
        },
        "decisions": {
            "type": "array",
            "description": "Rulings on the open questions listed in the digest.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["question_id", "action", "answer"],
                "properties": {
                    "question_id": {"type": "integer"},
                    "action": {"type": "string",
                               "enum": ["answer", "escalate"]},
                    "answer": {"type": "string",
                               "description": "Your ruling (empty when escalating)."},
                },
            },
        },
        "staffing": {
            "type": "array",
            "description": "Optional staffing changes based on utilisation.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["project", "action", "name", "reason"],
                "properties": {
                    "project": {"type": "string"},
                    "action": {"type": "string",
                               "enum": ["hire", "stand_down", "reinstate"]},
                    "name": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
    },
}

CTO_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["report_markdown", "escalations", "summary"],
    "properties": {
        "summary": {"type": "string"},
        "question_for_danny": {"type": "string",
                               "description": "Optional: one question needing Danny's decision, else empty."},
        "report_markdown": {"type": "string",
                            "description": "Concise cross-project status report."},
        "escalations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["project", "message"],
                "properties": {
                    "project": {"type": "string"},
                    "message": {"type": "string"},
                },
            },
        },
    },
}


def _danny_answers(project_name: str, item_key: str) -> str:
    rows = db.answers_for(project_name, item_key)
    if not rows:
        return ""
    lines = ["\nDanny has already decided the following about this item:"]
    lines += [f"- Q: {r['question'][:150]}\n  A: {r['answer'][:250]}" for r in rows]
    return "\n".join(lines) + "\n"


async def triage_issue(project, issue: dict, cwd: str) -> dict:
    prompt = f"""You are Ruth, the section's analyst.
Triage this GitHub issue for {project['repo']}. You are in a
clean checkout of the {project['dev_branch']} branch. Investigate properly:
read the relevant code, try to reproduce the claim where practical.

Issue #{issue['number']}: {issue['title']}
Author: {issue['author']['login'] if isinstance(issue.get('author'), dict) else issue.get('author', '')}

{issue.get('body', '')}

Comments:
{json.dumps([{'author': c.get('author', {}).get('login', ''), 'body': c.get('body', '')} for c in issue.get('comments', [])], indent=2)[:6000]}

Assess: is it valid? A bug or a feature request? Could Harness fix it safely
(small, well-understood change with test coverage)? Feature requests are only
"fixable" when they are small, clearly specified, and an obvious product fit —
otherwise leave them for the maintainer. Write a draft reply for the issue
where a reply would help (asking for missing info, explaining a
misunderstanding, or confirming the plan). Do not modify any files.
{_danny_answers(project["name"], f"issue#{issue['number']}")}"""
    return await run_agent(
        project_name=project["name"], role="ic",
        item_key=f"issue#{issue['number']}", task="triage",
        prompt=prompt, cwd=cwd, schema=TRIAGE_SCHEMA, readonly=True)


async def fix_issue(project, issue: dict, plan: str, cwd: str,
                    resume: str | None = None,
                    persona: str = "Malcolm") -> dict:
    prompt = f"""You are {persona}, one of the section's engineers.
Fix this issue in {project['repo']}. You are on a work branch
off {project['dev_branch']} in harness's checkout. The triage plan is below —
verify it against the code before following it.

Issue #{issue['number']}: {issue['title']}

{issue.get('body', '')}

Triage plan:
{plan}

Requirements:
- Follow the project's existing style and conventions (read CLAUDE.md if present).
- Add or update tests that cover the fix.
- Run the test suite ({project['test_command']}) and make it pass.
- Update README/docs if behaviour visible to users changed.
- Do NOT commit, push, or touch git config — leave changes in the working tree.
- If the fix is riskier or larger than the plan suggested, stop and report
  success=false with an explanation rather than forcing it.
{_danny_answers(project["name"], f"issue#{issue['number']}")}"""
    return await run_agent(
        project_name=project["name"], role="ic",
        item_key=f"issue#{issue['number']}", task="fix",
        prompt=prompt, cwd=cwd, schema=FIX_SCHEMA, resume=resume,
        persona=persona)


async def review_pr(project, pr: dict, diff: str, test_result: str,
                    cwd: str) -> dict:
    checks = json.dumps(pr.get("statusCheckRollup") or [], indent=2)[:3000]
    prompt = f"""You are Ruth, the section's analyst.
Review this pull request to {project['repo']} as a careful maintainer. You are in a checkout with the PR already merged onto
{project['dev_branch']} so you can read the combined result.

PR #{pr['number']}: {pr['title']}
Author: {pr['author']['login'] if isinstance(pr.get('author'), dict) else ''}
Base: {pr.get('baseRefName')} | +{pr.get('additions')} -{pr.get('deletions')} in {pr.get('changedFiles')} files

Description:
{pr.get('body', '')[:4000]}

Diff:
{diff}

Local test run of the merged result:
{test_result[-4000:]}

CI checks: {checks}

Judge two things separately:
1. Value: is this a worthwhile addition to the product — coherent with its
   direction, not bloat, not something better done differently? Being
   well-written does not make a change worth merging.
2. Quality: correctness, tests, migrations, security, style, docs. Check the
   diff against the actual codebase, not just on its own.

Verdict "merge" only when you'd stake the release on it. "needs_work" with a
courteous, specific draft_review when the idea is good but the execution
isn't there. "reject" when it doesn't belong, with a kind explanation. Do not
modify any files.
{_danny_answers(project["name"], f"pr#{pr['number']}")}"""
    return await run_agent(
        project_name=project["name"], role="ic",
        item_key=f"pr#{pr['number']}", task="review",
        prompt=prompt, cwd=cwd, schema=REVIEW_SCHEMA, readonly=True)


async def draft_release(project, queued_items: list, current_version: str,
                        commit_log: str, cwd: str) -> dict:
    items_txt = "\n".join(
        f"- {i['kind']}#{i['number']}: {i['title']} ({i['verdict']})"
        for i in queued_items)
    prompt = f"""You are Colin, the section's operations specialist.
Prepare a release of {project['repo']}. You are on the
{project['dev_branch']} branch in harness's checkout.

Current version: {current_version}
Changes queued for this release:
{items_txt}

Commits on {project['dev_branch']} since the last release:
{commit_log[:6000]}

Tasks:
1. Choose the next version (semver: features -> minor bump, fixes only ->
   patch bump).
2. Update the version in {project['version_file']} (pattern:
   {project['version_pattern']}).
3. Check README and docs are accurate for everything in this release; fix
   anything stale.
4. Update CHANGELOG.md (create it in Keep-a-Changelog style if the project
   doesn't have one): add this version's section with today's date.
5. Write clear, understated release notes grouped by features / fixes /
   other, crediting community contributors by GitHub handle where their
   PRs or reports are included (e.g. "thanks @user").
Do NOT commit, push or tag — leave the working tree changes in place."""
    return await run_agent(
        project_name=project["name"], role="ic",
        item_key="release", task="release",
        prompt=prompt, cwd=cwd, schema=RELEASE_SCHEMA)


NOTES_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["notes_markdown", "summary"],
    "properties": {
        "notes_markdown": {"type": "string",
                           "description": "The updated rolling desk notes."},
        "summary": {"type": "string"},
        "question_for_danny": {"type": "string",
                               "description": "Optional: one question needing Danny's decision, else empty."},
    },
}


async def compact_notes(project_name: str, old_notes: str,
                        new_events: str) -> dict:
    """Tariq folds recent activity into short rolling desk notes.

    The notes replace raw history in the team lead and CTO prompts, which is
    where the token saving happens. Runs on the cheap admin model.
    """
    prompt = f"""You are Tariq, the section's admin. Maintain the rolling desk
notes for the {project_name} harness. Fold the new activity below into the
existing notes: keep decisions, recurring problems, community context and
anything a team lead would need; drop routine noise and anything now stale.
Hard limit 200 words — these notes exist to keep prompts small.

Existing notes:
{old_notes or '(none yet)'}

New activity since the notes were last updated:
{new_events}"""
    return await run_agent(
        project_name=project_name, role="admin",
        item_key="", task="notes",
        prompt=prompt, cwd=None, schema=NOTES_SCHEMA, readonly=True,
        model=config.ADMIN_MODEL)


SECURITY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["report_markdown", "findings", "summary"],
    "properties": {
        "summary": {"type": "string"},
        "question_for_danny": {"type": "string",
                               "description": "Optional: one question needing Danny's decision, else empty."},
        "report_markdown": {"type": "string",
                            "description": "Full security review report."},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "title", "location", "detail"],
                "properties": {
                    "severity": {"type": "string",
                                 "enum": ["critical", "high", "medium",
                                          "low", "info"]},
                    "title": {"type": "string"},
                    "location": {"type": "string",
                                 "description": "file:line or component"},
                    "detail": {"type": "string",
                               "description": "What, why it matters, and the fix."},
                },
            },
        },
    },
}


async def security_review(project, cwd: str) -> dict:
    prompt = f"""You are Zaf, running a security review of {project['repo']}.
You are in a clean read-only checkout of the {project['dev_branch']} branch.

Review the codebase as a defender: authentication and session handling,
authorisation on every route, injection (SQL/command/template), file upload
and path handling, secrets in the repo or logs, CSRF/XSS, dependency red
flags, Docker/deployment configuration, and anything security-relevant in
recent changes (git log will show you what moved lately).

Report only genuine findings with concrete locations — no boilerplate
checklists, no credit for things done well beyond a sentence in the summary.
Rank by severity honestly; a self-hosted app behind a home network is still
allowed to have real vulnerabilities. For each finding give the fix you
would make. Do not modify any files."""
    return await run_agent(
        project_name=project["name"], role="ic",
        item_key="", task="security",
        prompt=prompt, cwd=cwd, schema=SECURITY_SCHEMA, readonly=True)


# --- Team Lead ---------------------------------------------------------------

async def lead_plan(project, state_digest: str, cwd: str) -> dict:
    prompt = f"""You are {project['lead_name']}, the team lead running the
{project['repo']} desk in the Harness harness. Your officers can: triage
issues (Ruth), fix triaged bugs (Malcolm), and review PRs (Ruth). The
harness (not you) handles merges, comments and releases behind policy gates.

Current project state:
{state_digest}

Produce this cycle's work plan: up to 10 tasks, most important first.
Prioritise: regressions and data-loss bugs, then community PRs waiting on
review (contributors deserve timely answers), then ordinary bugs, then small
feature requests. Use "skip" with a reason for open items deliberately not
worth agent time this cycle. Only reference issue/PR numbers from the state
digest above."""
    return await run_agent(
        project_name=project["name"], role="lead",
        item_key="", task="plan",
        prompt=prompt, cwd=cwd, schema=PLAN_SCHEMA, readonly=True)


# --- CTO ---------------------------------------------------------------------

async def standup(digest: str) -> dict:
    """Hourly stand-up: Harry hears every desk, checks nothing is stuck."""
    prompt = f"""You are Harry, head of section, taking the hourly stand-up.
Every desk's status is below: the team lead's latest plan, what's in
progress, what's blocked and why, how long things have been waiting, and
recent failures and spend.

{digest}

You make the decisions. The digest lists open questions from your people,
each with an id. Rule on each: answer it yourself when it is within the
section's remit (engineering judgement, priorities, process). Escalate to
Danny only what is genuinely his — product direction, breaking changes,
anything with consequences outside the codebase. Your answers reach the
team automatically.

You also run staffing. The utilisation figures show how busy each desk's
people are. If a desk has a backlog of fixable work, hire an extra engineer
onto it from the available pool (they genuinely increase how many fixes run
per cycle; max {config.MAX_EXTRA_ENGINEERS} extra per desk). If someone has
had no work for a week or more, stand them down — it keeps the board honest;
they can be reinstated any time. Only make changes utilisation justifies.

Run the stand-up: one line per desk on whether it's moving. Then call out
anything genuinely stuck — an item blocked for a reason nobody is acting on,
work waiting on the maintainer for days, repeated failures, unusual spend —
as blockers, each with a concrete next step. Set all_clear only if there is
truly nothing needing attention. Be brief; this happens every hour."""
    return await run_agent(
        project_name="", role="cto",
        item_key="", task="standup",
        prompt=prompt, cwd=None, schema=STANDUP_SCHEMA, readonly=True)


async def cto_review(digest: str) -> dict:
    prompt = f"""You are Harry, head of section, overseeing every Harness
harness (one per project, each run by a team lead). Below is the state of
every project: queues, blocked items, recent failures, costs, and pending
human approvals.

{digest}

Write a concise status report (markdown) for the maintainer's overview
dashboard: what shipped, what's blocked and why, what needs their decision,
notable community activity, and spend. Raise an escalation for anything
stuck more than a few days, repeatedly failing, or burning unusual cost.
Keep it short and plain — a busy person should get the picture in twenty
seconds."""
    return await run_agent(
        project_name="", role="cto",
        item_key="", task="cto_review",
        prompt=prompt, cwd=None, schema=CTO_SCHEMA, readonly=True)
