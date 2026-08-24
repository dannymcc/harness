"""Slash commands for the composer.

The box the operator types into is the single entry point, so it does two
jobs: plain prose is a direction for Harry to judge, and a leading `/` is a
command this code carries out itself. Every command lands on a route the GUI
already has, behind the policy gate that route already has — nothing here is
a new outward action, only a faster way to reach one from a phone.

This module only reads and parses. `parse()` returns a `Command` (or None for
prose) and raises `CommandError` with something the operator can read when it
cannot; calling the routes is `app.py`'s job.
"""
from dataclasses import dataclass

from .. import config, db


class CommandError(Exception):
    """A message for the operator, not a stack trace."""


@dataclass
class Command:
    """A parsed, resolved command. Only the fields its verb uses are set."""
    name: str
    project: str = ""
    kind: str = ""
    number: int = 0
    run_id: int = 0
    key: str = ""
    value: str = ""
    text: str = ""


# Usage line -> what it does. The order is the order the cheatsheet reads in.
# `[desk]` is optional everywhere: on a project page it is the page's desk,
# and on the overview it is whatever the composer's project select says.
HELP = [
    ("/approve [desk] <n>", "approve issue or PR n"),
    ("/merge [desk] pr <n>", "merge a PR with no review yet (tested first)"),
    ("/reject [desk] <n>", "reject issue or PR n"),
    ("/release [desk]", "ask for a release now"),
    ("/tell [desk] <who> <text>", "steer a live run (agent name or run id)"),
    ("/stop [desk] <who>", "stop a live run"),
    ("/budget [desk] <usd>", "set the daily budget"),
    ("/policy [desk] <key> <value>", "set a policy"),
    ("/cycle", "run a cycle now"),
    ("/p <desk>", "jump to a desk"),
    ("/? or /help", "this list"),
]

CHEATSHEET = "\n".join(
    ["Commands:"]
    + [f"  {usage:<30}{what}" for usage, what in HELP]
    + ["", "Anything without a leading / goes to Harry as a direction."])

# What a policy will take from a command. The gates take the words the
# settings page offers; the counters take a number. Keys in neither map
# (active_hours) are free text, as they are on the settings page.
POLICY_CHOICES = {
    "file_issues": ("auto", "off"),
    "fix_issues": ("auto", "lead", "approve"),
    "merge_prs": ("auto", "approve"),
    "merge_dependabot": ("auto", "approve"),
    "post_comments": ("auto", "approve"),
    "cut_release": ("auto", "approve"),
}
NUMERIC_POLICIES = ("release_min_changes", "release_max_age_days",
                    "daily_budget_usd")


def parse(text: str, default_project: str = "") -> Command | None:
    """A command, or None when the text is prose for Harry.

    Only a leading `/` makes a command; anything else — including a stray
    slash mid-sentence — is a direction exactly as before.
    """
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    words = text[1:].split()
    if not words:
        raise CommandError(CHEATSHEET)
    verb = words[0].lower()
    handler = VERBS.get(verb)
    if not handler:
        raise CommandError(f"No such command: /{verb}\n\n{CHEATSHEET}")
    return handler(verb, words[1:], default_project)


# --- shared argument handling ----------------------------------------------

def _desk(words: list[str], default_project: str, needed: int,
          usage: str) -> tuple[str, list[str]]:
    """Peel an optional leading desk name off the arguments.

    A desk is only ever spelled out when there is one argument more than the
    command needs, so `/budget 100` and `/budget may 100` both work and a
    typo in the desk name is caught rather than read as a value.
    """
    if len(words) > needed:
        name = words[0]
        if not db.get_project(name):
            raise CommandError(f"No desk called '{name}'. Usage: {usage}")
        return name, words[1:]
    if not (default_project and db.get_project(default_project)):
        raise CommandError(f"Which desk? Usage: {usage}")
    return default_project, words


def _item(verb: str, words: list[str], default_project: str) -> Command:
    """`/approve`, `/reject`, `/merge` — a desk, a number, maybe a kind.

    GitHub numbers an issue and a PR from the same sequence, so a bare number
    is resolved against the desk's items rather than assumed to be an issue.
    """
    usage = f"/{verb} [desk] [issue|pr] <number>"
    kind, number, desk = "", None, ""
    for word in words:
        low = word.lower()
        if low in ("issue", "pr"):
            kind = low
        elif word.lstrip("#").isdigit():
            number = int(word.lstrip("#"))
        elif not desk and db.get_project(word):
            desk = word
        else:
            raise CommandError(f"/{verb}: '{word}' is not a number, a kind or "
                               f"a desk. Usage: {usage}")
    if number is None:
        raise CommandError(f"Which one? Usage: {usage}")
    project, _ = _desk([desk] if desk else [], default_project, 0, usage)
    # /merge is about a PR by definition; without a kind, look at both.
    kinds = (kind,) if kind else ("pr",) if verb == "merge" else ("issue", "pr")
    for k in kinds:
        if db.get_item(project, k, number):
            # A PR still in 'new' is sent straight to a tested merge by the
            # approve route itself, so /merge is an alias, not a new action.
            return Command("approve" if verb == "merge" else verb,
                           project=project, kind=k, number=number)
    raise CommandError(f"{project} has no {' or '.join(kinds)} #{number}.")


