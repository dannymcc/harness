"""Deterministic orchestration around the agent roles.

The Team Lead plans, ICs execute, but every outward action (push, merge,
comment, release) happens HERE, in plain code, behind the policy gates.
Tests are always re-run deterministically before anything is pushed or
merged — an IC claiming success is never taken on trust.
"""
import asyncio
import json
from datetime import datetime, timezone

from . import agents, config, db, gh, repo
from .agents import AgentStalled
from .gh import CmdError

MAX_AGENT_TASKS_PER_CYCLE = 5


def _login(author) -> str:
    if isinstance(author, dict):
        return author.get("login", "")
    return str(author or "")


def _is_dependabot(author: str) -> bool:
    return "dependabot" in author.lower()


# --- sync -------------------------------------------------------------------

def sync(project) -> None:
    name = project["name"]
    open_now = set()
    for it in gh.list_issues(project["repo"]):
        open_now.add(("issue", it["number"]))
        db.upsert_item(name, "issue", it["number"], it["title"],
                       _login(it.get("author")), "open", it.get("updatedAt", ""))
    for pr in gh.list_prs(project["repo"]):
        if pr.get("isDraft"):
            continue
        open_now.add(("pr", pr["number"]))
        db.upsert_item(name, "pr", pr["number"], pr["title"],
                       _login(pr.get("author")), "open", pr.get("updatedAt", ""))
    # Anything we tracked as open that is no longer open was closed/merged
    # on GitHub (possibly by us, possibly by a human).
    for item in db.project_items(name):
        if item["gh_state"] == "open" and \
                (item["kind"], item["number"]) not in open_now:
            new_status = item["status"]
            if item["status"] not in ("queued", "released"):
                new_status = "closed"
            db.update_item(name, item["kind"], item["number"],
                           gh_state="closed", status=new_status)


# --- issue flow -------------------------------------------------------------

async def triage_item(project, item) -> None:
    name = project["name"]
    detail = gh.issue_detail(project["repo"], item["number"])
    cwd = str(repo.clean_checkout(project, project["dev_branch"]))
    res = await agents.triage_issue(project, detail, cwd)
    if not res["ok"]:
        db.update_item(name, "issue", item["number"], error=res["error"])
        return
    out = res["output"]
    fixable = bool(out["valid"] and out["fixable"]
                   and out["verdict"] in ("bug", "feature"))
    db.update_item(
        name, "issue", item["number"],
        verdict=out["verdict"],
        verdict_summary=out["summary"],
        plan=out.get("plan", ""),
        draft_comment=out.get("draft_comment", ""),
        status="triaged" if fixable else "waiting_human",
        error="",
    )
    if out.get("draft_comment") and db.policy(name, "post_comments") == "auto":
        gh.comment_issue(project["repo"], item["number"], out["draft_comment"])
        db.log_event(f"Posted triage reply on issue #{item['number']}",
                     project=name)
    db.log_event(
        f"Triaged issue #{item['number']}: {out['verdict']}"
        f"{' (fix planned)' if fixable else ''}", project=name)
    if fixable and db.policy(name, "fix_issues") == "auto":
        db.update_item(name, "issue", item["number"], status="approved")


