"""Open a project's pages in a real browser and report what CSS text cannot.

Harness's engineers write templates and stylesheets and, until this existed,
had no way of looking at the result: a layout was "verified" by reading the
diff, and a stylesheet that contains the right strings can still be unusable
on a phone. This script is the missing pair of eyes. It starts the project
under its preview command, opens each route in headless Chromium at each
viewport, saves a PNG, and reports the three things a diff reads clean on:

  * a page wider than the viewport (the horizontal-scroll fault),
  * elements whose right edge is past the viewport, outside any declared
    scroll box (what makes the page wider),
  * console and page errors.

Deliberately standalone — it imports nothing from harness — so it runs under
whichever interpreter has Playwright installed and can be run by hand from a
checkout:

    python harness/render.py --command 'flask run -p 8000' \\
        --base-url http://127.0.0.1:8000 --routes / /projects \\
        --viewport 412x915 --viewport 1280x800 --out .harness/screenshots

Exit status: 0 rendered and clean, 2 rendered with findings (the PNGs and the
report are still there — this is a verdict, not a crash), 1 nothing rendered
(the app never came up, or Playwright is not installed).
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_VIEWPORTS = ("412x915", "1280x800")
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
# How long to wait for the preview command to start answering, and how long
# one page may take. Both generous: a first run installs nothing but may
# migrate a database, and an agent waiting is cheaper than a false negative.
START_TIMEOUT_S = 90
PAGE_TIMEOUT_MS = 30_000
# A page is "wider than its viewport" only past this many pixels. Sub-pixel
# layout rounding routinely puts an element a fraction over the edge, and a
# report that cries wolf on every page is a report nobody reads.
SLACK_PX = 1

# What the browser is asked, once per page, after it settles. Returns the
# document's width against the viewport's and the elements sticking out of
# it. Children of an offender are dropped (one fault, not fifty) and so is
# anything inside an element that declares itself scrollable horizontally —
# a wide table in an `overflow-x: auto` box is a design, not a bug.
PROBE_JS = """
() => {
  const slack = %(slack)d;
  const vw = document.documentElement.clientWidth;
  const inScrollBox = (el) => {
    for (let p = el.parentElement; p; p = p.parentElement) {
      const ox = getComputedStyle(p).overflowX;
      if (ox === 'auto' || ox === 'scroll' || ox === 'hidden') return true;
    }
    return false;
  };
  const offending = new Set();
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    if (r.right <= vw + slack) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none') continue;
    if (inScrollBox(el)) continue;
    offending.add(el);
  }
  const describe = (el) => {
    const r = el.getBoundingClientRect();
    let sel = el.tagName.toLowerCase();
    if (el.id) sel += '#' + el.id;
    if (el.className && typeof el.className === 'string') {
      sel += '.' + el.className.trim().split(/\\s+/).slice(0, 3).join('.');
    }
    return {selector: sel, right: Math.round(r.right),
            width: Math.round(r.width),
            text: (el.textContent || '').trim().slice(0, 60)};
  };
  const outermost = [...offending].filter(
    el => !offending.has(el.parentElement)
       && ![...offending].some(o => o !== el && o.contains(el)));
  return {
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: vw,
    overflowing: outermost.slice(0, 10).map(describe),
    overflowingCount: outermost.length,
  };
}
""" % {"slack": SLACK_PX}


# --- pure helpers ------------------------------------------------------------

def parse_viewports(specs) -> list[tuple[int, int]]:
    """['412x915'] -> [(412, 915)]. Anything else is the caller's mistake."""
    out = []
    for spec in specs or DEFAULT_VIEWPORTS:
        m = re.fullmatch(r"\s*(\d{2,5})\s*[xX*]\s*(\d{2,5})\s*", str(spec))
        if not m:
            raise ValueError(f"viewport must look like 412x915, not {spec!r}")
        out.append((int(m.group(1)), int(m.group(2))))
    return out


