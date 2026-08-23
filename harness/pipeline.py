"""Deterministic orchestration around the agent roles.

The Team Lead plans, ICs execute, but every outward action (push, merge,
comment, release) happens HERE, in plain code, behind the policy gates.
Tests are always re-run deterministically before anything is pushed or
merged — an IC claiming success is never taken on trust.
"""
import asyncio
import json
from datetime import datetime, timezone

from . import agents, config, db, gh, repo, notify
from .agents import AgentStalled
from .gh import CmdError

MAX_AGENT_TASKS_PER_CYCLE = 5
TRACKING_ISSUES_PER_DAY = 3   # per desk; a lead filing issues is bounded work


def within_active_hours(name: str) -> bool:
    """True when the project's active_hours policy allows agent work now."""
    val = db.policy(name, "active_hours").strip().lower()
    if val in ("", "always", "24/7"):
        return True
    try:
        start_s, end_s = val.split("-", 1)
        start, end = int(start_s), int(end_s)
    except ValueError:
        return True  # unparseable policy never silences the section
    from zoneinfo import ZoneInfo
    hour = datetime.now(ZoneInfo(config.TIMEZONE)).hour
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # overnight range like 22-06
BREAKER_THRESHOLD = 2  # consecutive failed runs before an item is held


def _breaker_tripped(project, item) -> bool:
    """Hold items that keep failing instead of burning retries forever."""
    name = project["name"]
    key = f"{item['kind']}#{item['number']}"
    if db.consecutive_failures(name, key) >= BREAKER_THRESHOLD:
        db.update_item(name, item["kind"], item["number"],
                       status="waiting_human",
                       error=f"circuit breaker: {BREAKER_THRESHOLD} consecutive "
                             "failed runs — held for a human decision")
        db.log_event(f"Circuit breaker held {key} after repeated failures",
                     "warn", project=name)
        notify.send(f"Held: {key} ({name})",
                    "Two consecutive failed runs — needs your look.",
                    tags="warning", click_path=f"/p/{name}/{item['kind']}/{item['number']}")
        return True
    return False


PERSONA_MEMORY_KEY = {"Ruth": "analyst", "Malcolm": "engineering",
                      "Colin": "ops", "Zaf": "security"}


def _file_question(project_name: str, asked_by: str, item_key: str,
                   out: dict | None) -> None:
    if not out:
        return
    if out.get("question_for_human"):
        qid = db.ask_question(project_name, asked_by, item_key,
                              out["question_for_human"],
                              options=out.get("question_options"))
        if qid and asked_by == config.CTO_NAME:
            # Harry can't rule on his own question — it is the operator's.
            db.escalate_question(qid)
    if out.get("memory_note") and project_name:
        key = PERSONA_MEMORY_KEY.get(asked_by)
        if key is None:
            # team leads and hired engineers map by role
            key = "lead" if asked_by and asked_by[0].isupper() and                 db.get_project(project_name) and                 asked_by == db.get_project(project_name)["lead_name"]                 else "engineering"
        db.append_memory(project_name, key, out["memory_note"])


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
    if _breaker_tripped(project, item):
        return
    detail = gh.issue_detail(project["repo"], item["number"])
    with repo.clone_lock(project):
        cwd = str(repo.clean_checkout(project, project["dev_branch"]))
        res = await agents.triage_issue(project, detail, cwd)
    if not res["ok"]:
        db.update_item(name, "issue", item["number"], error=res["error"])
        return
    out = res["output"]
    _file_question(name, "Ruth", f"issue#{item['number']}", out)
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
    # Under "lead" the team lead's next plan is the sign-off; under
    # "approve" the operator's click is. Either way it stays "triaged".


