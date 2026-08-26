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
# Per desk: how many lead-filed tracking issues may sit open and unworked
# (status new or triaged) at once. A backlog ceiling, not a daily rate —
# worked-through filings free their slot however fast they were opened.
OPEN_TRACKING_ISSUES_CAP = 6


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
BREAKER_ASKER = "harness"   # who a circuit-breaker question comes from
BREAKER_OPTIONS = ["retry", "split", "escalate"]
MAX_BREAKER_TRIPS = 2       # trips before the item goes to the operator, ruling or no


def _failure_digest(name: str, key: str) -> str:
    """Why this item's last runs failed, as a ruling needs to read it."""
    lines = []
    for r in db.recent_failures(name, key, BREAKER_THRESHOLD):
        who = r["agent"] or r["role"]
        lines.append(f"- {r['started_at']} {who} ({r['task']}): "
                     f"{(r['summary'] or 'no cause recorded')[:200]}")
    return "\n".join(reversed(lines)) or "- (no run summaries recorded)"


def hold_for_ruling(project, item, asked_by: str, reason: str,
                    question: str, options: list[str]) -> str:
    """Put an item that cannot go forward on its own with Harry, not the
    operator. Returns the status it ended up in.

    This is the one road from "the section cannot do this as it stands" to
    a decision: a triage verdict of not-fixable, an engineer declining, a
    run that changed nothing, two red test runs, a review that is not an
    auto-merge, and the circuit breaker all come through here. The item is
    held with a question on it that Harry answers on the next questions
    pass, and his answer moves it (route_answers, or apply_breaker_ruling
    for the breaker's own vocabulary).

    One ruling per item: each hold counts a trip, and the trip after his
    ruling stops asking and puts the item on the operator's desk instead, so
    a ruling that leads straight back here cannot loop. The operator's own
    approve or "fix" forgives the trips — they have looked at the thing.
    """
    name = project["name"]
    kind, number = item["kind"], item["number"]
    key = f"{kind}#{number}"
    # Read the trip count fresh: a ruling may have landed since the caller
    # picked this row up.
    current = db.get_item(name, kind, number) or item
    trips = (current["breaker_trips"] or 0) + 1
    if trips >= MAX_BREAKER_TRIPS:
        db.update_item(name, kind, number, status="waiting_human",
                       breaker_trips=trips,
                       error=f"{reason} again after Harry's ruling — held for "
                             f"{config.OPERATOR}")
        db.log_event(f"{key} came back after Harry's ruling ({reason[:80]}) "
                     f"— {config.OPERATOR}'s decision now", "warn",
                     project=name)
        notify.send(f"Held: {key} ({name})",
                    f"{reason[:120]} — again after Harry's ruling; needs "
                    "your look.", tags="warning",
                    click_path=f"/p/{name}/{kind}/{number}")
        return "waiting_human"
    db.update_item(name, kind, number, status="held", breaker_trips=trips,
                   error=f"{reason} — with Harry for a ruling")
    if db.ask_question(name, asked_by, key, question, options=options) is None:
        # The very same question was ruled on already and the ruling stands;
        # an item held with nobody asked is an item nobody moves, so it is
        # the operator's rather than sitting there.
        db.update_item(name, kind, number, status="waiting_human",
                       error=f"{reason} — Harry has already ruled on this "
                             f"once; held for {config.OPERATOR}")
        db.log_event(f"{key}: {reason[:80]} — the question was already put "
                     f"to Harry and his ruling stands, so it is "
                     f"{config.OPERATOR}'s now", "warn", project=name)
        return "waiting_human"
    db.log_event(f"{key} held ({reason[:80]}) — asked Harry for a ruling",
                 "warn", project=name)
    return "held"


HOLD_OPTIONS = {"issue": ["Fix", "Skip", "Won't fix"],
                "pr": ["Merge", "Skip", "Won't fix"]}


def hold_item(project, item, asked_by: str, reason: str, context: str) -> str:
    """hold_for_ruling with the vocabulary route_answers acts on: Fix (or
    Merge) puts the item back in the flow, Skip parks it, Won't fix closes
    it out. `context` is what Harry needs to rule — the verdict summary,
    the decline reason, the failing output."""
    key = f"{item['kind']}#{item['number']}"
    opts = HOLD_OPTIONS[item["kind"]]
    question = (f"{key} ({item['title'][:80]}) is held: {reason}.\n"
                f"{context.strip()[:1200]}\n"
                f"Rule on it: {opts[0]} (the section gets on with it), "
                "Skip (parked, nobody works it), or Won't fix (closed out). "
                "Escalate only if this is genuinely the operator's call — "
                "product direction, a breaking change, consequences outside "
                "the codebase.")
    return hold_for_ruling(project, item, asked_by, reason, question, opts)


def _breaker_tripped(project, item) -> bool:
    """Hold items that keep failing instead of burning retries forever.

    The first trip goes to Harry, not the operator: a question on the item
    carrying both failures, answered on the next questions pass. He can
    retry it fresh, tell the desk to split it, or escalate — and only his
    escalation pages the operator. A second trip after that ruling stops
    asking and puts the item on the operator's desk, so a retry ruling
    cannot loop.
    """
    name = project["name"]
    key = f"{item['kind']}#{item['number']}"
    if db.consecutive_failures(name, key) < BREAKER_THRESHOLD:
        return False
    digest = _failure_digest(name, key)
    hold_for_ruling(
        project, item, BREAKER_ASKER,
        f"circuit breaker: {BREAKER_THRESHOLD} consecutive failed runs",
        f"{key} ({item['title'][:80]}) has failed {BREAKER_THRESHOLD} runs in "
        f"a row and is held. The failures:\n{digest}\n"
        "Rule on it: retry (a fresh session on the same item), split (tell "
        "the desk to break the work up — the right call when a run keeps "
        "hitting error_max_turns, which means the item is too big rather "
        "than broken), or escalate if this is genuinely the operator's.",
        BREAKER_OPTIONS)
    return True


def is_breaker_question(q) -> bool:
    return bool(q["asked_by"] == BREAKER_ASKER and q["item_key"]
                and q["project"])


