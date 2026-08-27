"""Harness GUI.

Server-rendered, mobile-first, no build step. Meant to sit behind
`tailscale serve` (or similar) — no auth of its own, so never expose it with
Funnel. If Tailscale identity headers are present they are shown in the nav.
"""
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import config, db, gh, housekeeping, pipeline, repo, worker
from . import commands

BASE = Path(__file__).parent
app = FastAPI(title="Harness")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _own_hosts(request: Request) -> set[str]:
    """Every host name this request could legitimately have been sent to."""
    hosts = {request.headers.get("host", "")}
    hosts.update(h.strip() for h
                 in request.headers.get("x-forwarded-host", "").split(","))
    if config.PUBLIC_URL:
        hosts.add(urlparse(config.PUBLIC_URL).netloc)
    return {h.lower() for h in hosts if h}


@app.middleware("http")
async def block_cross_site(request: Request, call_next):
    """Refuse state-changing requests set off by another site.

    The GUI has no auth of its own, so any page in the operator's browser
    could otherwise POST to /add — which stores shell commands the harness
    later runs. Checking here rather than with per-form tokens means routes
    added later are covered without anyone remembering to opt in, and the
    existing forms and fetch() calls need no change.

    `same-site` is refused along with `cross-site`: no in-app flow produces
    it, and on a tailnet (`ts.net` is a public suffix) a page served by
    another machine on the tailnet would otherwise qualify. Clients that
    send neither header — the ntfy action buttons, curl, health tooling —
    are let through: every current browser sends at least one of the two on
    a cross-origin form POST.
    """
    if request.method.upper() in SAFE_METHODS:
        return await call_next(request)
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        ok = site.lower() in ("same-origin", "none")
    else:
        # Older browsers, and anything behind a proxy that strips the
        # Sec-Fetch headers: fall back to comparing Origin with the host.
        origin = request.headers.get("origin")
        ok = (not origin
              or urlparse(origin).netloc.lower() in _own_hosts(request))
    if not ok:
        return PlainTextResponse("cross-site request refused", status_code=403)
    return await call_next(request)


def agent_name(role: str, task: str, lead_name: str = "") -> str:
    return config.persona(role, task, lead_name)


templates.env.globals["agent_name"] = agent_name
templates.env.filters["money"] = lambda v: f"≈US${(v or 0):,.2f}"


@app.get("/health")
def health():
    st = worker.status()
    ok = st["alive"] and not st["stale"]
    from fastapi.responses import JSONResponse
    return JSONResponse(
        {"ok": ok, "worker_alive": st["alive"],
         "heartbeat_age_s": st["heartbeat_age"], "stale": st["stale"]},
        status_code=200 if ok else 503)


def active_project(request: Request, ctx: dict) -> str:
    """Which project the page belongs to, for the nav.

    The path parameter covers every `/p/<name>/...` page; a run page has no
    name in its path, so fall back to the run's own project. Runs with no
    project (Harry's cross-project work) highlight nothing."""
    name = request.path_params.get("name")
    if not name and ctx.get("run") is not None:
        name = ctx["run"]["project"]
    return name or ""


def render(request: Request, template: str, **ctx):
    ctx.update(
        request=request,
        active_project=active_project(request, ctx),
        operator=config.OPERATOR,
        version=config.DISPLAY_VERSION,
        projects=db.all_projects(),
        paused=db.paused_until(),
        maintenance=db.maintenance(),
        paused_reason=db.get_setting("paused_reason"),
        worker=worker.status(),
        who=request.headers.get("Tailscale-User-Login", ""),
        cheatsheet=commands.CHEATSHEET,
        theme="dark" if request.cookies.get("theme") == "dark" else "light",
    )
    return templates.TemplateResponse(request, template, ctx)


# --- overview ---------------------------------------------------------------

OVERVIEW_TABS = {
    "projects": "projects & section",
    "activity": "recent activity",
}