async def fix_item(project, item, persona: str = "Malcolm") -> None:
    """Fix one issue in its own git worktree (safe to run concurrently)."""
    name = project["name"]
    if _breaker_tripped(project, item):
        return
    detail = gh.issue_detail(project["repo"], item["number"])
    branch = f"harness/issue-{item['number']}"
    wt = repo.add_worktree(project, branch)
    db.update_item(name, "issue", item["number"], status="working",
                   branch=branch)
    res = await agents.fix_issue(project, detail, item["plan"], str(wt),
                                 resume=item["session_id"] or None,
                                 persona=persona)
    db.update_item(name, "issue", item["number"],
                   session_id=res.get("session_id", ""))
    if not res["ok"] or not res["output"]["success"]:
        msg = (res["error"] or res["output"]["notes"]) if res["output"] \
            else res["error"]
        msg = (msg or "fix did not succeed")[:2000]
        if res.get("cancelled"):
            # A human pressed Stop — never auto-retry over their decision.
            db.update_item(name, "issue", item["number"],
                           status="waiting_human", error=msg)
        elif res["ok"] and res["output"] and not res["output"]["success"]:
            # The engineer deliberately declined (too risky, unclear spec).
            # Retrying repeats the same honest refusal at full cost.
            db.update_item(name, "issue", item["number"],
                           status="waiting_human", error=msg)
            db.log_event(f"{persona} declined issue #{item['number']}: "
                         f"{msg[:120]} — needs a human call", "warn",
                         project=name)
        else:
            # Mechanical failure (crash, timeout, transport): retry next
            # cycle; the circuit breaker holds it after two in a row.
            db.update_item(name, "issue", item["number"], status="approved",
                           error=msg)
            db.log_event(f"Fix for issue #{item['number']} failed "
                         f"({msg[:100]}) — will retry next cycle", "warn",
                         project=name)
        return
    out = res["output"]
    _file_question(name, persona, f"issue#{item['number']}", out)

    if not repo.wt_has_changes(project, wt):
        db.update_item(name, "issue", item["number"], status="waiting_human",
                       error="agent reported success but made no changes — "
                             "needs a human look")
        return

    # The deterministic gate: harness runs the tests itself, in the worktree.
    passed, test_out = await asyncio.to_thread(
        repo.run_tests, project, wt, False)
    if not passed:
        # One retry (the engineer resumes their session with the failure in
        # front of them); a second red run is a human's call, not a loop.
        again = (item["error"] or "").startswith("tests failed after fix")
        db.update_item(name, "issue", item["number"],
                       status="waiting_human" if again else "approved",
                       error="tests failed after fix:\n" + test_out[-1500:])
        db.log_event(f"Issue #{item['number']}: tests failed — fix not pushed"
                     + (", held for a human after two red runs" if again
                        else ", retrying next cycle"), "warn", project=name)
        return

    msg = out["commit_message"].strip() or f"fix: issue #{item['number']}"
    if f"#{item['number']}" not in msg:
        msg += f" (#{item['number']})"
    repo.wt_commit_all(project, wt, msg)
    stat, diff = repo.wt_diff(project, wt)
    landed, err = await asyncio.to_thread(
        repo.push_worktree_to_dev, project, wt, branch)
    if not landed:
        db.update_item(name, "issue", item["number"], status="approved",
                       diff=diff, error=err[:2000])
        db.log_event(f"Issue #{item['number']}: {err[:120]} — retrying "
                     "next cycle", "warn", project=name)
        return
    repo.remove_worktree(project, wt)
    db.update_item(
        name, "issue", item["number"],
        status="queued", queued_at=db.now(), diff=diff,
        commits=msg, verdict_summary=out["summary"], error="")
    db.log_event(
        f"{persona} fixed issue #{item['number']} and landed it on "
        f"{project['dev_branch']}", project=name)
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
    if _breaker_tripped(project, item):
        return
    detail = gh.pr_detail(project["repo"], item["number"])
    if detail.get("isDraft"):
        db.update_item(name, "pr", item["number"], status="waiting_human",
                       verdict_summary="draft PR - left for the author")
        return
    branch = f"harness/pr-{item['number']}"
    try:
        cwd = str(repo.fetch_pr_branch(project, item["number"], branch))
        # (lock held by caller for the whole review below)
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
    _file_question(name, "Ruth", f"pr#{item['number']}", out)
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
        await merge_pr_item(project, db.get_item(name, "pr", item["number"]),
                            validate=False)
    else:
        db.update_item(name, "pr", item["number"], status="waiting_human")
        if verdict in ("needs_work", "reject") and out["draft_review"] and \
                db.policy(name, "post_comments") == "auto":
            gh.comment_pr(project["repo"], item["number"], out["draft_review"])
            db.log_event(f"Posted review on PR #{item['number']}", project=name)


async def _pr_merges_clean_and_passes(project, item) -> bool:
    """Merge the PR onto dev in harness's clone and run the suite there.

    The operator can send a PR straight to merge without waiting for Ruth,
    so this is the only thing standing between an unreviewed contribution and
    the dev branch. Nothing merges on a red suite, whoever asked.
    """
    name, number = project["name"], item["number"]
    detail = gh.pr_detail(project["repo"], number)
    if detail.get("isDraft"):
        db.update_item(name, "pr", number, status="waiting_human",
                       error="still a draft — not merged")
        db.log_event(f"PR #{number} is still a draft; not merging", "warn",
                     project=name)
        return False
    try:
        with repo.clone_lock(project):
            repo.fetch_pr_branch(project, number, f"harness/pr-{number}")
            passed, out = await asyncio.to_thread(repo.run_tests, project)
    except CmdError as e:
        db.update_item(name, "pr", number, status="blocked",
                       error=f"does not merge cleanly onto "
                             f"{project['dev_branch']}: {e}"[:2000])
        db.log_event(f"PR #{number} does not merge cleanly onto "
                     f"{project['dev_branch']}", "warn", project=name)
        return False
    if not passed:
        db.update_item(name, "pr", number, status="waiting_human",
                       verdict="needs_work",
                       error=f"tests failed on the merge result:\n{out[-2000:]}")
        db.log_event(f"PR #{number} not merged: the suite fails once it is on "
                     f"{project['dev_branch']}", "warn", project=name)
        return False
    return True