def apply_breaker_ruling(q, ruling: str, answer: str, by: str = "Harry") -> None:
    """Carry out a ruling on a held item. `ruling` is one of the breaker
    options; anything else means no direction was given, so the item goes to
    the operator rather than sitting held with nobody acting.

    Every branch is an action the GUI could already take — the ruling only
    chooses between them."""
    project = db.get_project(q["project"])
    if not project:
        return
    name = project["name"]
    kind, _, num = q["item_key"].partition("#")
    item = db.get_item(name, kind, int(num)) if num.isdigit() else None
    if not item or item["status"] != "held":
        return  # already moved on (operator click, item closed)
    ruling = (ruling or "").strip().lower()
    if ruling == "retry":
        # Harry's retry deliberately keeps the trip count, so the next trip
        # on this item is the operator's rather than another ruling. Their
        # own retry forgives it: they have looked at the thing.
        _apply_directive_actions(project, [{"action": "retry_item",
                                            "kind": kind, "number": int(num)}],
                                 reset_trips=(by == config.OPERATOR))
        db.log_event(f"{by} sent {q['item_key']} back for a fresh attempt",
                     project=name)
        return
    if ruling == "split":
        _apply_directive_actions(project, [{"action": "tell_desk",
                                            "text": f"{q['item_key']}: "
                                                    + (answer or "split this "
                                                       "into smaller issues")}])
        db.update_item(name, kind, int(num),
                       error=f"held: {by} has told {project['lead_name']} to "
                             "take this apart")
        db.log_event(f"{by} sent {q['item_key']} back to "
                     f"{project['lead_name']} to be split", project=name)
        return
    db.update_item(name, kind, int(num), status="waiting_human",
                   error=f"circuit breaker: held for {config.OPERATOR}"
                         + (f" — {by}: {answer[:160]}" if answer else ""))
    if ruling == "escalate":
        return  # the escalation itself pages the operator
    db.log_event(f"{by}'s ruling on {q['item_key']} gave no direction "
                 f"({answer[:80]}) — held for {config.OPERATOR}", "warn",
                 project=name)
    if by != config.OPERATOR:
        notify.send(f"Held: {q['item_key']} ({name})",
                    f"{by} ruled but gave no direction — needs your look.",
                    tags="warning",
                    click_path=f"/p/{name}/{kind}/{num}")


def held_item_for(q):
    """The held item a question is about, or None. A ruling on a held item
    has to move it; a question about an item in any other state is just
    read by whoever is on it."""
    if not (q["project"] and q["item_key"]):
        return None
    kind, _, num = q["item_key"].partition("#")
    if kind not in ("issue", "pr") or not num.isdigit():
        return None
    item = db.get_item(q["project"], kind, int(num))
    return item if item and item["status"] == "held" else None


def park_held_item(q, note: str) -> None:
    """Harry has sent a held item's question to the operator: the item goes
    with it, otherwise it sits held with nobody left to rule on it."""
    item = held_item_for(q)
    if not item:
        return
    db.update_item(q["project"], item["kind"], item["number"],
                   status="waiting_human",
                   error=f"escalated by Harry — {config.OPERATOR}'s call"
                         + (f": {note[:160]}" if note else ""))


# --- answers that move items --------------------------------------------------
# Where an answer puts the item it is about. The operator answering "fix" is
# the same act as pressing approve, so it is the sign-off whatever the
# fix_issues policy says; "skip" leaves the item with them; "won't fix"
# closes it out. The wording-to-action mapping is db.ANSWER_ACTIONS — a fixed
# table, so no agent ever has to work out at fix time what the operator meant.
ANSWER_ROUTE = {"proceed": "approved", "hold": "waiting_human",
                "reject": "rejected"}
# Statuses an answer may move an item out of. Anything else — new (triage
# looks at it anyway), working, approved, queued, released — is already in
# hand, and the answer reaches it through the item's thread. A held item is
# routable by the operator as well as by Harry: their approve button works
# on it, and so must their answer.
ROUTABLE_STATUSES = ("waiting_human", "triaged", "blocked", "held")


def _proceed_status(project, item, by: str) -> str:
    """Where a "fix"/"merge" answer sends the item.

    The operator saying so is the sign-off whatever the policy. Harry's
    ruling is the section's decision, which the policy may say is not
    enough: under fix_issues: approve an issue nobody has yet signed off,
    or a PR under merge_prs: approve, is the operator's click by the
    operator's own setting, so his "fix" puts it on their desk with his
    recommendation attached rather than starting the work over their gate.
    """
    if by != config.CTO_NAME:
        return "approved"
    name = project["name"]
    if item["kind"] == "pr":
        key = "merge_dependabot" if _is_dependabot(item["author"]) else "merge_prs"
        gated = db.policy(name, key) != "auto"
    else:
        gated = (db.policy(name, "fix_issues") == "approve"
                 and not (item["session_id"] or item["branch"]))
    return "waiting_human" if gated else "approved"


def _back_to_asker(item) -> str:
    """Where an answer that doesn't say what to do sends the item.

    It still has to go somewhere: back to whoever asked, with the answer in
    front of them. An item an engineer had already started resumes with them
    (it was signed off once to get there); anything else goes back through
    triage or review, which is where an unread answer gets read."""
    return "approved" if (item["session_id"] or item["branch"]) else "new"


def route_answers(project) -> list[str]:
    """Act on answers that have not been picked up yet; returns what moved.

    Answering is an instruction about the item, not a note on it. Nothing
    here runs an agent or touches GitHub, so both the web handler (on the
    click, so the operator sees the item move) and every cycle (for
    anything left over, including items stranded before this existed) call
    it."""
    name = project["name"]
    moved = []
    for q in db.unrouted_answers(name):
        # Stamped before it is acted on, so an answer acts once and once
        # only: a decision the operator later reverses by hand must not be
        # undone again by the same old answer on the next cycle.
        db.mark_question_routed(q["id"])
        if is_breaker_question(q):
            continue  # apply_breaker_ruling has its own, richer vocabulary
        kind, _, num = q["item_key"].partition("#")
        if kind not in ("issue", "pr") or not num.isdigit():
            continue
        # Rows from before answered_by existed are the operator's — that is
        # all there was then.
        by = q["answered_by"] or "operator"
        harrys = by == config.CTO_NAME
        if not harrys and by not in ("operator", config.OPERATOR):
            continue
        item = db.get_item(name, kind, int(num))
        if not item or item["gh_state"] != "open" \
                or item["status"] not in ROUTABLE_STATUSES:
            continue
        # Harry's ruling moves an item that is held for exactly that ruling.
        # On anything else his answer reaches his people through the
        # question record and the thread; it is not the operator's sign-off.
        if harrys and item["status"] != "held":
            continue
        action = db.answer_action(q["answer"])
        if action == "proceed":
            status = _proceed_status(project, item, by)
        else:
            status = ANSWER_ROUTE.get(action) or _back_to_asker(item)
        fields = {"status": status}
        if status in ("approved", "new"):
            # Going back to an agent, so the reason it stopped goes with it,
            # failure history and all: without the breaker reset the old
            # failures hold the item again before the fresh attempt has run,
            # which is exactly what the decision being ignored looks like.
            # On a hold or a reject the error stays — it is the record of
            # why the item stopped. Harry's ruling keeps the trip count: the
            # next hold on this item is the operator's, not another ruling.
            fields.update(error="", breaker_reset_at=db.now())
            if not harrys:
                fields["breaker_trips"] = 0
        elif harrys and status == "waiting_human":
            fields["error"] = (f"parked by Harry: {q['answer'][:160]}"
                               if action == "hold" else
                               f"Harry recommends {q['answer'][:40]} — the "
                               f"policy makes this {config.OPERATOR}'s click")
        db.update_item(name, kind, item["number"], **fields)
        why = action or ("wording says nothing either way — back to the "
                         "agent that asked")
        who = "Harry's ruling" if harrys else f"{config.OPERATOR}'s answer"
        db.thread_append(name, q["item_key"], "harness", "event",
                         f"{who} acted on ({why}): "
                         f"{item['status']} → {status}.")
        db.log_event(f"{who} moved {q['item_key']} from "
                     f"{item['status']} to {status}", project=name)
        moved.append(f"{q['item_key']} -> {status}")
    return moved