async def fix_item(project, item) -> None:
    name = project["name"]
    detail = gh.issue_detail(project["repo"], item["number"])
    branch = f"harness/issue-{item['number']}"
    cwd = str(repo.create_branch(project, branch, project["dev_branch"]))
    db.update_item(name, "issue", item["number"], status="working",
                   branch=branch)
    res = await agents.fix_issue(project, detail, item["plan"], cwd,
                                 resume=item["session_id"] or None)
    db.update_item(name, "issue", item["number"],
                   session_id=res.get("session_id", ""))
    if not res["ok"] or not res["output"]["success"]:
        msg = res["error"] or res["output"]["notes"] if res["output"] else res["error"]
        db.update_item(name, "issue", item["number"], status="blocked",
                       error=(msg or "fix did not succeed")[:2000])
        db.log_event(f"Fix for issue #{item['number']} blocked: {msg}",
                     "warn", project=name)
        return
    out = res["output"]

    if not repo.has_changes(project, project["dev_branch"]):
        db.update_item(name, "issue", item["number"], status="blocked",
                       error="agent reported success but made no changes")
        return

    # The deterministic gate: harness runs the tests itself.
    passed, test_out = await asyncio.to_thread(repo.run_tests, project)
    if not passed:
        db.update_item(name, "issue", item["number"], status="blocked",
                       error="tests failed after fix:\n" + test_out[-1500:])
        db.log_event(f"Issue #{item['number']}: tests failed, fix not pushed",
                     "warn", project=name)
        return

    msg = out["commit_message"].strip() or f"fix: issue #{item['number']}"
    if f"#{item['number']}" not in msg:
        msg += f" (#{item['number']})"
    repo.commit_all(project, msg)
    stat, diff = repo.diff_stat(project, project["dev_branch"])
    try:
        repo.push_branch_to(project, branch, project["dev_branch"])
    except CmdError as e:
        db.update_item(name, "issue", item["number"], status="blocked",
                       diff=diff, error=f"push to dev failed: {e}"[:2000])
        return
    db.update_item(
        name, "issue", item["number"],
        status="queued", queued_at=db.now(), diff=diff,
        commits=repo.commit_log(project, project["dev_branch"]) or msg,
        verdict_summary=out["summary"], error="")
    db.log_event(
        f"Fixed issue #{item['number']} and pushed to "
        f"{project['dev_branch']} ({stat.strip().splitlines()[-1] if stat.strip() else 'changes'})",
        project=name)
    if item["draft_comment"] or out["summary"]:
        body = (f"A fix for this has been pushed to `{project['dev_branch']}` "
                f"and will be included in the next release.\n\n{out['summary']}")
        if db.policy(name, "post_comments") == "auto":
            gh.comment_issue(project["repo"], item["number"], body)
        else:
            db.update_item(name, "issue", item["number"], draft_comment=body)


# --- PR flow ----------------------------------------------------------------

async def review_item(project, item) -> None:
    name = project["name"]
    detail = gh.pr_detail(project["repo"], item["number"])
    if detail.get("isDraft"):
        db.update_item(name, "pr", item["number"], status="waiting_human",
                       verdict_summary="draft PR - left for the author")
        return
    branch = f"harness/pr-{item['number']}"
    try:
        cwd = str(repo.fetch_pr_branch(project, item["number"], branch))
    except CmdError:
        db.update_item(
            name, "pr", item["number"], status="waiting_human",
            verdict="needs_work",
            verdict_summary=f"does not merge cleanly onto {project['dev_branch']}",
            draft_comment=(f"Thanks for the PR! It currently conflicts with "
                           f"`{project['dev_branch']}` — could you rebase it? "
                           "Happy to take another look after that."))
        return
    passed, test_out = await asyncio.to_thread(repo.run_tests, project)
    diff = gh.pr_diff(project["repo"], item["number"])
    res = await agents.review_pr(project, detail, diff,
                                 ("PASSED\n" if passed else "FAILED\n") + test_out,
                                 cwd)
    if not res["ok"]:
        db.update_item(name, "pr", item["number"], error=res["error"])
        return
    out = res["output"]
    verdict = out["verdict"]
    if verdict == "merge" and not passed:
        verdict = "needs_work"  # agents don't outrank the test suite
    db.update_item(
        name, "pr", item["number"],
        verdict=verdict, verdict_summary=out["summary"],
        draft_comment=out["draft_review"], plan=out["risks"],
        status="triaged", error="")
    db.log_event(f"Reviewed PR #{item['number']}: {verdict}", project=name)

    author = item["author"]
    policy_key = "merge_dependabot" if _is_dependabot(author) else "merge_prs"
    if verdict == "merge" and db.policy(name, policy_key) == "auto":
        await merge_pr_item(project, db.get_item(name, "pr", item["number"]))
    else:
        db.update_item(name, "pr", item["number"], status="waiting_human")
        if verdict in ("needs_work", "reject") and out["draft_review"] and \
                db.policy(name, "post_comments") == "auto":
            gh.comment_pr(project["repo"], item["number"], out["draft_review"])
            db.log_event(f"Posted review on PR #{item['number']}", project=name)