async def merge_pr_item(project, item, validate: bool = True) -> None:
    """Merge an approved PR into dev (retargeting it there first).

    validate=False only from the review, which has just fetched and tested
    this exact merge result and still holds the clone lock — flock is not
    reentrant, so it must not be taken again here.
    """
    name = project["name"]
    if validate and not await _pr_merges_clean_and_passes(project, item):
        return
    try:
        detail = gh.pr_detail(project["repo"], item["number"])
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

def _release_due(project) -> list | None:
    """Queued items when it is time to cut, otherwise None.

    An empty list is a real answer, not "nothing to do": the operator pressed
    Release now and dev is ahead of main with nothing queued behind it, which
    happens whenever work landed on dev outside the harness. There is still a
    release to cut, just no harness items to credit in it.
    """
    name = project["name"]
    if db.open_release(name):
        return None
    queued = db.items_by_status(name, "queued")
    requested = db.get_setting(f"release_requested.{name}") == "1"
    if requested:
        db.set_setting(f"release_requested.{name}", "")
        if queued or repo.dev_ahead_count(project) > 0:
            return queued
        db.log_event(f"Release requested, but {project['dev_branch']} matches "
                     f"{project['main_branch']} and nothing is queued — "
                     "nothing to release", "warn", project=name)
        return None
    if not queued:
        return None
    min_changes = int(db.policy(name, "release_min_changes"))
    max_age_days = float(db.policy(name, "release_max_age_days"))
    if len(queued) >= min_changes:
        return queued
    oldest = min(q["queued_at"] or db.now() for q in queued)
    age_days = (datetime.now(timezone.utc)
                - datetime.strptime(oldest, "%Y-%m-%dT%H:%M:%SZ")
                .replace(tzinfo=timezone.utc)).total_seconds() / 86400
    return queued if age_days >= max_age_days else None


async def propose_release(project, queued) -> None:
    name = project["name"]
    with repo.clone_lock(project):
        rid = await _propose_release_locked(project, queued)
    if rid is None or db.policy(name, "cut_release") != "auto":
        return
    # Hands-off: nobody is going to click. Mark it merging before finalising
    # so the GUI cannot offer an approve button for a release already on its
    # way out — a second click would try to merge a merged PR.
    release = db.get_release(rid)
    db.update_release(rid, status="merging")
    db.log_event(f"cut_release is auto — merging and tagging v"
                 f"{release['version']} without waiting for "
                 f"{config.OPERATOR}", project=name)
    # finalize outside the lock: it re-acquires it, and flock is not reentrant
    finalize_release(project, release)


async def _propose_release_locked(project, queued) -> int | None:
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
    _file_question(name, "Colin", "release", out)
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
    if db.policy(name, "cut_release") != "auto":
        notify.send(f"Release v{version} proposed ({name})",
                    f"{len(queued)} changes batched and tested — approve to "
                    f"merge and tag.", priority="high", tags="rocket",
                    click_path=f"/p/{name}")
    return rid


def finalize_release(project, release) -> None:
    """Merge the release PR into main and push the version tag."""
    name = project["name"]
    version = release["version"]
    try:
        gh.merge_pr(project["repo"], release["pr_number"], squash=False)
        with repo.clone_lock(project):
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
    notify.send(f"{name} v{version} released", "Tagged and published; CI is "
                "building the images.", tags="tada", click_path=f"/p/{name}")


# --- cycle ------------------------------------------------------------------

def _state_digest(project) -> str:
    name = project["name"]
    lines = []
    notes = db.latest_report("notes", name)
    if notes:
        lines.append(f"Desk notes (rolling summary):\n{notes['content']}\n")
    directives = db.get_setting(f"directives.{name}", "")
    if directives:
        lines.append("Directives from Harry's last stand-up (address first):\n"
                     + directives + "\n")
    for it in db.project_items(name):
        if it["gh_state"] != "open" and it["status"] != "queued":
            continue
        lines.append(
            f"- {it['kind']}#{it['number']} [{it['status']}] "
            f"{it['title']} (by {it['author']})"
            + (f" — verdict: {it['verdict']}: {it['verdict_summary'][:120]}"
               if it["verdict"] else "")
            + (f" — error: {it['error'][:120]}" if it["error"] else ""))
    open_qs = db.open_questions(name)
    if open_qs:
        lines.append("\nQuestions already filed and pending (do not re-ask):")
        lines += [f"- ({q['asked_by']}) {q['question'][:150]}" for q in open_qs]
    answered = db.recent_answers(name)
    if answered:
        lines.append("\nRecent decisions (Harry's rulings and "
                     f"{config.OPERATOR}'s answers — binding):")
        lines += [f"- Q ({q['asked_by']}): {q['question'][:120]}\n"
                  f"  A ({q['answered_by'] or 'operator'}): {q['answer'][:200]}"
                  for q in answered]
    queued = db.items_by_status(name, "queued")
    lines.append(f"\nQueued for next release: {len(queued)} change(s). "
                 f"Release policy: >={db.policy(name, 'release_min_changes')} "
                 f"changes or oldest >{db.policy(name, 'release_max_age_days')} days.")
    return "\n".join(lines) or "No open items."


