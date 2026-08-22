"""Wilman GUI.

Server-rendered, mobile-first, no build step. Meant to sit behind
`tailscale serve` on hyperion — no auth of its own, so never expose it with
Funnel. If Tailscale identity headers are present they are shown in the nav.
"""
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import config, db, gh, pipeline, repo, worker

BASE = Path(__file__).parent
app = FastAPI(title="Wilman")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


def agent_name(role: str, task: str, lead_name: str = "") -> str:
    if role == "cto":
        return config.CTO_NAME
    if role == "lead":
        return lead_name or "lead"
    return config.IC_NAMES.get(task, "IC")


templates.env.globals["agent_name"] = agent_name


def render(request: Request, template: str, **ctx):
    ctx.update(
        request=request,
        projects=db.all_projects(),
        paused=db.paused_until(),
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
                  cto_report=db.latest_report("cto"),
                  events=db.recent_events(20),
                  total_cost=db.total_cost())


# --- project pages ----------------------------------------------------------

@app.get("/p/{name}")
def project_page(request: Request, name: str):
    p = db.get_project(name)
    if not p:
        return RedirectResponse("/", status_code=303)
    items = db.project_items(name)
    return render(
        request, "project.html", p=p, items=items,
        waiting=[i for i in items if i["status"] == "waiting_human"
                 and i["gh_state"] == "open"],
        blocked=[i for i in items if i["status"] == "blocked"],
        queued=[i for i in items if i["status"] == "queued"],
        open_items=[i for i in items if i["gh_state"] == "open"
                    and i["status"] not in ("waiting_human", "blocked", "queued")],
        release=db.open_release(name),
        releases=db.project_releases(name),
        lead_report=db.latest_report("lead", name),
        policies=db.all_policies(name),
        runs=db.recent_runs(15, name),
        events=db.recent_events(30, name),
        cost=db.total_cost(name))


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
    db.log_event(f"Human approved {kind}#{number}", project=name)
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
        pipeline.finalize_release(p, release)
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
    return RedirectResponse(f"/p/{name}", status_code=303)


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