def _live_run(verb: str, project: str, who: str) -> int:
    """The run `/tell` or `/stop` means: a run id, or an agent's live run."""
    if who.lstrip("#").isdigit():
        run_id = int(who.lstrip("#"))
        run = db.get_run(run_id)
        if not run or run["finished_at"] is not None:
            raise CommandError(f"Run {run_id} is not running.")
        return run_id
    runs = db.live_runs(project, who)
    if not runs:
        raise CommandError(
            f"{who} has no live run on {project}. Say it without the leading "
            "/ to file it as a direction instead.")
    if len(runs) > 1:
        ids = ", ".join(str(r["id"]) for r in runs)
        raise CommandError(f"{who} has {len(runs)} live runs on {project} "
                           f"({ids}) — say which: /{verb} <run id> …")
    return runs[0]["id"]


# --- one function per verb --------------------------------------------------

def _tell(verb, words, default_project) -> Command:
    usage = "/tell [desk] <agent or run id> <what to say>"
    # The message is free text, so a desk is only read off the front when
    # there is still an agent and something to say behind it.
    if len(words) >= 3 and db.get_project(words[0]):
        project, words = words[0], words[1:]
    else:
        project, words = _desk(words, default_project, len(words), usage)
    if len(words) < 2:
        raise CommandError(f"Say what? Usage: {usage}")
    run_id = _live_run("tell", project, words[0])
    return Command("tell", project=project, run_id=run_id,
                   text=" ".join(words[1:]))


def _stop(verb, words, default_project) -> Command:
    usage = "/stop [desk] <run id or agent>"
    project, words = _desk(words, default_project, 1, usage)
    if len(words) != 1:
        raise CommandError(f"Stop what? Usage: {usage}")
    return Command("stop", project=project,
                   run_id=_live_run("stop", project, words[0]))


def _policy(verb, words, default_project) -> Command:
    usage = ("/budget [desk] <usd>" if verb == "budget"
             else "/policy [desk] <key> <value>")
    needed = 1 if verb == "budget" else 2
    project, words = _desk(words, default_project, needed, usage)
    if len(words) != needed:
        raise CommandError(f"Usage: {usage}")
    key, value = ("daily_budget_usd", words[0]) if verb == "budget" else words
    key = key.lower()
    if key not in config.POLICY_DEFAULTS:
        raise CommandError(f"No policy called '{key}'. Keys: "
                           + ", ".join(sorted(config.POLICY_DEFAULTS)))
    choices = POLICY_CHOICES.get(key)
    if choices and value.lower() not in choices:
        raise CommandError(f"{key} takes {' or '.join(choices)}, not "
                           f"'{value}'.")
    if key in NUMERIC_POLICIES:
        try:
            float(value)
        except ValueError:
            raise CommandError(f"{key} takes a number, not '{value}'.") from None
    return Command("policy", project=project, key=key,
                   value=value.lower() if choices else value)


def _release(verb, words, default_project) -> Command:
    project, rest = _desk(words, default_project, 0, "/release [desk]")
    if rest:
        raise CommandError("Usage: /release [desk]")
    return Command("release", project=project)


def _jump(verb, words, default_project) -> Command:
    if len(words) != 1:
        raise CommandError("Usage: /p <desk>")
    if not db.get_project(words[0]):
        raise CommandError(f"No desk called '{words[0]}'.")
    return Command("p", project=words[0])


def _cycle(verb, words, default_project) -> Command:
    if words:
        raise CommandError("Usage: /cycle")
    return Command("cycle")


def _help(verb, words, default_project) -> Command:
    return Command("help")


VERBS = {
    "approve": _item,
    "reject": _item,
    "merge": _item,
    "release": _release,
    "tell": _tell,
    "stop": _stop,
    "budget": _policy,
    "policy": _policy,
    "p": _jump,
    "cycle": _cycle,
    "help": _help,
    "?": _help,
}