def shot_name(route: str, width: int, height: int) -> str:
    """A filename that says which page at which size, safe on any disk."""
    slug = re.sub(r"[^a-z0-9]+", "-", route.lower()).strip("-") or "root"
    return f"{slug[:60]}-{width}x{height}.png"


def page_findings(page: dict) -> list[str]:
    """What is wrong with one rendered page, in the words a human wants."""
    found = []
    if page.get("error"):
        return [f"did not render: {page['error']}"]
    if page.get("status") and page["status"] >= 400:
        found.append(f"HTTP {page['status']}")
    if page.get("scrollWidth", 0) > page.get("clientWidth", 0) + SLACK_PX:
        found.append(f"page scrolls sideways: scrollWidth "
                     f"{page['scrollWidth']} vs viewport {page['clientWidth']}")
    for el in page.get("overflowing", []):
        found.append(f"past the right edge: {el['selector']} "
                     f"(right {el['right']}px)")
    extra = page.get("overflowingCount", 0) - len(page.get("overflowing", []))
    if extra > 0:
        found.append(f"...and {extra} more elements past the right edge")
    for msg in page.get("consoleErrors", []):
        found.append(f"console error: {msg}")
    return found


def summarise(report: dict) -> str:
    """The report as the engineer reads it in the terminal."""
    lines = []
    for page in report["pages"]:
        head = f"{page['route']} @ {page['viewport']}"
        findings = page_findings(page)
        if findings:
            lines.append(f"FAIL {head}")
            lines.extend(f"       - {f}" for f in findings)
        else:
            lines.append(f"ok   {head}")
        if page.get("screenshot"):
            lines.append(f"       {page['screenshot']}")
    clean = not any(page_findings(p) for p in report["pages"])
    lines.append("")
    lines.append("Nothing to report — every route rendered clean." if clean
                 else "Findings above. The screenshots are the evidence: "
                      "open them before deciding they are wrong.")
    return "\n".join(lines)


def exit_code(report: dict) -> int:
    """0 clean, 2 rendered with findings, 1 nothing rendered."""
    if not report["pages"]:
        return 1
    return 2 if any(page_findings(p) for p in report["pages"]) else 0


# --- the app under test ------------------------------------------------------

def child_env(path_prefix: str = "") -> dict:
    """The environment the preview command runs in.

    Inherited, minus harness's GitHub credentials: this starts the project's
    own code, and the project's own code has no business with them. Not a
    sandbox — the fix role's shell isn't one either (see SECURITY.md).
    """
    env = dict(os.environ)
    for cred in ("GH_TOKEN", "GITHUB_TOKEN"):
        env[cred] = ""
    if path_prefix:
        env["PATH"] = f"{path_prefix}:{env.get('PATH', '')}"
    return env


def wait_for_app(url: str, proc, timeout: int = START_TIMEOUT_S) -> str:
    """Poll until the app answers. Returns '' when it does, else why not."""
    deadline = time.monotonic() + timeout
    last = "no response"
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return f"the preview command exited with status {proc.returncode}"
        try:
            with urllib.request.urlopen(url, timeout=5):
                return ""
        except urllib.error.HTTPError:
            return ""     # a 404 on / is still a live server
        except Exception as e:                      # noqa: BLE001 — any of them
            last = f"{type(e).__name__}: {e}"
        time.sleep(0.5)
    return f"{url} did not answer within {timeout}s ({last})"


def stop_app(proc) -> None:
    """Take the whole process group down; a dev server forks children."""
    if proc is None or proc.poll() is not None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (OSError, ProcessLookupError):
            return
        try:
            proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            continue


# --- rendering ---------------------------------------------------------------

def render(base_url: str, routes: list[str], viewports: list[tuple[int, int]],
           out: Path) -> list[dict]:
    """Open every route at every viewport. Playwright is imported here so the
    module stays importable (and testable) without a browser installed."""
    from playwright.sync_api import sync_playwright

    pages = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        for width, height in viewports:
            context = browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=1)
            for route in routes:
                pages.append(_one_page(context, base_url, route,
                                       width, height, out))
            context.close()
        browser.close()
    return pages