async def merge_pr_item(project, item) -> None:
    """Merge an approved PR into dev (retargeting it there first)."""
    name = project["name"]
    detail = gh.pr_detail(project["repo"], item["number"])
    try:
        if detail.get("baseRefName") != project["dev_branch"]:
            gh.retarget_pr(project["repo"], item["number"], project["dev_branch"])
        gh.merge_pr(project["repo"], item["number"])
    except CmdError as e:
        db.update_item(name, "pr", item["number"], status="blocked",
                       error=f"merge failed: {e}"[:2000])
        db.log_event(f"Merging PR #{item['number']} failed", "warn", project=name)
        return
    db.update_item(name, "pr", item["number"], status="queued",
                   queued_at=db.now(), gh_state="merged", error="")
    db.log_event(f"Merged PR #{item['number']} into {project['dev_branch']}",
                 project=name)
    if item["draft_comment"] and db.policy(name, "post_comments") == "auto":
        gh.comment_pr(project["repo"], item["number"], item["draft_comment"])


# --- release flow -----------------------------------------------------------

def _release_due(project) -> list:
    """Queued items, if the batch thresholds say a release is due."""
    name = project["name"]
    queued = db.items_by_status(name, "queued")
    if not queued:
        return []
    if db.open_release(name):
        return []
    min_changes = int(db.policy(name, "release_min_changes"))
    max_age_days = float(db.policy(name, "release_max_age_days"))
    if len(queued) >= min_changes:
        return queued
    oldest = min(q["queued_at"] or db.now() for q in queued)
    age_days = (datetime.now(timezone.utc)
                - datetime.strptime(oldest, "%Y-%m-%dT%H:%M:%SZ")
                .replace(tzinfo=timezone.utc)).total_seconds() / 86400
    return queued if age_days >= max_age_days else []


async def propose_release(project, queued) -> None:
    name = project["name"]
    cwd = str(repo.clean_checkout(project, project["dev_branch"]))
    version_before = repo.current_version(project)
    log = gh.run(["git", "log", "--oneline",
                  f"origin/{project['main_branch']}..origin/{project['dev_branch']}"],
                 cwd=repo.repo_dir(project))
    res = await agents.draft_release(project, [dict(q) for q in queued],
                                     version_before, log, cwd)
    if not res["ok"]:
        db.log_event(f"Release drafting failed: {res['error']}", "warn",
                     project=name)
        return
    out = res["output"]
    version = out["version"].lstrip("v")
    passed, test_out = await asyncio.to_thread(repo.run_tests, project)
    if not passed:
        db.log_event("Release blocked: tests failing on dev\n" + test_out[-500:],
                     "warn", project=name)
        return
    if repo.has_changes(project, project["dev_branch"]):
        repo.commit_all(project, f"chore: bump version to {version}")
        repo.push_branch_to(project, project["dev_branch"], project["dev_branch"])
    try:
        pr_number = gh.create_pr(
            project["repo"], project["main_branch"], project["dev_branch"],
            f"Release v{version}", out["notes_markdown"])
    except CmdError as e:
        db.log_event(f"Release PR creation failed: {e}", "warn", project=name)
        return
    rid = db.create_release(name, version, out["notes_markdown"],
                            [f"{q['kind']}#{q['number']}" for q in queued])
    db.log_event(f"Proposed release v{version} (PR #{pr_number}, "
                 f"{len(queued)} changes)", project=name)
    db.update_release(rid, pr_number=pr_number)
    if db.policy(name, "cut_release") == "auto":
        finalize_release(project, db.get_release(rid))