async def run_cycle(project, force: bool = False,
                    quick: bool = False) -> None:
    """One full cycle for one project.

    quick=True is a re-wake to start work that is already signed off: if
    there are fresh approvals they run without re-planning the desk (the
    plan is the expensive part and they'd run before it anyway)."""
    name = project["name"]
    sync(project)
    if db.paused_until():
        return
    if not force and not within_active_hours(name):
        return  # outside working hours; sync done, agent work waits

    if db.get_setting(f"security_requested.{name}") == "1":
        db.set_setting(f"security_requested.{name}", "")
        await run_security_review(project)

    try:
        _reconcile_branches(project)

        # Release first: a due release must never be starved by a long
        # sweep (or a restart mid-sweep).
        queued = _release_due(project)
        if queued is not None:
            await propose_release(project, queued)

        fix_jobs: list = []

        # Anything a human approved in the GUI runs first. Fresh approvals
        # ahead of retries (items carrying an error), so a fix that keeps
        # failing cannot starve new work; a quick re-wake runs fresh ones
        # only — retries wait for the normal poll.
        approved = sorted(db.items_by_status(name, "approved"),
                          key=lambda i: bool(i["error"]))
        for item in approved:
            if item["kind"] == "issue":
                if not (quick and item["error"]):
                    fix_jobs.append(item)
            else:
                await merge_pr_item(project, item)

        # Team Lead plans the rest of the cycle.
        cwd = str(repo.clean_checkout(project, project["dev_branch"]))
        if quick and fix_jobs:
            plan_res = {"ok": False, "output": None}
        else:
            db.set_setting(f"last_plan_at.{name}", db.now())
            plan_res = await agents.lead_plan(project, _state_digest(project), cwd)
        tasks = plan_res["output"]["tasks"] if plan_res["ok"] else []
        if plan_res["ok"]:
            db.save_report("lead", name, plan_res["output"]["summary"])
            _file_question(name, project["lead_name"], "", plan_res["output"])
            db.set_setting(f"directives.{name}", "")  # consumed by this plan
            req = (plan_res["output"].get("staffing_request") or "").strip()
            if req:
                db.set_setting(f"staffing_request.{name}", req)
                db.log_event(f"{project['lead_name']} asked Harry for "
                             f"staffing: {req[:150]}", project=name)
            _open_tracking_issues(project, plan_res["output"].get("new_issues"))
        await process_questions(name)

        staff = db.staff_get(name)
        engineers = ["Malcolm"] + staff["extra"]
        fix_policy = db.policy(name, "fix_issues")

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
                await process_questions(name)  # Ruth may have asked Harry
                # auto-advance freshly approved fixes in the same cycle
                item = db.get_item(name, t["kind"], t["number"])
                if item["status"] == "approved":
                    fix_jobs.append(item)
            elif t["action"] == "fix" and item["status"] in ("triaged", "approved"):
                # The lead's "fix" is the section's sign-off. Only the
                # "approve" policy additionally waits for the operator.
                if item["kind"] != "issue":
                    continue
                if item["status"] == "approved" or fix_policy in ("auto", "lead"):
                    if item["status"] == "triaged":
                        db.update_item(name, "issue", item["number"],
                                       status="approved")
                        db.log_event(f"{project['lead_name']} put an engineer "
                                     f"on issue #{item['number']}: "
                                     f"{t.get('reason', '')[:120]}", project=name)
                        item = db.get_item(name, t["kind"], t["number"])
                    fix_jobs.append(item)
            elif t["action"] == "review" and item["status"] == "new":
                with repo.clone_lock(project):
                    await review_item(project, item)
                done += 1
                await process_questions(name)

        # Execute fixes as one concurrent wave: one engineer per job, each
        # in an isolated worktree. Hires give the desk real parallelism.
        seen, wave = set(), []
        for item in fix_jobs:
            k = (item["kind"], item["number"])
            if k in seen:
                continue
            seen.add(k)
            wave.append(item)
        wave = wave[:len(engineers)]
        if wave:
            await asyncio.to_thread(repo.ensure_test_env, project)
            results = await asyncio.gather(*(
                fix_item(project, item, engineers[i % len(engineers)])
                for i, item in enumerate(wave)), return_exceptions=True)
            for item, r in zip(wave, results):
                if isinstance(r, AgentStalled):
                    raise r
                if isinstance(r, BaseException):
                    # Setup failed before/around the engineer (worktree, gh).
                    # Park it with the reason: retried on the normal poll,
                    # never on the fast re-wake, and visible on the board.
                    db.update_item(name, "issue", item["number"],
                                   status="approved",
                                   error=f"{type(r).__name__}: {r}"[:2000])
                    db.log_event(f"Fix for issue #{item['number']} could not "
                                 f"start: {str(r)[:120]} — will retry next "
                                 "cycle", "warn", project=name)
            done += len(wave)
            await process_questions(name)

        # Fallback: triage anything new the lead didn't mention.
        for item in db.items_by_status(name, "new"):
            if done >= MAX_AGENT_TASKS_PER_CYCLE:
                break
            if item["kind"] == "issue":
                await triage_item(project, item)
            else:
                with repo.clone_lock(project):
                    await review_item(project, item)
            done += 1
        await process_questions(name)

        queued = _release_due(project)
        if queued is not None:
            await propose_release(project, queued)  # catches same-cycle landings
    except AgentStalled:
        db.log_event(f"Cycle for {name} paused mid-way; will resume", "warn",
                     project=name)