def _one_page(context, base_url: str, route: str, width: int, height: int,
              out: Path) -> dict:
    page_result = {"route": route, "viewport": f"{width}x{height}",
                   "consoleErrors": [], "screenshot": "", "error": ""}
    page = context.new_page()
    page.on("console", lambda m: (
        m.type == "error"
        and len(page_result["consoleErrors"]) < 10
        and page_result["consoleErrors"].append(m.text[:300])))
    page.on("pageerror", lambda e: (
        len(page_result["consoleErrors"]) < 10
        and page_result["consoleErrors"].append(str(e)[:300])))
    try:
        url = base_url.rstrip("/") + "/" + route.lstrip("/")
        resp = page.goto(url, wait_until="load", timeout=PAGE_TIMEOUT_MS)
        page_result["status"] = resp.status if resp else 0
        page.wait_for_timeout(400)     # let webfonts and JS settle
        shot = out / shot_name(route, width, height)
        page.screenshot(path=str(shot), full_page=True)
        page_result["screenshot"] = str(shot)
        page_result.update(page.evaluate(PROBE_JS))
    except Exception as e:             # noqa: BLE001 — a bad page is a finding
        page_result["error"] = f"{type(e).__name__}: {e}"[:300]
    finally:
        page.close()
    return page_result


# --- entry point -------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="render.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--command", default="",
                   help="shell command that starts the app (omit if it is "
                        "already running at --base-url)")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL,
                   help=f"where the app answers (default {DEFAULT_BASE_URL})")
    p.add_argument("--routes", nargs="+", default=["/"],
                   help="paths to open, e.g. / /projects /projects/1")
    p.add_argument("--viewport", dest="viewports", action="append",
                   help="WxH, repeatable (default %s)"
                        % " and ".join(DEFAULT_VIEWPORTS))
    p.add_argument("--out", default=".harness/screenshots",
                   help="directory for the PNGs and report.json")
    p.add_argument("--cwd", default=".", help="where to run --command")
    p.add_argument("--path-prefix", default="",
                   help="directory to put first on PATH (a project venv)")
    p.add_argument("--start-timeout", type=int, default=START_TIMEOUT_S)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    viewports = parse_viewports(args.viewports)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report = {"base_url": args.base_url, "routes": args.routes,
              "viewports": [f"{w}x{h}" for w, h in viewports],
              "out": str(out), "pages": [], "error": ""}

    proc = None
    app_log = out / "app.log"
    if args.command:
        with app_log.open("wb") as log:
            proc = subprocess.Popen(
                ["bash", "-c", args.command], cwd=args.cwd,
                env=child_env(args.path_prefix), stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        why = wait_for_app(args.base_url, proc, args.start_timeout)
        if why:
            report["error"] = why
        else:
            try:
                report["pages"] = render(args.base_url, args.routes,
                                         viewports, out)
            except ImportError:
                report["error"] = (
                    "Playwright is not installed for this interpreter. "
                    "In the harness image it is; from a checkout, "
                    "`pip install playwright && playwright install chromium`.")
            except Exception as e:     # noqa: BLE001 — report, never traceback
                report["error"] = f"{type(e).__name__}: {e}"[:500]
    finally:
        stop_app(proc)

    (out / "report.json").write_text(json.dumps(report, indent=2))
    if report["error"]:
        print(f"render failed: {report['error']}")
        if args.command and app_log.exists():
            tail = app_log.read_text(errors="replace")[-2000:]
            print(f"--- last of {app_log} ---\n{tail}")
        return 1
    print(summarise(report))
    print(f"\nFull report: {out / 'report.json'}")
    return exit_code(report)


if __name__ == "__main__":       # pragma: no cover
    sys.exit(main())