@app.get("/")
def overview(request: Request):
    tab = request.query_params.get("tab", "")
    if tab not in OVERVIEW_TABS:
        tab = "projects"          # unknown or missing: the default view
    cards = []
    for p in db.all_projects():
        counts = db.counts_by_status(p["name"])
        waiting = counts.get("waiting_human", 0)
        rel = db.open_release(p["name"])
        pending = db.get_setting(f"release_requested.{p['name']}") == "1"
        auto = db.policy(p["name"], "cut_release") == "auto"
        can_release = (not rel and not pending
                       and pipeline.anything_to_release(p))
        if rel and not auto:
            waiting += 1  # an auto release is not waiting on anybody
        cards.append({
            "project": p,
            "counts": counts,
            "waiting": waiting,
            "queued": counts.get("queued", 0),
            "release": rel,
            "release_auto": auto,
            "release_pending": pending,
            "can_release": can_release,
            "cost": db.total_cost(p["name"]),
            "lead_report": db.latest_report("lead", p["name"]),
        })
    return render(request, "overview.html",
                  cards=cards,
                  questions=_enrich_questions(db.escalated_questions()),
                  harry_inbox=_enrich_questions(db.harry_inbox()),
                  staff=_staff_board(),
                  cto_report=db.latest_report("cto"),
                  events=db.recent_events(20) if tab == "activity" else [],
                  total_cost=db.total_cost(),
                  tab=tab,
                  tabs=list(OVERVIEW_TABS.items()))


# --- project pages ----------------------------------------------------------

KANBAN_COLUMNS = [
    ("Inbox", "Ruth", ("new",)),
    ("Assessed", "Ruth", ("triaged",)),
    ("In progress", "Malcolm", ("approved", "working")),
    ("With Harry", "Harry", ("held",)),
    ("Your decision", "you", ("waiting_human",)),
    ("Blocked", "—", ("blocked",)),
    ("Release queue", "Colin", ("queued",)),
    ("Done", "", ("released", "closed", "rejected")),
]

AGENT_ROSTER = [
    ("lead", ("plan",)),
    ("ic", ("triage", "review")),      # Ruth
    ("ic", ("fix",)),                  # Malcolm
    ("ic", ("release",)),              # Colin
    ("ic", ("security",)),             # Zaf
    ("admin", ("notes",)),             # Tariq
]


def _member_status(display, runs, match):
    last = next((r for r in runs if match(r)), None)
    if not last:
        return {"name": display, "state": "idle", "detail": "no runs yet",
                "run_id": None}
    what = f"{last['task']} {last['item_key']}".strip()
    if last["finished_at"] is None:
        return {"name": display, "state": "working", "detail": what,
                "run_id": last["id"]}
    if last["ok"]:
        return {"name": display, "state": "ok",
                "detail": f"{what} · {last['started_at']}",
                "run_id": last["id"]}
    if last["summary"] == db.ORPHANED_SUMMARY:
        # A restart interrupted this run; the item was requeued
        # automatically. Not a real failure — don't dress it as one.
        return {"name": display, "state": "restarted",
                "detail": f"{what} — interrupted by a restart, requeued",
                "run_id": last["id"]}
    if last["summary"] == db.HOUSEKEEPING_ORPHAN_SUMMARY:
        # Housekeeping swept this one up: no result after hours, with the
        # process still up. It might be a hung agent, so say what happened
        # rather than blaming a restart that never occurred.
        return {"name": display, "state": "orphaned",
                "detail": f"{what} — no result recorded after "
                          f"{housekeeping.ORPHAN_RUN_HOURS}h",
                "run_id": last["id"]}
    return {"name": display, "state": "failed",
            "detail": f"{what} — {(last['summary'] or 'no reason recorded')[:90]}",
            "run_id": last["id"]}


ANSWER_HINTS = {
    "proceed": "puts it back in the flow — an engineer picks it up",
    "hold": "leaves it waiting on you, with your reason on the thread",
    "reject": "closes it out",
}


def _enrich_questions(qs):
    """Attach the referenced item's title/link so the operator can see what a
    question is actually about, and what each option button will do to it."""
    out = []
    for q in qs:
        d = dict(q)
        d["options_list"] = db.question_options(q)
        d["option_hints"] = {
            o: ANSWER_HINTS.get(db.answer_action(o), "") if q["item_key"] else ""
            for o in d["options_list"]}
        d["item_title"], d["item_url"] = "", ""
        if q["item_key"] and "#" in q["item_key"] and q["project"]:
            kind, _, num = q["item_key"].partition("#")
            item = db.get_item(q["project"], kind, int(num))
            if item:
                d["item_title"] = item["title"]
                d["item_url"] = f"/p/{q['project']}/{kind}/{num}"
        out.append(d)
    return out