def work_ready(project) -> bool:
    """True when the desk has work it can start without anyone's click —
    the worker wakes again soon rather than waiting a full poll interval.

    Deliberately narrow: fresh approvals (not retries carrying an error —
    those wait for the normal poll so a failing fix can't spin) and issues
    triaged since the lead last planned (if the lead has seen an item and
    left it triaged, that was a decision)."""
    name = project["name"]
    if not within_active_hours(name):
        return False
    if any(not i["error"] for i in db.items_by_status(name, "approved")):
        return True
    if db.policy(name, "fix_issues") not in ("auto", "lead"):
        return False
    # Compared with when the lead was last *asked* to plan (set before the
    # run, success or not) so a failing plan cannot keep the desk "ready".
    since = db.get_setting(f"last_plan_at.{name}", "")
    return any(i["gh_state"] == "open" and i["kind"] == "issue"
               and i["updated_at"] > since
               for i in db.items_by_status(name, "triaged"))


async def run_all_cycles(force: bool = False,
                         only: list[str] | None = None) -> list[str]:
    """Run every desk's cycle (or just `only` — a fast re-wake for desks
    with signed-off work). Returns the desks with work ready to go on."""
    projects = db.all_projects(enabled_only=True)
    for p in projects:
        if only is not None and p["name"] not in only:
            continue
        db.touch_heartbeat()
        try:
            await run_cycle(p, force=force, quick=only is not None)
        except AgentStalled:
            break
        except Exception as e:
            db.log_event(f"Cycle failed: {type(e).__name__}: {e}", "error",
                         project=p["name"])
    # Harry's cross-project review now happens in the hourly stand-up
    # (run_standup) rather than every sweep — cheaper and more predictable.
    if db.paused_until():
        return []
    return [p["name"] for p in projects if work_ready(p)]


def _reconcile_branches(project) -> None:
    """Fast-forward a stale dev to main before anyone branches from it."""
    import subprocess
    name = project["name"]
    try:
        with repo.clone_lock(project):
            state = repo.reconcile_dev(project)
    except (CmdError, subprocess.TimeoutExpired) as e:
        state = f"failed: {str(e)[:150]}"
    # Warn on a change of state, not every cycle (cycles can be a minute apart).
    key = f"branch_state.{name}"
    if state == "fast-forwarded":
        db.log_event(f"{project['dev_branch']} was behind "
                     f"{project['main_branch']} — fast-forwarded so work "
                     "branches from current code", project=name)
        db.set_setting(key, "")
    elif state != db.get_setting(key, ""):
        db.set_setting(key, state)
        if state == "diverged":
            db.log_event(f"{project['dev_branch']} and {project['main_branch']} "
                         "have diverged — needs a human merge before the "
                         "next release", "warn", project=name)
        elif state:
            db.log_event(f"Branch check {state}", "warn", project=name)


def _open_tracking_issues(project, new_issues) -> None:
    """The lead filed tracking issues from their plan: create them for real
    so the work enters triage. Deterministic, policy-free — opening an issue
    on your own repo is the lowest-stakes outward action there is."""
    name = project["name"]
    if not new_issues:
        return
    if db.policy(name, "file_issues") != "auto":
        db.log_event(f"{project['lead_name']} wanted to open "
                     f"{len(new_issues)} tracking issue(s) but file_issues "
                     "is off — not filed", "warn", project=name)
        return
    import re as _re

    def norm(t: str) -> str:
        return _re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()

    items = db.project_items(name)
    existing = {norm(i["title"]) for i in items if i["gh_state"] == "open"}
    day_ago = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=1)
               ).strftime("%Y-%m-%dT%H:%M:%SZ")
    filed_today = sum(1 for i in items
                      if i["author"] == project["lead_name"]
                      and i["created_at"] >= day_ago)
    for ni in new_issues[:3]:
        if filed_today >= TRACKING_ISSUES_PER_DAY:
            db.log_event(f"{project['lead_name']} wanted to open another "
                         "tracking issue but the desk's daily cap "
                         f"({TRACKING_ISSUES_PER_DAY}) is reached", "warn",
                         project=name)
            break
        title = (ni.get("title") or "").strip()
        body = (ni.get("body") or "").strip()
        if not title or not body or norm(title) in existing:
            continue  # already filed (plan re-run, or a human beat us to it)
        try:
            num = gh.create_issue(project["repo"], title, body)
        except CmdError as e:
            db.log_event(f"Could not open tracking issue '{title[:60]}': "
                         f"{str(e)[:120]}", "warn", project=name)
            continue
        db.upsert_item(name, "issue", num, title, project["lead_name"],
                       "open", db.now())
        existing.add(norm(title))
        filed_today += 1
        db.log_event(f"{project['lead_name']} opened issue #{num}: "
                     f"{title[:80]}", project=name)