PERSONA_MEMORY_KEY = {"Ruth": "analyst", "Malcolm": "engineering",
                      "Colin": "ops", "Zaf": "security"}


def _file_question(project_name: str, asked_by: str, item_key: str,
                   out: dict | None) -> None:
    if not out:
        return
    if out.get("question_for_human"):
        # Harry's own questions are filed straight to the operator by
        # db.ask_question — he cannot rule on himself.
        db.ask_question(project_name, asked_by, item_key,
                        out["question_for_human"],
                        options=out.get("question_options"))
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
    key = f"issue#{item['number']}"
    _file_question(name, "Ruth", key, out)
    fixable = bool(out["valid"] and out["fixable"]
                   and out["verdict"] in ("bug", "feature"))
    repro_path = (out.get("repro_test_path") or "").strip().lstrip("/")
    repro_body = out.get("repro_test_content") or ""
    repro = json.dumps({"path": repro_path, "content": repro_body}) \
        if fixable and repro_path and repro_body.strip() and ".." not in repro_path \
        else ""
    db.update_item(
        name, "issue", item["number"],
        verdict=out["verdict"],
        verdict_summary=out["summary"],
        plan=out.get("plan", ""),
        draft_comment=out.get("draft_comment", ""),
        repro_test=repro,
        status="triaged" if fixable else "waiting_human",
        error="",
    )
    db.thread_append(name, key, "Ruth", "finding",
                     f"Verdict: {out['verdict']} (valid={out['valid']}, "
                     f"fixable={out['fixable']})\n{out['summary']}")
    if not fixable and not out.get("needs_operator"):
        # Not something the section does on its own — but that is Harry's
        # call before it is the operator's. Only Ruth flagging it as the
        # maintainer's (product direction, a breaking change, outside the
        # codebase) leaves it on their desk.
        hold_item(project, db.get_item(name, "issue", item["number"]), "Ruth",
                  f"Ruth's verdict is {out['verdict']}, not fixable as it "
                  "stands", f"Ruth: {out['summary']}")
    if out.get("plan"):
        db.thread_append(name, key, "Ruth", "plan", out["plan"])
    if repro:
        db.thread_append(name, key, "Ruth", "test",
                         f"Reproduction test written for {repro_path} "
                         "(placed in the engineer's worktree; must fail "
                         "before the fix and pass after).")
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
    key = f"issue#{item['number']}"
    detail = gh.issue_detail(project["repo"], item["number"])
    branch = f"harness/issue-{item['number']}"
    wt, salvage = repo.add_worktree(project, branch)
    db.update_item(name, "issue", item["number"], status="working",
                   branch=branch)
    if salvage:
        # The branch is cut fresh from dev on every dispatch, so a retry has
        # to say where the last attempt's work went.
        db.thread_append(name, key, "harness", "event", salvage)
    repro_path = ""
    if item["repro_test"]:
        try:
            rt = json.loads(item["repro_test"])
            target = (wt / rt["path"]).resolve()
            if str(target).startswith(str(wt.resolve())):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(rt["content"])
                repro_path = rt["path"]
        except (ValueError, KeyError, OSError) as e:
            db.thread_append(name, key, "harness", "test",
                             f"Could not place the reproduction test: {e}")
    if repro_path and not item["session_id"]:
        # Prove the bug before touching it: the repro must fail on current code.
        passed, out = await asyncio.to_thread(repo.run_tests, project, wt, True)
        db.thread_append(name, key, "harness", "test",
                         ("Reproduction test FAILS on current code as expected "
                          "— bug confirmed." if not passed else
                          "Reproduction test PASSES on current code — it does "
                          "not reproduce the bug. Engineer: treat the plan with "
                          "care and fix or replace the test.")
                         + (f"\n{out[-600:]}" if not passed else ""))
    db.thread_append(name, key, persona, "event",
                     f"Starting the fix on branch {branch}"
                     + (" (resuming earlier session)" if item["session_id"] else ""))
    res = await agents.fix_issue(project, detail, item["plan"], str(wt),
                                 resume=item["session_id"] or None,
                                 persona=persona, repro_path=repro_path)
    if (not res["ok"] and item["session_id"]
            and "no conversation found" in (res["error"] or "").lower()):
        # The saved session did not survive whatever restarted the container.
        # Starting fresh here costs one wasted call; leaving it to the next
        # cycle costs a whole cycle and a circuit-breaker count.
        db.thread_append(name, key, persona, "event",
                         "No saved session for that id (likely lost in a "
                         "restart) — starting fresh in this run instead of "
                         "losing a cycle.")
        res = await agents.fix_issue(project, detail, item["plan"], str(wt),
                                     resume=None, persona=persona,
                                     repro_path=repro_path)
    db.update_item(name, "issue", item["number"],
                   session_id=res.get("session_id", ""))
    if not res["ok"] or not res["output"]["success"]:
        msg = (res["error"] or res["output"]["notes"]) if res["output"] \
            else res["error"]
        msg = (msg or "fix did not succeed")[:2000]
        db.thread_append(name, key, persona, "event",
                         f"Fix attempt did not succeed: {msg[:600]}")
        if res.get("cancelled"):
            # A human pressed Stop — never auto-retry over their decision.
            db.update_item(name, "issue", item["number"],
                           status="waiting_human", error=msg)
        elif res["ok"] and res["output"] and not res["output"]["success"]:
            # The engineer deliberately declined (too risky, unclear spec).
            # Retrying repeats the same honest refusal at full cost, so it
            # is held for Harry's ruling — and a second refusal after his
            # ruling is the operator's (hold_for_ruling's trip count), not
            # another round with the same engineer.
            db.log_event(f"{persona} declined issue #{item['number']}: "
                         f"{msg[:120]} — held for a ruling", "warn",
                         project=name)
            hold_item(project, db.get_item(name, "issue", item["number"]),
                      persona, f"{persona} declined the work",
                      f"{persona}: {msg[:600]}")
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
    _file_question(name, persona, key, out)
    db.thread_append(name, key, persona, "note",
                     out["summary"] + (f"\nNotes: {out['notes']}" if out.get("notes") else ""))

    if not repo.wt_has_changes(project, wt):
        hold_item(project, db.get_item(name, "issue", item["number"]), persona,
                  f"{persona} reported success but changed nothing",
                  f"{persona}: {out['summary'][:600]}")
        return

    # The deterministic gate: harness runs the tests itself, in the worktree.
    passed, test_out = await asyncio.to_thread(
        repo.run_tests, project, wt, False)
    if not passed:
        # One retry (the engineer resumes their session with the failure in
        # front of them); a second red run is a human's call, not a loop.
        again = (item["error"] or "").startswith("tests failed after fix")
        db.update_item(name, "issue", item["number"], status="approved",
                       error="tests failed after fix:\n" + test_out[-1500:])
        db.thread_append(name, key, "harness", "test",
                         "Tests FAILED after the fix — not pushed"
                         + (" — held for a ruling after two red runs." if again
                            else "; the engineer retries with this in front of them.")
                         + "\n" + test_out[-1200:])
        db.log_event(f"Issue #{item['number']}: tests failed — fix not pushed"
                     + (", held for a ruling after two red runs" if again
                        else ", retrying next cycle"), "warn", project=name)
        if again:
            hold_item(project, db.get_item(name, "issue", item["number"]),
                      persona, "tests failed after the fix twice running",
                      f"Second red run:\n{test_out[-800:]}")
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
        db.thread_append(name, key, "harness", "event",
                         f"Tests passed but the fix did not land on "
                         f"{project['dev_branch']}:\n{err[:1200]}")
        db.log_event(f"Issue #{item['number']}: {err[:160]} — retrying "
                     "next cycle", "warn", project=name)
        if repo.SAFETY_PUSH_FAILED in err:
            # The one case where the commit exists nowhere but this box:
            # say so plainly rather than leave it inside a truncated error.
            db.log_event(f"Issue #{item['number']}: the fix could not be "
                         f"pushed to origin/{branch} either — it exists only "
                         "in the worktree on this box", "warn", project=name)
        return
    repo.remove_worktree(project, wt)
    db.update_item(
        name, "issue", item["number"],
        status="queued", queued_at=db.now(), diff=diff,
        commits=msg, verdict_summary=out["summary"], error="",
        breaker_trips=0)
    db.thread_append(name, key, "harness", "event",
                     f"Tests passed; landed on {project['dev_branch']} as "
                     f"\"{msg.splitlines()[0][:100]}\"\n{stat[-800:]}")
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
        repo.remove_pr_run(project, item["number"])
        db.update_item(
            name, "pr", item["number"], status="waiting_human",
            verdict="needs_work",
            verdict_summary=f"does not merge cleanly onto {project['dev_branch']}",
            draft_comment=(f"Thanks for the PR! It currently conflicts with "
                           f"`{project['dev_branch']}` — could you rebase it? "
                           "Happy to take another look after that."))
        return
    # The contributor's tests and Ruth's read of them happen in the throwaway
    # checkout; it goes whatever the verdict, and nothing after here needs it.
    try:
        passed, test_out = await asyncio.to_thread(
            repo.run_pr_tests, project, item["number"])
        diff = gh.pr_diff(project["repo"], item["number"])
        res = await agents.review_pr(project, detail, diff,
                                     ("PASSED\n" if passed else "FAILED\n") + test_out,
                                     cwd)
    finally:
        repo.remove_pr_run(project, item["number"])
    if not res["ok"]:
        db.update_item(name, "pr", item["number"], error=res["error"])
        return
    out = res["output"]
    _file_question(name, "Ruth", f"pr#{item['number']}", out)
    verdict = out["verdict"]
    if verdict == "merge" and not passed:
        verdict = "needs_work"  # agents don't outrank the test suite
    db.thread_append(name, f"pr#{item['number']}", "Ruth", "finding",
                     f"Review verdict: {verdict} (valuable={out['valuable']}; "
                     f"tests {'passed' if passed else 'FAILED'})\n{out['summary']}"
                     + (f"\nRisks: {out['risks']}" if out.get("risks") else ""))
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
        if verdict in ("needs_work", "reject") and out["draft_review"] and \
                db.policy(name, "post_comments") == "auto":
            gh.comment_pr(project["repo"], item["number"], out["draft_review"])
            db.log_event(f"Posted review on PR #{item['number']}", project=name)
        # Not an auto-merge: Harry rules on it (merge, park, close out)
        # before the operator hears of it. Under merge_prs: approve his
        # "merge" lands it on their desk as a recommendation — the policy
        # makes the press theirs.
        hold_item(project, db.get_item(name, "pr", item["number"]), "Ruth",
                  f"Ruth's review verdict is {verdict}"
                  + ("" if passed else " (tests failed)"),
                  f"Ruth: {out['summary']}"
                  + (f"\nRisks: {out['risks']}" if out.get("risks") else ""))


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
            passed, out = await asyncio.to_thread(repo.run_pr_tests, project,
                                                  number)
    except CmdError as e:
        db.update_item(name, "pr", number, status="blocked",
                       error=f"does not merge cleanly onto "
                             f"{project['dev_branch']}: {e}"[:2000])
        db.log_event(f"PR #{number} does not merge cleanly onto "
                     f"{project['dev_branch']}", "warn", project=name)
        return False
    finally:
        repo.remove_pr_run(project, number)
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