def _staff_board():
    """Everyone in the section, grouped, with live status."""
    runs = db.recent_runs(300)
    groups = [{
        "group": "Section",
        "members": [
            _member_status(config.CTO_NAME, runs, lambda r: r["role"] == "cto"),
            _member_status(config.ADMIN_NAME, runs, lambda r: r["role"] == "admin"),
        ],
    }]
    for p in db.all_projects(enabled_only=True):
        name = p["name"]
        staff = db.staff_get(name)
        members = [_member_status(
            f"{p['lead_name']} (lead)", runs,
            lambda r, n=name: r["project"] == n and r["role"] == "lead")]
        for tasks, display in ((("triage", "review"), "Ruth"),
                               (("fix",), "Malcolm"),
                               (("release",), "Colin"),
                               (("security",), "Zaf")):
            m = _member_status(
                display, runs,
                lambda r, n=name, t=tasks, d=display: r["project"] == n
                and r["role"] == "ic" and r["task"] in t
                and (r["agent"] or d) == d)
            if display in staff["benched"]:
                m["state"] = "benched"
                m["detail"] = "stood down by Harry"
            members.append(m)
        for extra in staff["extra"]:
            members.append(_member_status(
                f"{extra} (hired)", runs,
                lambda r, n=name, e=extra: r["project"] == n
                and r["agent"] == e))
        groups.append({"group": name, "members": members})
    return groups


def _agent_roster(p, runs):
    """Latest activity per persona, from the run history."""
    roster = []
    for role, tasks in AGENT_ROSTER:
        display = agent_name(role, tasks[0], p["lead_name"])
        last = next((r for r in runs
                     if r["role"] == role and r["task"] in tasks), None)
        state = "idle"
        detail = "no runs yet"
        if last:
            if last["finished_at"] is None:
                state = "working"
                detail = f"{last['task']} {last['item_key']}".strip()
            else:
                state = "ok" if last["ok"] else "failed"
                detail = f"{last['task']} {last['item_key']}".strip() +                          f" · {last['started_at']}"
        roster.append({"name": display, "state": state, "detail": detail})
    return roster


@app.get("/p/{name}")
def project_page(request: Request, name: str):
    p = db.get_project(name)
    if not p:
        return RedirectResponse("/", status_code=303)
    items = db.project_items(name)
    runs = db.recent_runs(80, name)
    # Who is (or was last) on each item: live run wins, else latest run.
    activity = {}
    for r in reversed(runs):
        if r["item_key"] and r["agent"]:
            live = r["finished_at"] is None
            prev = activity.get(r["item_key"])
            if prev is None or live or not prev["live"]:
                activity[r["item_key"]] = {"agent": r["agent"], "live": live,
                                           "ok": r["ok"]}
    recent = {"released", "closed", "rejected"}
    board = []
    for title, agent, statuses in KANBAN_COLUMNS:
        cards = []
        for i in items:
            if i["status"] not in statuses or                     (i["status"] in recent and i["gh_state"] == "open"):
                continue
            act = activity.get(f"{i['kind']}#{i['number']}")
            cards.append({"i": i, "act": act,
                          "live": bool(act and act["live"])})
        cards.sort(key=lambda c: not c["live"])  # active work first
        if statuses == ("released", "closed", "rejected"):
            cards = cards[:10]
        board.append({"title": title, "agent": agent, "cards": cards,
                      "live_count": sum(1 for c in cards if c["live"])})
    return render(
        request, "project.html", p=p, items=items, board=board,
        harry_line=db.latest_report("harry", name),
        roster=_agent_roster(p, runs),
        waiting=[i for i in items if i["status"] == "waiting_human"
                 and i["gh_state"] == "open"],
        with_harry=[i for i in items if i["status"] == "held"
                    and i["gh_state"] == "open"],
        release=db.open_release(name),
        releases=db.project_releases(name),
        release_pending=db.get_setting(f"release_requested.{name}") == "1",
        release_auto=db.policy(name, "cut_release") == "auto",
        release_trigger=pipeline.release_trigger_phrase(name),
        queued_count=sum(1 for i in items if i["status"] == "queued"),
        can_release=pipeline.anything_to_release(p),
        lead_report=db.latest_report("lead", name),
        desk_notes=db.latest_report("notes", name),
        security_report=db.latest_report("security", name),
        security_pending=db.get_setting(f"security_requested.{name}") == "1",
        directions=db.recent_directions(name),
        questions=_enrich_questions(db.escalated_questions(name)),
        harry_inbox=_enrich_questions(db.harry_inbox(name)),
        policies=db.all_policies(name),
        runs=runs[:15],
        events=db.recent_events(30, name),
        cost=db.total_cost(name))