# --- Harry's inbox -------------------------------------------------------------

async def process_questions(project_name: str | None = None) -> None:
    """Harry rules on his people's questions as soon as they are asked.

    Answers land on the question record (the asker's next prompt carries
    them); only what he escalates reaches the operator — via the GUI's
    decision queue and ntfy."""
    inbox = db.harry_inbox(project_name)
    if not inbox or db.paused_until():
        return
    lines = []
    for q in inbox:
        opts = db.question_options(q)
        lines.append(
            f"- id={q['id']} [{q['project'] or 'section'}] from {q['asked_by']}"
            f"{' re ' + q['item_key'] if q['item_key'] else ''}: {q['question']}"
            + (f"\n  options offered: {' / '.join(opts)}" if opts else ""))
    ctx = []
    for pname in sorted({q["project"] for q in inbox if q["project"]}):
        pr = db.get_project(pname)
        if pr:
            ctx.append(f"## {pname} ({pr['repo']}, lead {pr['lead_name']})\n"
                       + _state_digest(pr))
    try:
        res = await agents.rule_questions("\n".join(lines),
                                          "\n\n".join(ctx) or "(none)")
    except AgentStalled:
        return
    if not res["ok"]:
        db.log_event(f"Harry could not rule on questions: {res['error'][:120]}",
                     "warn")
    else:
        _apply_decisions(res["output"].get("decisions", []))
    # Anything Harry left undecided (or a ruling run that failed) gets one
    # more pass; after that it goes to the operator rather than costing a
    # ruling run every few minutes.
    for q in inbox:
        if db.question(q["id"])["status"] != "open":
            _undecided.pop(q["id"], None)
            continue
        _undecided[q["id"]] = _undecided.get(q["id"], 0) + 1
        if _undecided[q["id"]] >= 2:
            db.escalate_question(q["id"])
            _undecided.pop(q["id"], None)
            db.log_event(f"Harry left {q['asked_by']}'s question undecided "
                         f"twice — escalated to {config.OPERATOR}", "warn",
                         project=q["project"])


_undecided: dict[int, int] = {}  # question id -> ruling passes left open


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
    inbox = db.harry_inbox()

    def age_days(ts: str) -> str:
        try:
            dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ") \
                .replace(tzinfo=timezone.utc)
            return f"{(now_ts - dt).total_seconds() / 86400:.1f}d"
        except (ValueError, TypeError):
            return "?"

    sections = []
    if inbox:
        sections.append("Open questions awaiting your ruling:\n" + "\n".join(
            f"- id={q['id']} [{q['project'] or 'section'}] from {q['asked_by']}"
            f"{' re ' + q['item_key'] if q['item_key'] else ''}: {q['question'][:200]}"
            + (f" (options: {' / '.join(db.question_options(q))})"
               if q['options'] else "")
            for q in inbox))
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
                lines.append(f"waiting on operator {it['kind']}#{it['number']} "
                             f"for {age_days(it['updated_at'])}: "
                             f"{it['verdict']} — {it['title'][:80]}")
        fix_policy = db.policy(name, "fix_issues")
        for it in db.items_by_status(name, "triaged"):
            if it["kind"] == "issue" and it["gh_state"] == "open":
                who = ("operator's approval (fix policy: approve)"
                       if fix_policy == "approve"
                       else f"{p['lead_name']}'s plan to assign an engineer")
                lines.append(f"triaged, awaiting {who}: issue#{it['number']} "
                             f"for {age_days(it['updated_at'])} — "
                             f"{it['title'][:80]}")
        for it in db.items_by_status(name, "queued"):
            lines.append(f"queued for release {it['kind']}#{it['number']} "
                         f"({age_days(it['queued_at'] or it['updated_at'])})")
        staff = db.staff_get(name)
        util = {}
        for r in db.recent_runs(100, name):
            who = r["agent"] or r["role"]
            util[who] = util.get(who, 0) + 1
        lines.append("On this desk: Malcolm" +
                     ("".join(f", {e}" for e in staff["extra"])) +
                     ", Ruth, Colin, Zaf" +
                     (f"; stood down: {', '.join(staff['benched'])}"
                      if staff["benched"] else "") +
                     f". Available hire pool: "
                     f"{', '.join(n for n in __import__('harness.config', fromlist=['c']).HIRE_POOL if n not in staff['extra'])}")
        req = db.get_setting(f"staffing_request.{name}", "")
        if req:
            lines.append(f"STAFFING REQUEST from {p['lead_name']}: {req}")
        lines.append("Recent run counts by person: " +
                     (", ".join(f"{k}={v}" for k, v in sorted(util.items()))
                      or "none"))
        fails = [r for r in db.recent_runs(10, name) if r["ok"] == 0]
        for r in fails[:5]:
            lines.append(f"failed run: {r['task']} {r['item_key']} — "
                         f"{r['summary'][:150]}")
        lines.append(f"Spend to date: ${db.total_cost(name):.2f}")
        sections.append("\n".join(lines))
    return "\n\n".join(sections) or "No harnesses configured."