def finalize_release(project, release) -> None:
    """Merge the release PR into main and push the version tag."""
    name = project["name"]
    version = release["version"]
    try:
        gh.merge_pr(project["repo"], release["pr_number"], squash=False)
        d = repo.clean_checkout(project, project["main_branch"])
        gh.run(["git", "tag", "-a", f"v{version}", "-m", f"Release v{version}"],
               cwd=d)
        gh.run(["git", "push", "origin", f"v{version}"], cwd=d)
    except CmdError as e:
        db.log_event(f"Release v{version} failed: {e}", "error", project=name)
        return
    try:
        gh.publish_release(project["repo"], f"v{version}", f"v{version}",
                           release["notes"])
    except CmdError as e:
        db.log_event(f"Tag pushed but GitHub release publish failed: {e}",
                     "warn", project=name)
    db.update_release(release["id"], status="released", released_at=db.now())
    for key in json.loads(release["items_json"]):
        kind, number = key.split("#")
        item = db.get_item(name, kind, int(number))
        if item:
            db.update_item(name, kind, int(number), status="released")
            if kind == "issue" and item["gh_state"] == "open":
                try:
                    gh.close_issue(project["repo"], int(number),
                                   f"Fixed in v{version}.")
                except CmdError:
                    pass
    db.log_event(f"Released v{version} 🎉", project=name)


# --- cycle ------------------------------------------------------------------

def _state_digest(project) -> str:
    name = project["name"]
    lines = []
    notes = db.latest_report("notes", name)
    if notes:
        lines.append(f"Desk notes (rolling summary):\n{notes['content']}\n")
    for it in db.project_items(name):
        if it["gh_state"] != "open" and it["status"] != "queued":
            continue
        lines.append(
            f"- {it['kind']}#{it['number']} [{it['status']}] "
            f"{it['title']} (by {it['author']})"
            + (f" — verdict: {it['verdict']}: {it['verdict_summary'][:120]}"
               if it["verdict"] else "")
            + (f" — error: {it['error'][:120]}" if it["error"] else ""))
    queued = db.items_by_status(name, "queued")
    lines.append(f"\nQueued for next release: {len(queued)} change(s). "
                 f"Release policy: >={db.policy(name, 'release_min_changes')} "
                 f"changes or oldest >{db.policy(name, 'release_max_age_days')} days.")
    return "\n".join(lines) or "No open items."


async def run_cycle(project) -> None:
    """One full cycle for one project."""
    name = project["name"]
    sync(project)
    if db.paused_until():
        return

    try:
        # Anything a human approved in the GUI runs first.
        for item in db.items_by_status(name, "approved"):
            if item["kind"] == "issue":
                await fix_item(project, item)
            else:
                await merge_pr_item(project, item)

        # Team Lead plans the rest of the cycle.
        cwd = str(repo.clean_checkout(project, project["dev_branch"]))
        plan_res = await agents.lead_plan(project, _state_digest(project), cwd)
        tasks = plan_res["output"]["tasks"] if plan_res["ok"] else []
        if plan_res["ok"]:
            db.save_report("lead", name, plan_res["output"]["summary"])

        done = 0
        for t in tasks:
            if done >= MAX_AGENT_TASKS_PER_CYCLE:
                break
            item = db.get_item(name, t["kind"], t["number"])
            if item is None or t["action"] == "skip":
                continue
            if t["action"] == "triage" and item["status"] == "new":
                await triage_item(project, item)
                done += 1
                # auto-advance freshly approved fixes in the same cycle
                item = db.get_item(name, t["kind"], t["number"])
                if item["status"] == "approved" and done < MAX_AGENT_TASKS_PER_CYCLE:
                    await fix_item(project, item)
                    done += 1
            elif t["action"] == "fix" and item["status"] in ("triaged", "approved"):
                if db.policy(name, "fix_issues") == "auto" or \
                        item["status"] == "approved":
                    await fix_item(project, item)
                    done += 1
            elif t["action"] == "review" and item["status"] == "new":
                await review_item(project, item)
                done += 1

        # Fallback: triage anything new the lead didn't mention.
        for item in db.items_by_status(name, "new"):
            if done >= MAX_AGENT_TASKS_PER_CYCLE:
                break
            if item["kind"] == "issue":
                await triage_item(project, item)
            else:
                await review_item(project, item)
            done += 1

        queued = _release_due(project)
        if queued:
            await propose_release(project, queued)
    except AgentStalled:
        db.log_event(f"Cycle for {name} paused mid-way; will resume", "warn",
                     project=name)