# How each policy reads on the settings page: key -> (label, one-line hint).
# The stored keys never change — "auto release" is only how cut_release is
# labelled for the operator.
POLICY_COPY = {
    "fix_issues": ("fix issues",
                   "Who starts a fix: auto — Ruth's verdict is enough; "
                   "lead — the team lead's plan is the sign-off, so the "
                   "section runs the fix and your gate moves to the "
                   "release; approve — you click before an engineer starts."),
    "file_issues": ("file issues",
                    "Whether team leads may open tracking issues on the "
                    "repo from their plan, capped at six of theirs sitting "
                    "open and unworked at once. Also gates the follow-up "
                    "issue opened when a release's CI goes red."),
    "merge_prs": ("merge community PRs",
                  "Community PRs are validated and tested locally first "
                  "either way; this decides who presses merge."),
    "merge_dependabot": ("merge Dependabot bumps",
                         "Dependency bumps are tested locally first either "
                         "way; this decides who presses merge."),
    "post_comments": ("post comments",
                      "Whether drafted comments and reviews go up on "
                      "GitHub on their own or wait for your click."),
    "cut_release": ("auto release",
                    "auto — Harness drafts the release, runs the tests, "
                    "merges to the main branch and tags it without asking. "
                    "approve — it prepares the release and waits for your "
                    "click. Nothing ships on a failing suite either way."),
    "release_schedule": ("release schedule",
                         "What sets a release off. changes — the two "
                         "settings below, whichever comes first. daily, "
                         "weekly or monthly — one release a window at most, "
                         "timed from the last release and carrying "
                         "everything queued since it, with the two settings "
                         "below ignored. A window with nothing in it is "
                         "skipped quietly; a window missed because the desk "
                         "was off gives one catch-up release, not a run of "
                         "them."),
    "release_min_changes": ("release after this many changes",
                            "How many queued changes it takes to start a "
                            "release."),
    "release_max_age_days": ("...or this many days",
                             "How old the oldest queued change may get "
                             "before a release starts anyway."),
    "active_hours": ("active hours",
                     "Local hours agent work may run in (\"HH-HH\", or "
                     "\"always\"). Anything you trigger yourself ignores it."),
    "daily_budget_usd": ("daily budget (USD)",
                         "The desk stops starting agent work once it has "
                         "spent this much in the last 24 hours."),
}

# The policies that decide an auto release, shown together.
RELEASE_POLICY_KEYS = ("cut_release", "release_schedule",
                       "release_min_changes", "release_max_age_days")

# The two that only apply on the "changes" schedule; the form greys them out
# and says so when a time schedule is picked.
COUNT_POLICY_KEYS = ("release_min_changes", "release_max_age_days")


@app.get("/p/{name}/settings")
def project_settings(request: Request, name: str):
    p = db.get_project(name)
    if not p:
        return RedirectResponse("/", status_code=303)
    return render(request, "settings.html", p=p,
                  policies=db.all_policies(name),
                  policy_copy=POLICY_COPY,
                  release_keys=RELEASE_POLICY_KEYS,
                  count_keys=COUNT_POLICY_KEYS,
                  policy_choices=commands.POLICY_CHOICES,
                  on_a_clock=pipeline.release_window_days(name) is not None,
                  staff=db.staff_get(name))


# How the item thread can be narrowed: query key -> (link label, the entry
# kinds it keeps; None keeps everything). Rulings and directions are pinned
# above the list whatever is picked — they bind every agent that reads the
# thread, which is how `agents._item_context` frames them.
THREAD_FILTERS = {
    "all": ("all", None),
    "ruling": ("rulings", ("ruling",)),
    "direction": ("directions", ("direction",)),
    "work": ("findings/plans", ("finding", "plan")),
    "note": ("notes", ("note",)),
    "log": ("events/tests", ("event", "test")),
}


@app.get("/p/{name}/{kind}/{number}")
def item_page(request: Request, name: str, kind: str, number: int):
    p = db.get_project(name)
    item = db.get_item(name, kind, number)
    if not (p and item):
        return RedirectResponse(f"/p/{name}", status_code=303)
    # `kind` is already the path parameter (issue|pr), so the thread filter
    # is read straight off the query string. No persistence: the URL is the
    # only thing that decides what an item page shows.
    sel = request.query_params.get("kind", "all")
    if sel not in THREAD_FILTERS:
        sel = "all"
    return render(request, "item.html", p=p, item=item,
                  thread=db.thread(name, f"{kind}#{number}"),
                  thread_filters=[(key, label)
                                  for key, (label, _) in THREAD_FILTERS.items()],
                  thread_kind=sel, thread_kinds=THREAD_FILTERS[sel][1],
                  gh_url=f"https://github.com/{p['repo']}/"
                         f"{'issues' if kind == 'issue' else 'pull'}/{number}")


# --- actions ----------------------------------------------------------------