def standup_due() -> bool:
    from datetime import datetime, timezone
    last = db.get_setting("last_standup_at")
    if not last:
        return True
    dt = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() >= 3600


async def run_standup(force: bool = False) -> None:
    """Harry's hourly stand-up across every desk."""
    _unstick_working()
    projects = db.all_projects(enabled_only=True)
    if db.paused_until() or not projects:
        return
    if not force and not any(within_active_hours(p["name"]) for p in projects):
        return  # the whole section is off the clock
    try:
        res = await agents.standup(_standup_digest())
    except AgentStalled:
        return
    if not res["ok"]:
        db.log_event(f"Stand-up failed: {res['error']}", "warn")
        return
    out = res["output"]
    _file_question("", "Harry", "", out)
    db.save_report("cto", "", out["standup_markdown"])
    for desk in out.get("desks", []):
        if db.get_project(desk["project"]):
            marker = "" if desk["moving"] else "⚠ "
            db.save_report("harry", desk["project"],
                           marker + desk["status_line"])
    for b in out.get("blockers", []):
        db.log_event(f"Stand-up blocker: {b['message']}", "warn",
                     project=b.get("project", ""))
    _apply_decisions(out.get("decisions", []))
    _apply_staffing(out.get("staffing", []))
    for d in out.get("directives", []):
        pr = db.get_project(d.get("project", ""))
        if pr and d.get("directive", "").strip():
            prev = db.get_setting(f"directives.{d['project']}", "")
            db.set_setting(f"directives.{d['project']}",
                           (prev + "\n- " if prev else "- ") +
                           d["directive"].strip()[:400])
            db.log_event(f"Harry directed {pr['lead_name']}: "
                         f"{d['directive'][:150]}", project=d["project"])
    for pr in db.all_projects(enabled_only=True):
        db.set_setting(f"staffing_request.{pr['name']}", "")  # Harry has ruled
    db.set_setting("last_standup_at", db.now())
    if out["all_clear"]:
        db.log_event("Stand-up: all clear")


def _apply_decisions(decisions: list) -> None:
    valid = {q["id"]: q for q in db.harry_inbox()}
    for d in decisions:
        q = valid.get(d.get("question_id"))
        if not q:
            continue
        if d["action"] == "answer" and d.get("answer", "").strip():
            db.answer_question(q["id"], d["answer"].strip(), by="Harry")
            db.log_event(f"Harry ruled on {q['asked_by']}'s question "
                         f"({q['question'][:60]}…): {d['answer'][:150]}",
                         project=q["project"])
        elif d["action"] == "escalate":
            db.escalate_question(q["id"])
            db.log_event(f"Harry escalated {q['asked_by']}'s question to {config.OPERATOR}",
                         "warn", project=q["project"])
            from urllib.parse import urlencode
            opts = db.question_options(q)
            actions = [{"label": o, "body": urlencode({"answer": o}),
                        "path": f"/p/{q['project'] or '-'}/question/{q['id']}"
                                f"/answer?via=ntfy"}
                       for o in opts]
            notify.send(
                f"Harry needs your decision ({q['project'] or 'section'})",
                f"{q['asked_by']} asks: {q['question'][:300]}",
                priority="high", tags="question",
                click_path=f"/p/{q['project']}" if q["project"] else "/",
                actions=actions)


def _apply_staffing(actions: list) -> None:
    from . import config as cfg
    for a in actions:
        pname, name = a.get("project", ""), a.get("name", "").strip()
        if not db.get_project(pname):
            continue
        staff = db.staff_get(pname)
        if a["action"] == "hire":
            if name in cfg.HIRE_POOL and name not in staff["extra"] \
                    and len(staff["extra"]) < cfg.MAX_EXTRA_ENGINEERS:
                staff["extra"].append(name)
                staff.setdefault("hired_at", {})[name] = db.now()
                if name in staff["benched"]:
                    staff["benched"].remove(name)
                db.staff_set(pname, staff)
                db.log_event(f"Harry has brought {name} onto the {pname} desk: "
                             f"{a['reason'][:150]}", project=pname)
        elif a["action"] == "stand_down":
            hired = staff.get("hired_at", {}).get(name, "")
            if hired:
                age_h = (datetime.now(timezone.utc)
                         - datetime.strptime(hired, "%Y-%m-%dT%H:%M:%SZ")
                         .replace(tzinfo=timezone.utc)).total_seconds() / 3600
                if age_h < 24:
                    db.log_event(
                        f"Refused Harry's stand-down of {name} on {pname}: "
                        f"hired {age_h:.0f}h ago — zero runs right after "
                        "hiring means not started, not idle", "warn",
                        project=pname)
                    continue
            if name in staff["extra"]:
                staff["extra"].remove(name)
            if name not in staff["benched"] and name not in ("Harry",):
                staff["benched"].append(name)
            db.staff_set(pname, staff)
            db.log_event(f"Harry has stood {name} down on the {pname} desk: "
                         f"{a['reason'][:150]}", project=pname)
        elif a["action"] == "reinstate":
            if name in staff["benched"]:
                staff["benched"].remove(name)
                db.staff_set(pname, staff)
                db.log_event(f"Harry has reinstated {name} on the {pname} "
                             f"desk", project=pname)