def anything_to_release(project, queued=None) -> bool:
    """Would a release request actually have something to cut?

    Queued items are the usual case; dev ahead of main covers work that
    landed outside the harness. Neither means Release now would be a dead
    press, so the GUI asks this before offering the button — and asks it
    here, so button and cycle can never disagree.
    """
    if queued is None:
        queued = db.items_by_status(project["name"], "queued")
    return bool(queued) or repo.dev_ahead_count(project) > 0


# Time-based release cadences: policy value -> the length of one window in
# days. Anything not in here (the default, "changes") uses the two thresholds.
RELEASE_WINDOW_DAYS = {"daily": 1.0, "weekly": 7.0, "monthly": 30.0}


def release_window_days(project_name: str) -> float | None:
    """The window in days when this project releases on a clock, else None.

    Anything unrecognised reads as the default trigger rather than as no
    trigger at all: a typed-in policy value must never stop releases dead.
    """
    return RELEASE_WINDOW_DAYS.get(_release_schedule(project_name))


def _release_schedule(project_name: str) -> str:
    return (db.policy(project_name, "release_schedule") or "").strip().lower()


def release_trigger_phrase(project_name: str) -> str:
    """Plain English for what sets this project's next release off.

    One sentence fragment that reads after "once ...", so the project page and
    the desk digest describe the live trigger and only the live trigger.
    """
    schedule = _release_schedule(project_name)
    if schedule in RELEASE_WINDOW_DAYS:
        window = {"daily": "a day", "weekly": "a week",
                  "monthly": "a month"}[schedule]
        return f"{window} has passed since the last release"
    return (f"{db.policy(project_name, 'release_min_changes')} changes are "
            "queued or the oldest is "
            f"{db.policy(project_name, 'release_max_age_days')} days old")


def _age_days(ts: str) -> float:
    """How many days ago a stored UTC timestamp was."""
    return (datetime.now(timezone.utc)
            - datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc)).total_seconds() / 86400