# NOTE: these two routes MUST be registered before the generic item route
# below. Starlette matches in registration order, and /p/x/release/8/approve
# also matches /p/{name}/{kind}/{number}/approve — for months every press of
# "Merge & tag" was swallowed by the item route as kind='release' and did
# nothing to the release.
@app.post("/p/{name}/release/{rid}/approve")
def approve_release(name: str, rid: int):
    p = db.get_project(name)
    release = db.get_release(rid)
    if p and release and release["status"] == "proposed":
        # Claim it atomically, then finalize off the request thread —
        # merging/tagging takes ~30s and a second tap must not double-run.
        db.update_release(rid, status="merging")
        db.log_event(f"Operator approved release#{rid}; merging and tagging",
                     project=name)
        import threading
        threading.Thread(target=pipeline.finalize_release, args=(p, release),
                         daemon=True).start()
    return RedirectResponse(f"/p/{name}", status_code=303)


@app.post("/p/{name}/release/{rid}/abandon")
def abandon_release(name: str, rid: int):
    release = db.get_release(rid)
    if release:
        db.update_release(rid, status="abandoned")
        db.log_event(f"Release v{release['version']} abandoned", project=name)
    return RedirectResponse(f"/p/{name}", status_code=303)


@app.post("/p/{name}/{kind}/{number}/approve")
def approve(name: str, kind: str, number: int):
    if kind not in ("issue", "pr"):
        return RedirectResponse(f"/p/{name}", status_code=303)
    item = db.get_item(name, kind, number)
    unreviewed = bool(item and kind == "pr" and item["status"] == "new")
    fields = {}
    if pipeline.fresh_session_on_approve(item):
        # The last run produced nothing to build on, so approving it is a
        # fresh attempt rather than a resume — otherwise the session that
        # already believes the work is done picks up where it left off and
        # the item comes straight back.
        fields["session_id"] = ""
    db.update_item(name, kind, number, status="approved", error="",
                   breaker_reset_at=db.now(), breaker_trips=0, **fields)
    db.log_event(
        f"{config.OPERATOR} sent {kind}#{number} straight to merge, without "
        "a review — the harness tests it first" if unreviewed
        else f"Operator approved {kind}#{number}", project=name)
    worker.trigger(name)
    return RedirectResponse(f"/p/{name}", status_code=303)


@app.post("/p/{name}/{kind}/{number}/reject")
def reject(name: str, kind: str, number: int):
    db.update_item(name, kind, number, status="rejected")
    db.log_event(f"Human rejected {kind}#{number}", project=name)
    return RedirectResponse(f"/p/{name}", status_code=303)


@app.post("/p/{name}/{kind}/{number}/close")
def close_item(name: str, kind: str, number: int, reason: str = Form("")):
    """Close an item that is already done — as opposed to rejecting it,
    which says we are not doing the work. An issue is closed on GitHub too,
    so a finished item stops coming back round the loop."""
    p = db.get_project(name)
    if p and kind in ("issue", "pr") and \
            pipeline.close_item(p, kind, number, reason):
        db.log_event(f"{config.OPERATOR} closed {kind}#{number} as done"
                     + (f": {reason.strip()[:80]}" if reason.strip() else ""),
                     project=name)
    return RedirectResponse(f"/p/{name}/{kind}/{number}", status_code=303)


@app.post("/p/{name}/{kind}/{number}/retry")
def retry(name: str, kind: str, number: int):
    # Starting over is the operator's say-so, so the item's failure history
    # goes with it: without the reset the old failures trip the breaker
    # again before the fresh attempt has run.
    db.update_item(name, kind, number, status="new", error="", session_id="",
                   breaker_reset_at=db.now(), breaker_trips=0)
    worker.trigger(name)
    return RedirectResponse(f"/p/{name}/{kind}/{number}", status_code=303)


@app.post("/p/{name}/{kind}/{number}/post-comment")
def post_comment(name: str, kind: str, number: int):
    p = db.get_project(name)
    item = db.get_item(name, kind, number)
    if p and item and item["draft_comment"]:
        if kind == "issue":
            gh.comment_issue(p["repo"], number, item["draft_comment"])
        else:
            gh.comment_pr(p["repo"], number, item["draft_comment"])
        db.log_event(f"Human posted drafted comment on {kind}#{number}",
                     project=name)
    return RedirectResponse(f"/p/{name}/{kind}/{number}", status_code=303)


@app.post("/p/{name}/release/request")
def request_release(name: str):
    """Ask Colin for a release now, without waiting for the batch thresholds.

    Sets the same flag Harry sets when told to, so there is one release path:
    the next cycle drafts, tests and opens the PR, and cut_release decides
    whether it then waits for a click. With a release already open, or with
    nothing to cut, it does nothing — the button is hidden in both cases, so
    this only catches a stale page."""
    p = db.get_project(name)
    if p and not db.open_release(name) and pipeline.anything_to_release(p):
        db.set_setting(f"release_requested.{name}", "1")
        db.log_event(f"{config.OPERATOR} asked for a release", project=name)
        worker.trigger(name)
    return RedirectResponse(f"/p/{name}", status_code=303)