# --- security review (manually triggered) ------------------------------------

async def run_security_review(project) -> None:
    name = project["name"]
    with repo.clone_lock(project):
        cwd = str(repo.clean_checkout(project, project["dev_branch"]))
    db.log_event("Security review started (Zaf)", project=name)
    try:
        res = await agents.security_review(project, cwd)
    except AgentStalled:
        db.set_setting(f"security_requested.{name}", "1")  # retry after pause
        return
    if not res["ok"]:
        db.log_event(f"Security review failed: {res['error']}", "warn",
                     project=name)
        return
    out = res["output"]
    _file_question(name, "Zaf", "", out)
    db.save_report("security", name, out["report_markdown"])
    serious = [f for f in out.get("findings", [])
               if f["severity"] in ("critical", "high")]
    for f in serious:
        db.log_event(f"Security finding [{f['severity']}] {f['title']} "
                     f"({f['location']})", "warn", project=name)
    db.log_event(f"Security review complete: {len(out.get('findings', []))} "
                 f"finding(s), {len(serious)} serious", project=name)


# --- operator directives -----------------------------------------------------

def _apply_directive_actions(project, actions: list) -> list[str]:
    """Deterministically execute Harry's directive actions. Every action is
    something the GUI could already do — no new privileges."""
    name = project["name"]
    done = []
    for a in actions or []:
        act = a.get("action")
        try:
            if act in ("approve_item", "reject_item", "hold_item", "retry_item"):
                kind, num = a.get("kind"), a.get("number")
                if not (kind and num and db.get_item(name, kind, num)):
                    continue
                status = {"approve_item": "approved", "reject_item": "rejected",
                          "hold_item": "waiting_human",
                          "retry_item": "approved"}[act]
                fields = {"status": status}
                if act == "retry_item":
                    fields.update(error="", session_id="")
                db.update_item(name, kind, num, **fields)
                done.append(f"{act} {kind}#{num}")
            elif act in ("hire", "stand_down", "reinstate"):
                if a.get("name"):
                    _apply_staffing([{"project": name, "action": act,
                                      "name": a["name"],
                                      "reason": "operator directive"}])
                    done.append(f"{act} {a['name']}")
            elif act == "security_review":
                db.set_setting(f"security_requested.{name}", "1")
                done.append("security review queued")
            elif act == "propose_release":
                db.set_setting(f"release_requested.{name}", "1")
                done.append("release proposal queued")
            elif act == "set_policy":
                if a.get("key") in config.POLICY_DEFAULTS and a.get("value"):
                    db.set_policy(name, a["key"], a["value"].strip())
                    done.append(f"policy {a['key']}={a['value'].strip()}")
            elif act == "tell_desk":
                if a.get("text"):
                    prev = db.get_setting(f"directives.{name}", "")
                    db.set_setting(f"directives.{name}",
                                   (prev + "\n- " if prev else "- ")
                                   + a["text"].strip()[:400])
                    done.append("tasked the lead")
            elif act == "create_issue":
                if a.get("title") and a.get("text"):
                    num = gh.create_issue(project["repo"], a["title"].strip(),
                                          a["text"].strip())
                    db.upsert_item(name, "issue", num, a["title"].strip(),
                                   "operator", "open", db.now())
                    done.append(f"opened issue#{num}: {a['title'][:50]}")
            elif act == "answer_question":
                if a.get("question_id") and a.get("text"):
                    db.answer_question(a["question_id"], a["text"].strip(),
                                       by="Harry")
                    done.append(f"answered q{a['question_id']}")
        except Exception as e:
            db.log_event(f"Directive action {act} failed: {e}", "warn",
                         project=name)
    for d in done:
        db.log_event(f"Directive: {d}", project=name)
    return done


async def process_directives() -> None:
    """Turn pending operator directions into actions, promptly."""
    for q in db.pending_directives():
        project = db.get_project(q["project"])
        if not project:
            db.resolve_directive(q["id"], "project no longer exists")
            continue
        try:
            res = await agents.execute_directive(
                project, q["question"], q["item_key"], _state_digest(project))
        except AgentStalled:
            return  # stays pending; retried when limits clear
        if not res["ok"]:
            db.log_event(f"Directive processing failed: {res['error'][:120]}",
                         "warn", project=q["project"])
            continue  # stays pending for the next wake
        out = res["output"]
        done = _apply_directive_actions(project, out.get("actions", []))
        reply = out.get("reply", "").strip() or             ("Done: " + ", ".join(done) if done else "Noted.")
        db.resolve_directive(q["id"], reply)
        db.log_event(f"Harry actioned the direction: {reply[:140]}",
                     project=q["project"])