async def run_all_cycles() -> None:
    projects = db.all_projects(enabled_only=True)
    for p in projects:
        try:
            await run_cycle(p)
        except AgentStalled:
            break
        except Exception as e:
            db.log_event(f"Cycle failed: {type(e).__name__}: {e}", "error",
                         project=p["name"])
    # Harry's cross-project review now happens in the hourly stand-up
    # (run_standup) rather than every sweep — cheaper and more predictable.


# --- hourly stand-up ---------------------------------------------------------

STUCK_WORKING_HOURS = 6


def _unstick_working() -> list[str]:
    """Requeue items stranded in 'working' (e.g. after a crash/restart)."""
    freed = []
    cutoff = (datetime.now(timezone.utc)
              .timestamp() - STUCK_WORKING_HOURS * 3600)
    for p in db.all_projects(enabled_only=True):
        for item in db.items_by_status(p["name"], "working"):
            ts = datetime.strptime(item["updated_at"], "%Y-%m-%dT%H:%M:%SZ") \
                .replace(tzinfo=timezone.utc).timestamp()
            if ts < cutoff:
                db.update_item(p["name"], item["kind"], item["number"],
                               status="approved")
                freed.append(f"{p['name']} {item['kind']}#{item['number']}")
                db.log_event(
                    f"Stand-up: {item['kind']}#{item['number']} was stuck in "
                    f"'working' for over {STUCK_WORKING_HOURS}h — requeued",
                    "warn", project=p["name"])
    return freed


def _standup_digest() -> str:
    now_ts = datetime.now(timezone.utc)

    def age_days(ts: str) -> str:
        try:
            dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ") \
                .replace(tzinfo=timezone.utc)
            return f"{(now_ts - dt).total_seconds() / 86400:.1f}d"
        except (ValueError, TypeError):
            return "?"

    sections = []
    if db.paused_until():
        sections.append(f"NOTE: agent work is paused for API limits until "
                        f"{db.paused_until()}.")
    for p in db.all_projects(enabled_only=True):
        name = p["name"]
        lead = db.latest_report("lead", name)
        lines = [f"## {name} desk (lead: {p['lead_name']}, {p['repo']})"]
        if lead:
            lines.append(f"{p['lead_name']}'s last plan ({age_days(lead['created_at'])} ago): "
                         f"{lead['content'][:300]}")
        counts = db.counts_by_status(name)
        lines.append("Open items by status: " +
                     (", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
                      or "none"))
        for it in db.items_by_status(name, "blocked"):
            lines.append(f"BLOCKED {it['kind']}#{it['number']} "
                         f"({age_days(it['updated_at'])}): {it['error'][:200]}")
        for it in db.items_by_status(name, "waiting_human"):
            if it["gh_state"] == "open":
                lines.append(f"waiting on maintainer {it['kind']}#{it['number']} "
                             f"for {age_days(it['updated_at'])}: "
                             f"{it['verdict']} — {it['title'][:80]}")
        for it in db.items_by_status(name, "queued"):
            lines.append(f"queued for release {it['kind']}#{it['number']} "
                         f"({age_days(it['queued_at'] or it['updated_at'])})")
        fails = [r for r in db.recent_runs(10, name) if r["ok"] == 0]
        for r in fails[:5]:
            lines.append(f"failed run: {r['task']} {r['item_key']} — "
                         f"{r['summary'][:150]}")
        lines.append(f"Spend to date: ${db.total_cost(name):.2f}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections) or "No harnesses configured."


async def run_standup() -> None:
    """Harry's hourly stand-up across every desk."""
    _unstick_working()
    if db.paused_until() or not db.all_projects(enabled_only=True):
        return
    try:
        res = await agents.standup(_standup_digest())
    except AgentStalled:
        return
    if not res["ok"]:
        db.log_event(f"Stand-up failed: {res['error']}", "warn")
        return
    out = res["output"]
    db.save_report("cto", "", out["standup_markdown"])
    for b in out.get("blockers", []):
        db.log_event(f"Stand-up blocker: {b['message']}", "warn",
                     project=b.get("project", ""))
    if out["all_clear"]:
        db.log_event("Stand-up: all clear")