@app.post("/p/{name}/policy/{key}")
def set_policy(name: str, key: str, value: str = Form(...)):
    if key in config.POLICY_DEFAULTS:
        db.set_policy(name, key, value)
        db.log_event(f"Policy {key} -> {value}", project=name)
    return RedirectResponse(f"/p/{name}/settings", status_code=303)


@app.post("/p/{name}/question/{qid}/answer")
def answer_question(name: str, qid: int, answer: str = Form(...),
                    via: str = ""):
    q = db.question(qid)
    db.answer_question(qid, answer.strip())
    db.log_event(f"{config.OPERATOR} answered a question: {answer.strip()[:100]}",
                 project=name)
    if q and pipeline.is_breaker_question(q):
        # Answering a held item's question over Harry's head still has to
        # move the item — the option buttons are the same vocabulary he uses.
        pipeline.apply_breaker_ruling(q, answer.strip(), answer.strip(),
                                      by=config.OPERATOR)
    else:
        # Every other answer about an item moves it now, on the click,
        # rather than landing on the thread and waiting to be noticed.
        p = db.get_project(name)
        if p:
            pipeline.route_answers(p)
    worker.trigger(name)
    if via == "ntfy":  # ntfy http actions want a plain 2xx, not a redirect
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": True, "answered": qid})
    return RedirectResponse(f"/p/{name}" if name != "-" else "/",
                            status_code=303)


@app.post("/p/{name}/question/{qid}/dismiss")
def dismiss_question(name: str, qid: int):
    db.dismiss_question(qid)
    return RedirectResponse(f"/p/{name}" if name != "-" else "/",
                            status_code=303)


@app.get("/p/{name}/directions.json")
def directions_json(name: str):
    from fastapi.responses import JSONResponse
    rows = db.recent_directions(name)
    return JSONResponse({"directions": [
        {"id": r["id"], "text": r["question"], "reply": r["answer"],
         "pending": r["status"] == "directive", "ts": r["created_at"]}
        for r in rows]})


def _run_command(text: str, project: str):
    """Carry out a slash command typed into the composer, or return None.

    None means the text was prose and belongs to Harry. Everything else ends
    here: a command is dispatched to the ordinary route function — the same
    code the buttons run, with the same policy gates — and anything that
    cannot be acted on comes back as plain text under the box.
    """
    try:
        cmd = commands.parse(text, project)
    except commands.CommandError as err:
        return PlainTextResponse(str(err), status_code=400)
    if cmd is None:
        return None
    if cmd.name == "help":
        return PlainTextResponse(commands.CHEATSHEET)
    if cmd.name == "p":
        return RedirectResponse(f"/p/{cmd.project}", status_code=303)
    if cmd.name == "release":
        p = db.get_project(cmd.project)
        if db.open_release(cmd.project):
            return PlainTextResponse(
                f"{cmd.project} already has a release open.", status_code=400)
        if not pipeline.anything_to_release(p):
            return PlainTextResponse(
                f"Nothing to release on {cmd.project} yet.", status_code=400)
    # Log the command itself, so the stream shows what was typed as well as
    # what the route then did.
    db.log_event(f"{config.OPERATOR}: {text.strip()}", project=cmd.project)
    if cmd.name in ("approve", "reject"):
        route = approve if cmd.name == "approve" else reject
        return route(cmd.project, cmd.kind, cmd.number)
    if cmd.name == "release":
        return request_release(cmd.project)
    if cmd.name == "tell":
        return steer_run(cmd.run_id, cmd.text)
    if cmd.name == "stop":
        return stop_run(cmd.run_id)
    if cmd.name == "policy":
        return set_policy(cmd.project, cmd.key, cmd.value)
    return run_now()  # cycle — the only verb left


@app.post("/tell")
def tell_from_overview(project: str = Form(...), text: str = Form(...)):
    done = _run_command(text, project)
    if done is not None:
        return done
    if db.get_project(project):
        db.add_direction(project, text)
        worker.trigger(project)
    return RedirectResponse("/", status_code=303)


@app.post("/p/{name}/tell")
def tell_team(name: str, text: str = Form(...), item_key: str = Form("")):
    done = _run_command(text, name)
    if done is not None:
        return done
    if db.get_project(name):
        db.add_direction(name, text, item_key)
        worker.trigger(name)
    target = f"/p/{name}/{item_key.replace('#', '/')}" if item_key else f"/p/{name}"
    return RedirectResponse(target, status_code=303)


