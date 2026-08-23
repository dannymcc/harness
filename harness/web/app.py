"""Harness GUI.

Server-rendered, mobile-first, no build step. Meant to sit behind
`tailscale serve` (or similar) — no auth of its own, so never expose it with
Funnel. If Tailscale identity headers are present they are shown in the nav.
"""
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import config, db, gh, pipeline, repo, worker

BASE = Path(__file__).parent
app = FastAPI(title="Harness")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


def agent_name(role: str, task: str, lead_name: str = "") -> str:
    if role == "cto":
        return config.CTO_NAME
    if role == "admin":
        return config.ADMIN_NAME
    if role == "lead":
        return lead_name or "lead"
    return config.IC_NAMES.get(task, "IC")


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


def render(request: Request, template: str, **ctx):
    ctx.update(
        request=request,
        operator=config.OPERATOR,
        version=config.DISPLAY_VERSION,
        projects=db.all_projects(),
        paused=db.paused_until(),
        maintenance=db.maintenance(),
        paused_reason=db.get_setting("paused_reason"),
        worker=worker.status(),
        who=request.headers.get("Tailscale-User-Login", ""),
    )
    return templates.TemplateResponse(request, template, ctx)


# --- overview ---------------------------------------------------------------

@app.get("/")
def overview(request: Request):
    cards = []
    for p in db.all_projects():
        counts = db.counts_by_status(p["name"])
        waiting = counts.get("waiting_human", 0)
        rel = db.open_release(p["name"])
        if rel:
            waiting += 1
        cards.append({
            "project": p,
            "counts": counts,
            "waiting": waiting,
            "queued": counts.get("queued", 0),
            "release": rel,
            "cost": db.total_cost(p["name"]),
            "lead_report": db.latest_report("lead", p["name"]),
        })
    return render(request, "overview.html",
                  cards=cards,
                  questions=_enrich_questions(db.open_questions()),
                  staff=_staff_board(),
                  cto_report=db.latest_report("cto"),
                  events=db.recent_events(20),
                  total_cost=db.total_cost())


# --- project pages ----------------------------------------------------------

KANBAN_COLUMNS = [
    ("Inbox", "Ruth", ("new",)),
    ("Assessed", "Ruth", ("triaged",)),
    ("In progress", "Malcolm", ("approved", "working")),
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
    if (last["summary"] or "").startswith("orphaned"):
        # A restart interrupted this run; the item was requeued
        # automatically. Not a real failure — don't dress it as one.
        return {"name": display, "state": "restarted",
                "detail": f"{what} — interrupted by a restart, requeued",
                "run_id": last["id"]}
    return {"name": display, "state": "failed",
            "detail": f"{what} — {(last['summary'] or 'no reason recorded')[:90]}",
            "run_id": last["id"]}


def _enrich_questions(qs):
    """Attach the referenced item's title/link so the operator can see what a
    question is actually about."""
    out = []
    for q in qs:
        d = dict(q)
        d["options_list"] = db.question_options(q)
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
        harry_report=db.latest_report("cto"),
        roster=_agent_roster(p, runs),
        waiting=[i for i in items if i["status"] == "waiting_human"
                 and i["gh_state"] == "open"],
        release=db.open_release(name),
        releases=db.project_releases(name),
        lead_report=db.latest_report("lead", name),
        desk_notes=db.latest_report("notes", name),
        security_report=db.latest_report("security", name),
        security_pending=db.get_setting(f"security_requested.{name}") == "1",
        directions=db.recent_directions(name),
        questions=_enrich_questions(db.open_questions(name)),
        policies=db.all_policies(name),
        runs=runs[:15],
        events=db.recent_events(30, name),
        cost=db.total_cost(name))


@app.get("/p/{name}/settings")
def project_settings(request: Request, name: str):
    p = db.get_project(name)
    if not p:
        return RedirectResponse("/", status_code=303)
    return render(request, "settings.html", p=p,
                  policies=db.all_policies(name),
                  staff=db.staff_get(name))


@app.get("/p/{name}/{kind}/{number}")
def item_page(request: Request, name: str, kind: str, number: int):
    p = db.get_project(name)
    item = db.get_item(name, kind, number)
    if not (p and item):
        return RedirectResponse(f"/p/{name}", status_code=303)
    return render(request, "item.html", p=p, item=item,
                  gh_url=f"https://github.com/{p['repo']}/"
                         f"{'issues' if kind == 'issue' else 'pull'}/{number}")


# --- actions ----------------------------------------------------------------

@app.post("/p/{name}/{kind}/{number}/approve")
def approve(name: str, kind: str, number: int):
    db.update_item(name, kind, number, status="approved", error="")
    db.log_event(f"Operator approved {kind}#{number}", project=name)
    worker.trigger()
    return RedirectResponse(f"/p/{name}", status_code=303)


@app.post("/p/{name}/{kind}/{number}/reject")
def reject(name: str, kind: str, number: int):
    db.update_item(name, kind, number, status="rejected")
    db.log_event(f"Human rejected {kind}#{number}", project=name)
    return RedirectResponse(f"/p/{name}", status_code=303)


@app.post("/p/{name}/{kind}/{number}/retry")
def retry(name: str, kind: str, number: int):
    db.update_item(name, kind, number, status="new", error="", session_id="")
    worker.trigger()
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


@app.post("/p/{name}/policy/{key}")
def set_policy(name: str, key: str, value: str = Form(...)):
    if key in config.POLICY_DEFAULTS:
        db.set_policy(name, key, value)
        db.log_event(f"Policy {key} -> {value}", project=name)
    return RedirectResponse(f"/p/{name}/settings", status_code=303)


@app.post("/p/{name}/question/{qid}/answer")
def answer_question(name: str, qid: int, answer: str = Form(...),
                    via: str = ""):
    db.answer_question(qid, answer.strip())
    db.log_event(f"{config.OPERATOR} answered a question: {answer.strip()[:100]}",
                 project=name)
    worker.trigger()
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


@app.post("/p/{name}/tell")
def tell_team(name: str, text: str = Form(...), item_key: str = Form("")):
    if db.get_project(name):
        db.add_direction(name, text, item_key)
        worker.trigger()
    target = f"/p/{name}/{item_key.replace('#', '/')}" if item_key else f"/p/{name}"
    return RedirectResponse(target, status_code=303)


@app.post("/p/{name}/security-review")
def request_security_review(name: str):
    if db.get_project(name):
        db.set_setting(f"security_requested.{name}", "1")
        db.log_event("Security review requested", project=name)
        worker.trigger()
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
    return render(request, "run.html", run=run, transcript=transcript,
                  display_name=run["agent"] or agent_name(run["role"],
                                                          run["task"], lead))


@app.get("/run/{run_id}/tail")
def run_tail(run_id: int, offset: int = 0):
    """Incremental transcript bytes for the live console view."""
    from fastapi.responses import JSONResponse
    run = db.get_run(run_id)
    if not run or not run["log_path"]:
        return JSONResponse({"data": "", "offset": 0, "live": False})
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
            "live": run["finished_at"] is None,
        })
    except OSError:
        return JSONResponse({"data": "", "offset": offset, "live": False})


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
        worker.trigger()
    return RedirectResponse(f"/p/{slug}" if slug else "/", status_code=303)


@app.post("/p/{name}/toggle")
def toggle_project(name: str):
    p = db.get_project(name)
    if p:
        db.update_project(name, enabled=0 if p["enabled"] else 1)
    return RedirectResponse(f"/p/{name}", status_code=303)