def _scheduled_release_due(project, queued, window_days: float) -> list | None:
    """The clock-shaped trigger: at most one release per window.

    The window is anchored to the last release that actually went out, not to
    the oldest queued item, so the cut point does not drift with when work
    happened to land. A project that has never released is due as soon as it
    has anything to release.

    A window nothing landed in passes silently — no release, no warning. A
    window that was missed (desk offline, outside active hours, tests red)
    gives exactly one catch-up release on the next eligible cycle: the anchor
    only moves when a release is cut, so however many windows went by, what
    comes out is one release carrying everything since the last one.
    """
    last = db.last_release(project["name"])
    if last and _age_days(last["released_at"]) < window_days:
        return None
    if not anything_to_release(project, queued):
        return None
    return queued


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
        if anything_to_release(project, queued):
            return queued
        db.log_event(f"Release requested, but {project['dev_branch']} matches "
                     f"{project['main_branch']} and nothing is queued — "
                     "nothing to release", "warn", project=name)
        return None
    window_days = release_window_days(name)
    if window_days is not None:
        return _scheduled_release_due(project, queued, window_days)
    if not queued:
        return None
    min_changes = int(db.policy(name, "release_min_changes"))
    max_age_days = float(db.policy(name, "release_max_age_days"))
    if len(queued) >= min_changes:
        return queued
    oldest = min(q["queued_at"] or db.now() for q in queued)
    return queued if _age_days(oldest) >= max_age_days else None


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
        # Back to proposed with the reason attached: left at 'merging' the
        # card shows "reload for the result" forever, with no button and no
        # cause, until a restart sweeps it up.
        db.update_release(release["id"], status="proposed", error=str(e)[:2000])
        db.log_event(f"Release v{version} failed: {e}", "error", project=name)
        return
    try:
        gh.publish_release(project["repo"], f"v{version}", f"v{version}",
                           release["notes"])
    except CmdError as e:
        db.log_event(f"Tag pushed but GitHub release publish failed: {e}",
                     "warn", project=name)
    db.update_release(release["id"], status="released", released_at=db.now(),
                      error="")
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
        line = (f"- {it['kind']}#{it['number']} [{it['status']}] "
                f"{it['title']} (by {it['author']})")
        if it["status"] == "triaged" and it["kind"] == "issue":
            # Awaiting the lead's sign-off: give them the whole case, not a
            # 120-character glimpse of it.
            line += (f"\n  Ruth's verdict: {it['verdict']} — {it['verdict_summary']}"
                     f"\n  Plan:\n    " + (it["plan"] or "(none)").replace("\n", "\n    ")
                     + ("\n  A reproduction test is ready." if it["repro_test"] else ""))
        elif it["verdict"]:
            line += f" — verdict: {it['verdict']}: {it['verdict_summary'][:300]}"
        if it["error"]:
            line += f" — error: {it['error'][:160]}"
        lines.append(line)
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
                 f"Release policy: a release goes out once "
                 f"{release_trigger_phrase(name)}.")
    return "\n".join(lines) or "No open items."


LEAD_REVIEW_HOURS = 6   # the lead looks over a quiet board at least this often


def _since_last_plan(project) -> str:
    return db.get_setting(f"last_plan_at.{project['name']}", "")


def _budget_hold(project) -> bool:
    """True when the desk has spent its daily budget: no new agent work
    starts until the 24h window rolls on. Logged once per hold."""
    name = project["name"]
    try:
        cap = float(db.policy(name, "daily_budget_usd"))
    except ValueError:
        return False
    if cap <= 0:
        return False
    since = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=1)
             ).strftime("%Y-%m-%dT%H:%M:%SZ")
    spent = db.spend_since(name, since)
    key = f"budget_hold.{name}"
    if spent >= cap:
        if db.get_setting(key) != "1":
            db.set_setting(key, "1")
            db.log_event(f"Daily budget reached (${spent:.2f} of ${cap:.0f} in "
                         "24h) — agent work on this desk pauses until it "
                         "rolls off; raise daily_budget_usd to continue",
                         "warn", project=name)
            notify.send(f"{name}: daily budget reached",
                        f"${spent:.2f} spent in 24h; the desk is paused until "
                        "the window rolls on.", tags="moneybag",
                        click_path=f"/p/{name}/settings")
        return True
    if db.get_setting(key) == "1":
        db.set_setting(key, "")
        db.log_event("Daily budget window rolled on — desk resumes", project=name)
    return False


def _backlog_key(approved) -> str:
    return json.dumps(sorted(i["number"] for i in approved))


def _backlog_grew(name: str, approved) -> bool:
    """True if an approved item exists that was not in the backlog the lead
    last planned over."""
    try:
        seen = set(json.loads(db.get_setting(f"plan_backlog.{name}", "[]")))
    except ValueError:
        seen = set()
    return any(i["number"] not in seen for i in approved)


def desk_events(project) -> list[str]:
    """Why the lead should plan now. Empty means nothing has changed since
    the last plan that needs a lead's judgement — no plan, no cost."""
    name = project["name"]
    since = _since_last_plan(project)
    reasons = []
    if db.get_setting(f"directives.{name}", "").strip():
        reasons.append("directive from Harry")
    if db.answers_since(name, since):
        # The operator has decided something since the last plan. Whatever it
        # was, the desk shouldn't wait for a poll to notice: this is also
        # what makes work_ready() bring the worker straight back after an
        # answer, so the wave runs on the cycle the answer triggered.
        reasons.append("the operator has answered a question")
    fix_policy = db.policy(name, "fix_issues")
    triaged = [i for i in db.items_by_status(name, "triaged")
               if i["kind"] == "issue" and i["gh_state"] == "open"]
    if fix_policy == "lead" and any(i["updated_at"] > since for i in triaged):
        reasons.append("triage results awaiting sign-off")
    approved = [i for i in db.items_by_status(name, "approved")
                if i["kind"] == "issue" and not i["error"]]
    engineers = 1 + len(db.staff_get(name)["extra"])
    # Ordering is a judgement call only when the backlog has *new* members
    # since the lead last ordered it. A retry, a restart requeue or a
    # failed attempt bumps updated_at but changes nothing the lead must
    # decide — those used to re-plan the desk every sweep.
    if len(approved) > engineers and _backlog_grew(name, approved):
        reasons.append("backlog has new items beyond the engineers — ordering needed")
    open_items = [i for i in db.project_items(name) if i["gh_state"] == "open"]
    if open_items and not since:
        reasons.append("first look at this desk")
    elif open_items and since:
        age_h = (datetime.now(timezone.utc)
                 - datetime.strptime(since, "%Y-%m-%dT%H:%M:%SZ")
                 .replace(tzinfo=timezone.utc)).total_seconds() / 3600
        if age_h >= LEAD_REVIEW_HOURS:
            reasons.append(f"routine review ({LEAD_REVIEW_HOURS}h since last plan)")
    return reasons


ATTEND_INTERVAL_S = 20   # how often a long step looks for new directions


def _loop_lock(name: str) -> asyncio.Lock:
    """A named asyncio.Lock scoped to the running event loop.

    Desks run concurrently and several of them (plus the attendants inside
    engineer waves) call process_directives/process_questions; without a
    lock two callers can both pick up the same pending row and have Harry
    action it twice. The worker runs one long-lived loop for the whole
    section, so hanging the locks off it keeps every desk's wake loop and
    every attendant under the same lock, while tests that call in with
    `asyncio.run` still get a set of their own."""
    loop = asyncio.get_running_loop()
    locks = getattr(loop, "_harness_locks", None)
    if locks is None:
        locks = {}
        loop._harness_locks = locks
    return locks.setdefault(name, asyncio.Lock())