@app.post("/p/{name}/security-review")
def request_security_review(name: str):
    if db.get_project(name):
        db.set_setting(f"security_requested.{name}", "1")
        db.log_event("Security review requested", project=name)
        worker.trigger(name)
    return RedirectResponse(f"/p/{name}", status_code=303)


@app.get("/run/{run_id}")
def run_page(request: Request, run_id: int):
    run = db.get_run(run_id)
    if not run:
        return RedirectResponse("/", status_code=303)
    transcript = ""
    if run["log_path"]:
        try:
            from pathlib import Path as _P
            text = _P(run["log_path"]).read_text(errors="replace")
            transcript = text[-60_000:]
        except OSError:
            transcript = "(transcript file not available)"
    lead = ""
    if run["project"]:
        proj = db.get_project(run["project"])
        lead = proj["lead_name"] if proj else ""
    # directions filed on the item since this run began — the ones the
    # operator queued instead of steering, waiting on whoever comes next
    followups = (db.item_directions(run["project"], run["item_key"],
                                    since=run["started_at"])
                 if run["project"] and run["item_key"] else [])
    # Three states worth telling apart on a finished run: the session took it,
    # the operator sent it on as a direction, or it is still theirs to settle.
    # A discarded steer is gone from the page entirely.
    steers = [s for s in db.run_steers(run_id) if s["resolution"] != "discarded"]
    settled = [s for s in steers
               if s["resolution"] == "kept" or
               (s["delivered_at"] and not s["resolution"])]
    return render(request, "run.html", run=run, transcript=transcript,
                  steers=steers, followups=followups,
                  undelivered=db.undelivered_steers(run_id), settled=settled,
                  display_name=run["agent"] or agent_name(run["role"],
                                                          run["task"], lead))


@app.get("/run/{run_id}/tail")
def run_tail(run_id: int, offset: int = 0):
    """Incremental transcript bytes, plus the facts strip above the console.

    The facts ride along with every chunk — including the first few seconds
    before a log file exists — so the strip moves while the run does."""
    from fastapi.responses import JSONResponse
    run = db.get_run(run_id)
    if not run:
        return JSONResponse({"data": "", "offset": 0, "live": False,
                             "turns": 0, "cost_usd": 0.0, "model": "",
                             "started_at": None, "finished_at": None})
    facts = {"turns": run["turns"], "cost_usd": run["cost_usd"],
             "model": run["model"], "started_at": run["started_at"],
             "finished_at": run["finished_at"]}
    live = run["finished_at"] is None
    if not run["log_path"]:
        # Nothing to tail yet, but the run is not over: saying live=False here
        # is what stops the poller for good.
        return JSONResponse({"data": "", "offset": 0, "live": live, **facts})
    from pathlib import Path as _P
    try:
        f = _P(run["log_path"])
        size = f.stat().st_size
        offset = max(0, min(offset, size))
        with open(f, "rb") as fh:
            fh.seek(offset)
            chunk = fh.read(65536)
        return JSONResponse({
            "data": chunk.decode("utf-8", errors="replace"),
            "offset": offset + len(chunk),
            "live": live,
            **facts,
        })
    except OSError:
        return JSONResponse({"data": "", "offset": offset, "live": live,
                             **facts})


@app.post("/run/{run_id}/steer")
def steer_run(run_id: int, text: str = Form(...)):
    run = db.get_run(run_id)
    if run and run["finished_at"] is None and text.strip():
        db.add_steer(run_id, text)
        if run["project"] and run["item_key"]:
            db.thread_append(run["project"], run["item_key"], config.OPERATOR,
                             "direction", f"(to {run['agent'] or run['task']}, "
                             f"mid-run) {text.strip()}")
        db.log_event(f"{config.OPERATOR} steered run {run_id} "
                     f"({run['task']} {run['item_key']}): {text.strip()[:100]}",
                     project=run["project"])
    return RedirectResponse(f"/run/{run_id}", status_code=303)


def _pending_steer(run_id: int, steer_id: int):
    """A steer of this run the session never took and nobody has settled."""
    steer = db.get_steer(steer_id)
    if (steer and steer["run_id"] == run_id and steer["delivered_at"] is None
            and not steer["resolution"]):
        return steer
    return None


@app.post("/run/{run_id}/steer/{steer_id}/keep")
def keep_steer(run_id: int, steer_id: int):
    """Send an undelivered steer on as an operator direction instead.

    The session never saw it, so this is an action rather than a note: the
    direction is pending until Harry actions it on the next cycle, and it
    can move the item on. The text is already in the item thread — the steer
    box mirrored it there when it was sent — so don't write it twice."""
    run = db.get_run(run_id)
    steer = _pending_steer(run_id, steer_id)
    if run and steer and run["project"]:
        db.add_direction(run["project"], steer["text"], run["item_key"],
                         note_thread=False)
        db.resolve_steer(steer_id, "kept")
        worker.trigger(run["project"])
    return RedirectResponse(f"/run/{run_id}", status_code=303)


@app.post("/run/{run_id}/steer/{steer_id}/discard")
def discard_steer(run_id: int, steer_id: int):
    run = db.get_run(run_id)
    steer = _pending_steer(run_id, steer_id)
    if run and steer:
        db.resolve_steer(steer_id, "discarded")
        db.log_event(f"{config.OPERATOR} discarded an undelivered steer on "
                     f"run {run_id}: {steer['text'][:100]}",
                     project=run["project"])
    return RedirectResponse(f"/run/{run_id}", status_code=303)


@app.post("/run/{run_id}/followup")
def followup_run(run_id: int, text: str = Form(...)):
    """The other half of the steer box: say it, but don't interrupt.

    The message becomes an operator direction on the run's item, so it lands
    in the thread and the next agent to pick the item up reads it as binding
    context. Nothing reaches the live session. Runs with no item have nowhere
    to file it, so the button is hidden and this does nothing. Deliberately
    not gated on the run still being live — a note filed as the run ends is
    still worth keeping."""
    run = db.get_run(run_id)
    if run and run["project"] and run["item_key"] and text.strip():
        db.add_direction(run["project"], text, run["item_key"])
        worker.trigger(run["project"])
    return RedirectResponse(f"/run/{run_id}", status_code=303)


@app.post("/run/{run_id}/stop")
def stop_run(run_id: int):
    run = db.get_run(run_id)
    if run and run["finished_at"] is None:
        db.request_cancel(run_id)
        db.log_event(f"{config.OPERATOR} pressed Stop on run {run_id} "
                     f"({run['task']} {run['item_key']})", "warn",
                     project=run["project"])
    return RedirectResponse(f"/run/{run_id}", status_code=303)


@app.post("/run-now")
def run_now():
    worker.trigger()
    return RedirectResponse("/", status_code=303)


@app.post("/theme")
def set_theme(request: Request, value: str = Form(...)):
    """Remember the chosen palette in a cookie; anything else means light."""
    back = request.headers.get("referer") or "/"
    if urlparse(back).netloc not in ("", request.url.netloc):
        back = "/"  # never bounce off this host on a referer we don't own
    resp = RedirectResponse(back, status_code=303)
    resp.set_cookie("theme", "dark" if value == "dark" else "light",
                    max_age=31536000, path="/", samesite="lax")
    return resp


@app.post("/resume")
def resume_now():
    db.set_setting("paused_until", "")
    db.set_setting("paused_reason", "")
    db.set_setting("backoff_count", "0")
    db.log_event("Human cleared the API-limit pause")
    worker.trigger()
    return RedirectResponse("/", status_code=303)


# --- add project ------------------------------------------------------------

@app.get("/add")
def add_form(request: Request):
    return render(request, "add.html", defaults=config.PROJECT_DEFAULTS)


@app.post("/add")
def add_project(
    request: Request,
    name: str = Form(...), gh_repo: str = Form(...),
    dev_branch: str = Form(""), main_branch: str = Form(""),
    version_file: str = Form(""), version_pattern: str = Form(""),
    test_command: str = Form(""), setup_command: str = Form(""),
):
    slug = "".join(ch for ch in name.lower().replace(" ", "-")
                   if ch.isalnum() or ch == "-")
    if slug and not db.get_project(slug):
        db.create_project(slug, gh_repo.strip(),
                          dev_branch=dev_branch.strip(),
                          main_branch=main_branch.strip(),
                          version_file=version_file.strip(),
                          version_pattern=version_pattern.strip(),
                          test_command=test_command.strip(),
                          setup_command=setup_command.strip())
        db.log_event(f"Project {slug} added ({gh_repo})", project=slug)
        worker.trigger(slug)
    return RedirectResponse(f"/p/{slug}" if slug else "/", status_code=303)


@app.post("/p/{name}/toggle")
def toggle_project(name: str):
    p = db.get_project(name)
    if p:
        db.update_project(name, enabled=0 if p["enabled"] else 1)
    return RedirectResponse(f"/p/{name}", status_code=303)