class _Attendant:
    """Actions operator directions while a long step (an engineer wave,
    a run of triages) is in progress, so a direction typed mid-cycle is
    with Harry within a minute rather than after the sweep.

        async with _Attendant():
            await asyncio.gather(...)

    Harry's directive run is read-only and has no worktree, so it is safe
    alongside engineers — it is the same concurrency `ask_harry` already
    relies on. Failures are logged, never raised into the step."""

    def __init__(self):
        self._stop = asyncio.Event()
        self._task = None

    async def _loop(self):
        while not self._stop.is_set():
            try:
                await process_directives()
            except Exception as e:  # never take the wave down
                db.log_event(f"Directive attendant: {type(e).__name__}: {e}",
                             "warn")
            try:
                await asyncio.wait_for(self._stop.wait(), ATTEND_INTERVAL_S)
            except asyncio.TimeoutError:
                pass

    async def __aenter__(self):
        self._task = asyncio.create_task(self._loop())
        return self

    async def __aexit__(self, *exc):
        self._stop.set()
        if self._task:
            await self._task
        return False


async def run_cycle(project, force: bool = False) -> None:
    """One pass over one desk. Event-driven: new items are triaged or
    reviewed straight away, the lead plans only when something needs their
    judgement, and signed-off fixes run in a wave. Cheap when nothing has
    changed — a sync and no agent runs."""
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

        # An answer the operator has given is an instruction: act on it
        # before any work is chosen, so the item it is about is in the right
        # queue for this very cycle rather than the next one.
        route_answers(project)

        # Release first: a due release must never be starved by a long
        # sweep (or a restart mid-sweep).
        queued = _release_due(project)
        if queued is not None:
            await propose_release(project, queued)

        if _budget_hold(project):
            return

        done = 0
        # 1. Anything new gets looked at now — triage and review are the
        #    section's reflexes, not something the lead has to schedule.
        for item in db.items_by_status(name, "new"):
            if done >= MAX_AGENT_TASKS_PER_CYCLE:
                break
            await process_directives()   # a direction never waits on a triage queue
            if item["kind"] == "issue":
                await triage_item(project, item)
            else:
                with repo.clone_lock(project):
                    await review_item(project, item)
            done += 1
        if done:
            await process_questions(name)

        # 2. The lead plans when there is something to decide.
        # A forced cycle ("Run cycle now", an approval, an answer) syncs and
        # starts ready work; it is not a reason for every lead to re-plan.
        reasons = desk_events(project)
        staff = db.staff_get(name)
        engineers = ["Malcolm"] + staff["extra"]
        fix_policy = db.policy(name, "fix_issues")
        if reasons:
            cwd = str(repo.clean_checkout(project, project["dev_branch"]))
            db.set_setting(f"last_plan_at.{name}", db.now())
            db.set_setting(f"plan_backlog.{name}", _backlog_key(
                [i for i in db.items_by_status(name, "approved")
                 if i["kind"] == "issue" and not i["error"]]))
            digest = _state_digest(project) + \
                "\n\nYou are planning because: " + "; ".join(reasons) + "."
            plan_res = await agents.lead_plan(project, digest, cwd)
            if plan_res["ok"]:
                out = plan_res["output"]
                db.save_report("lead", name, out["summary"])
                _file_question(name, project["lead_name"], "", out)
                db.set_setting(f"directives.{name}", "")  # consumed by this plan
                req = (out.get("staffing_request") or "").strip()
                if req:
                    db.set_setting(f"staffing_request.{name}", req)
                    db.log_event(f"{project['lead_name']} asked Harry for "
                                 f"staffing: {req[:150]}", project=name)
                _open_tracking_issues(project, out.get("new_issues"))
                for t in out["tasks"]:
                    item = db.get_item(name, t["kind"], t["number"])
                    if item is None or t["kind"] != "issue":
                        continue
                    if t["action"] == "fix" and item["status"] == "triaged" \
                            and fix_policy in ("auto", "lead"):
                        db.update_item(name, "issue", item["number"],
                                       status="approved")
                        db.thread_append(name, f"issue#{item['number']}",
                                         project["lead_name"], "ruling",
                                         "Signed off for an engineer: "
                                         + (t.get("reason") or "")[:400])
                        db.log_event(f"{project['lead_name']} put an engineer "
                                     f"on issue #{item['number']}: "
                                     f"{t.get('reason', '')[:120]}", project=name)
                    elif t["action"] == "skip" and item["status"] == "triaged":
                        db.thread_append(name, f"issue#{item['number']}",
                                         project["lead_name"], "ruling",
                                         "Not this time: " + (t.get("reason") or "")[:400])
            await process_questions(name)

        # 3. Signed-off fixes run as a wave: one engineer per job, each in an
        #    isolated worktree. Fresh approvals ahead of retries.
        approved = sorted(db.items_by_status(name, "approved"),
                          key=lambda i: bool(i["error"]))
        wave = []
        for item in approved:
            if item["kind"] == "issue":
                wave.append(item)
            else:
                await merge_pr_item(project, item)
        wave = wave[:len(engineers)]
        if wave:
            await asyncio.to_thread(repo.ensure_test_env, project)
            async with _Attendant():
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
            await process_questions(name)

        queued = _release_due(project)
        if queued is not None:
            await propose_release(project, queued)  # catches same-cycle landings
    except AgentStalled:
        db.log_event(f"Cycle for {name} paused mid-way; will resume", "warn",
                     project=name)


def work_ready(project) -> bool:
    """True when the desk can start more work without anyone's click —
    the worker comes straight back rather than waiting for the next sync.

    Deliberately narrow: fresh approvals and fresh new items (not ones
    carrying an error — those wait for the normal poll so a failing run
    can't spin), triage results the lead hasn't seen, a pending directive.
    Never outside active hours or under a budget hold."""
    name = project["name"]
    if not within_active_hours(name) or db.get_setting(f"budget_hold.{name}") == "1":
        return False
    if any(not i["error"] for i in db.items_by_status(name, "approved", "new")
           if i["gh_state"] == "open"):
        return True
    return bool(desk_events(project))


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
    on your own repo is the lowest-stakes outward action there is. The only
    bound is the desk's open, unworked backlog of lead-filed issues: filings
    that have been triaged, fixed or closed cost nothing."""
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
    open_lead_items = sum(1 for i in items
                          if i["author"] == project["lead_name"]
                          and i["gh_state"] == "open"
                          and i["status"] in ("new", "triaged"))
    for ni in new_issues[:OPEN_TRACKING_ISSUES_CAP]:
        title = (ni.get("title") or "").strip()
        if open_lead_items >= OPEN_TRACKING_ISSUES_CAP:
            db.log_event(f"{project['lead_name']} wanted to open "
                         f"'{title[:80]}' but their backlog of open, "
                         "unworked tracking issues is at its "
                         f"cap ({OPEN_TRACKING_ISSUES_CAP}) — not filed "
                         "until some of that queue is worked through",
                         "warn", project=name)
            break
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
        open_lead_items += 1
        db.log_event(f"{project['lead_name']} opened issue #{num}: "
                     f"{title[:80]}", project=name)


# --- Harry's inbox -------------------------------------------------------------

# Ruling passes a question may sit through undecided before it is the
# operator's. Counted on the questions row (db.bump_ruling_passes).
UNDECIDED_PASSES = 2


async def process_questions(project_name: str | None = None) -> None:
    async with _loop_lock("questions"):
        return await _process_questions_locked(project_name)


async def _process_questions_locked(project_name: str | None = None) -> None:
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
    # The count is kept on the question row, not in this process: the harness
    # restarts on every release, and a counter that went with it would let a
    # question cycle for ever without ever reaching the operator.
    for q in inbox:
        if db.question(q["id"])["status"] != "open":
            continue
        if db.bump_ruling_passes(q["id"]) >= UNDECIDED_PASSES:
            if is_breaker_question(q):
                # The item cannot sit held with nobody ruling on it.
                apply_breaker_ruling(q, "escalate", "no ruling after two passes")
            else:
                park_held_item(q, "no ruling after two passes")
            db.escalate_question(q["id"])
            db.log_event(f"Harry left {q['asked_by']}'s question undecided "
                         f"twice — escalated to {config.OPERATOR}", "warn",
                         project=q["project"])


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


# --- blockers carried between stand-ups --------------------------------------
# A blocker Harry names is only worth naming if someone then acts on it. Each
# desk's blockers are kept until the next stand-up, which reports them back
# with what has actually moved since — so a repeat is visibly a repeat.

MAX_KEPT_BLOCKERS = 6      # per desk; a stand-up naming more is its own problem


def _norm_blocker(message: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", " ", message.lower()).strip()


def _blocker_items(project: str, message: str) -> dict:
    """Items a blocker names, with the status they were in when it was named.

    'issue#12', 'pr#5' and a bare '#302' all count; a bare number is matched
    against both kinds, since Harry writes as he speaks."""
    import re
    found = {}
    for kind, num in re.findall(r"(?:\b(issue|pr)\s*)?#(\d+)\b",
                                message, re.I)[:5]:
        for k in ([kind.lower()] if kind else ["issue", "pr"]):
            it = db.get_item(project, k, int(num))
            if it:
                found[f"{k}#{num}"] = it["status"]
    return found


def _desk_activity(project: str, since: str) -> list[str]:
    """Work on a desk since `since` — runs and the desk's own events.

    Stand-up's own lines are skipped: naming a blocker (and directing the
    lead about it) must not read as the blocker having moved."""
    moved = []
    for r in db.recent_runs(30, project):
        if r["started_at"] > since:
            moved.append(f"{r['agent'] or r['role']} ran {r['task']}"
                         + (f" on {r['item_key']}" if r["item_key"] else ""))
    for e in db.recent_events(60, project):
        if (e["project"] == project and e["ts"] > since
                and not e["message"].startswith("Stand-up")):
            moved.append(e["message"][:120])
    return moved


def _blocker_change(project: str, b: dict) -> str:
    """What has moved on a blocker since it was named; empty if nothing has.

    Judged on facts, not on wording. When the blocker names an item, that
    item is the test — it moving to a new status, or being worked, is
    progress, and activity elsewhere on the desk is not. When it names no
    item, the desk's own runs and events since stand in."""
    since = b.get("at", "")
    items = b.get("items") or {}
    moved = []
    since_runs = ([r for r in db.recent_runs(30, project)
                   if r["started_at"] > since] if items else [])
    for key, was in items.items():
        kind, _, num = key.partition("#")
        it = db.get_item(project, kind, int(num))
        now_status = it["status"] if it else "no longer tracked"
        if now_status != was:
            moved.append(f"{key} {was} → {now_status}")
        runs = sum(1 for r in since_runs if r["item_key"] == key)
        if runs:
            moved.append(f"{runs} run(s) on {key}")
    if not items:
        moved += _desk_activity(project, since)
    return "; ".join(moved[:4])


def _prior_blockers(project: str) -> list:
    try:
        rows = json.loads(db.get_setting(f"standup_blockers.{project}", "[]"))
        return rows if isinstance(rows, list) else []
    except ValueError:
        return []


def _record_blockers(blockers: list) -> None:
    """Keep each desk's blockers for the next stand-up to follow up.

    Written at the end of the stand-up, once Harry's rulings and directives
    are in, so that his own bookkeeping falls before the recorded time and
    cannot be mistaken for movement. A blocker named again keeps counting:
    `repeats` is how many stand-ups running it has been raised."""
    by_project = {}
    for b in blockers:
        project, msg = b.get("project", ""), (b.get("message") or "").strip()
        if msg and db.get_project(project):
            by_project.setdefault(project, []).append(msg)
    for p in db.all_projects(enabled_only=True):
        name = p["name"]
        was = {_norm_blocker(b.get("message", "")): b
               for b in _prior_blockers(name)}
        rows = [{"message": msg[:400], "at": db.now(),
                 "repeats": was.get(_norm_blocker(msg), {}).get("repeats", 0) + 1,
                 "items": _blocker_items(name, msg)}
                for msg in by_project.get(name, [])[:MAX_KEPT_BLOCKERS]]
        db.set_setting(f"standup_blockers.{name}", json.dumps(rows))


def _blocker_followup(project: str) -> list[str]:
    """The digest section that makes last stand-up's blockers answerable."""
    prior = _prior_blockers(project)
    if not prior:
        return []
    lines = ["Blockers you named last stand-up, with what changed since:"]
    for b in prior:
        change = _blocker_change(project, b)
        repeats = b.get("repeats", 1)
        lines.append(
            f"- {b.get('message', '')[:200]}"
            + (f" [named at {repeats} stand-ups running]" if repeats > 1 else "")
            + (f" — changed: {change}" if change
               else " — unchanged: no activity since"))
    return lines


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
    ruled = db.operator_rulings_since(_hours_ago(STANDUP_ASK_WINDOW_H))
    if ruled:
        sections.append(
            f"{config.OPERATOR}'s answers to your own questions in the last "
            f"{STANDUP_ASK_WINDOW_H}h (binding — act on them, do not ask "
            "again):\n" + "\n".join(
                f"- [{q['project'] or 'section'}"
                f"{' ' + q['item_key'] if q['item_key'] else ''}] "
                f"Q: {q['question'][:160]}\n  A ({q['answered_at']}): "
                f"{q['answer'][:200]}" for q in ruled))
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
        for it in db.items_by_status(name, "held"):
            if it["gh_state"] == "open":
                lines.append(f"HELD (yours to rule on) {it['kind']}#"
                             f"{it['number']} ({age_days(it['updated_at'])}): "
                             f"{it['error'][:200]}")
        for it in db.items_by_status(name, "waiting_human"):
            if it["gh_state"] != "open":
                continue
            err = it["error"] or ""
            if err.startswith("parked by Harry"):
                who = "parked by your own ruling"
            else:
                who = (f"with {config.OPERATOR} (their call, not a blocker "
                       "of yours)")
            lines.append(f"{who}: {it['kind']}#{it['number']} "
                         f"for {age_days(it['updated_at'])}: "
                         f"{it['verdict']} — {it['title'][:80]}"
                         + (f" — {err[:120]}" if err else ""))
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
        lines += _blocker_followup(name)
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


STANDUP_ASK_WINDOW_H = 24   # Harry asks the operator about a thing once a day


def _hours_ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - __import__("datetime")
            .timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mentioned_projects(text: str) -> list[str]:
    import re
    return [p["name"] for p in db.all_projects(enabled_only=True)
            if re.search(r"(?<![a-z0-9])" + re.escape(p["name"].lower())
                         + r"(?![a-z0-9])", text.lower())]


def standup_question_target(text: str) -> tuple[str, str, list[str]]:
    """(project, item_key, other item keys) a stand-up question is about.

    Harry writes as he speaks — "roan has four features waiting", "#302 on
    may" — so the project is taken from the desk he names, and the items
    from the numbers he gives on that desk (or on any desk when he names
    none). One project and one item key go on the record, so the answer
    routes and the next stand-up's dedupe matches; the rest ride in the
    text."""
    named = _mentioned_projects(text)
    keys = []
    for pname in named or [p["name"] for p in db.all_projects(enabled_only=True)]:
        for key in _blocker_items(pname, text):
            keys.append((pname, key))
    if keys:
        project = keys[0][0]
        mine = [k for p, k in keys if p == project]
        return project, mine[0], mine[1:]
    return (named[0] if named else ""), "", []


def file_standup_question(out: dict) -> int | None:
    """File what Harry could not decide at stand-up — to the operator, on the
    record of the item or desk it is about, and once.

    Dropped when he gives no reason it is outside his remit: a question he
    could have answered is a directive he did not issue, not an escalation.
    Dropped as well when the same item (or the same desk, for a question
    naming no item) is already in front of the operator, or was ruled on
    within STANDUP_ASK_WINDOW_H — the digest carries that ruling back to
    him, and an event says so, rather than the operator hearing the same
    question in new words every hour."""
    text = (out.get("question_for_human") or "").strip()
    if not text:
        return None
    reason = (out.get("outside_remit_reason") or "").strip()
    if not reason:
        db.log_event("Stand-up: Harry raised a question without saying why "
                     "it is the operator's rather than his — dropped; a call "
                     f"he can make is a directive: {text[:120]}", "warn")
        return None
    project, key, rest = standup_question_target(text)
    prior = db.harry_prior_question(project, key,
                                    _hours_ago(STANDUP_ASK_WINDOW_H))
    about = key or (project and f"the {project} desk") or "the section"
    if prior is not None and prior["status"] in ("open", "escalated"):
        db.log_event(f"Stand-up: Harry's question about {about} is already "
                     f"with {config.OPERATOR} (asked {prior['created_at']}) — "
                     "not filed again", project=project)
        return None
    if prior is not None:
        db.log_event(f"Stand-up: {config.OPERATOR} already ruled on {about} "
                     f"at {prior['answered_at']}: {prior['answer'][:160]} — "
                     "Harry's question not filed again; the ruling stands",
                     project=project)
        return None
    if rest:
        text += "\nAlso concerns: " + ", ".join(rest) + "."
    text += f"\nWhy this is yours: {reason}"
    return db.ask_question(project, config.CTO_NAME, key, text,
                           options=out.get("question_options"))


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
    file_standup_question(out)
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
    _record_blockers(out.get("blockers", []))
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
            if is_breaker_question(q):
                # A held item needs the ruling carried out, not just recorded.
                apply_breaker_ruling(q, d.get("item_action", ""),
                                     d["answer"].strip())
            elif held_item_for(q):
                # Same for an item held for a Fix/Skip/Won't fix ruling: it
                # moves now, on the ruling, not on the next sync.
                project = db.get_project(q["project"])
                if project:
                    route_answers(project)
        elif d["action"] == "escalate":
            reason = (d.get("outside_remit_reason") or "").strip()
            if is_breaker_question(q):
                apply_breaker_ruling(q, "escalate", d.get("answer", "").strip())
            else:
                park_held_item(q, reason or d.get("answer", "").strip())
            db.escalate_question(q["id"])
            db.log_event(f"Harry escalated {q['asked_by']}'s question to "
                         f"{config.OPERATOR}"
                         + (f": {reason[:150]}" if reason else
                            " without saying why it is theirs"),
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


# --- closing an item out -----------------------------------------------------

def close_item(project, kind: str, number: int, reason: str = "") -> bool:
    """Finish an item that is already done: the fix shipped some other way,
    or the work has landed and only the paperwork is outstanding.

    Closed is not rejected. Rejected means we are not doing the work; closed
    means it is done. An issue is closed on GitHub too, because an issue
    left open comes back round the loop — sync keeps it, the lead's state
    digest still lists it, and the next plan puts an engineer back on
    finished work. A PR is only closed on our board: closing someone else's
    PR is not ours to do.

    Returns False for an item we don't know about."""
    name = project["name"]
    item = db.get_item(name, kind, number)
    if not item:
        return False
    reason = (reason or "").strip()
    fields = {"status": "closed", "error": "", "session_id": ""}
    if kind == "issue" and item["gh_state"] == "open":
        comment = (f"Closed as already shipped: {reason}" if reason
                   else "Closed: this work is already done.")
        try:
            gh.close_issue(project["repo"], number, comment)
            fields["gh_state"] = "closed"
        except CmdError as e:
            # Local status still moves — the item must leave the queues
            # either way — but the operator needs to know GitHub didn't take.
            db.log_event(f"Closed {kind}#{number} on the board but the "
                         f"GitHub close failed: {e}", "warn", project=name)
    db.update_item(name, kind, number, **fields)
    return True


# --- operator directives -----------------------------------------------------

def _apply_directive_actions(project, actions: list,
                             reset_trips: bool = True) -> list[str]:
    """Deterministically execute Harry's directive actions. Every action is
    something the GUI could already do — no new privileges.

    `reset_trips` forgives the item's circuit-breaker trips along with the
    failure window. That is right when the operator says "try again" and
    wrong for a ruling on a held item, which must not be able to buy itself
    an unlimited supply of retries."""
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
                if act in ("approve_item", "retry_item"):
                    fields["breaker_reset_at"] = db.now()
                    if reset_trips:
                        fields["breaker_trips"] = 0
                if act == "retry_item":
                    fields.update(error="", session_id="")
                db.update_item(name, kind, num, **fields)
                done.append(f"{act} {kind}#{num}")
            elif act == "close_item":
                kind, num = a.get("kind"), a.get("number")
                why = (a.get("reason") or "").strip()
                if kind and num and close_item(project, kind, num, why):
                    done.append(f"closed {kind}#{num} as done"
                                + (f": {why[:80]}" if why else ""))
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
    async with _loop_lock("directives"):
        await _process_directives_locked()


async def _process_directives_locked() -> None:
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
