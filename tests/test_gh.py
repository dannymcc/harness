"""Cover gh.py, which builds every outward action the harness takes.

Comment, create, close, retarget, merge, release, PR creation: each one is a
list of strings handed to the `gh` CLI, and until now nothing asserted what
was in it (issue #104). The rest of the suite monkeypatches this module or
stubs `gh.run`, so a regression that dropped `-R <repo>`, lost `--squash`
from merge_pr or reordered close_pr's comment-then-close would pass
everything and only show up against a live repo.

So the tests below stub subprocess.run and assert on the captured argv.
Nothing here launches a process, reaches the network or needs the gh binary
installed -- the fake records the command and hands back canned output.
"""
import subprocess

import pytest


class _Fake:
    """Stands in for subprocess.run, recording calls and replaying output."""

    def __init__(self):
        self.calls = []          # one dict per call: cmd, cwd, timeout, env
        self.stdout = ""
        self.stderr = ""
        self.returncode = 0

    def __call__(self, cmd, cwd=None, capture_output=True, text=True,
                 timeout=600, env=None):
        self.calls.append(
            {"cmd": list(cmd), "cwd": cwd, "timeout": timeout, "env": env})
        return subprocess.CompletedProcess(
            cmd, self.returncode, self.stdout, self.stderr)

    @property
    def cmd(self):
        """The argv of the one and only call."""
        assert len(self.calls) == 1, f"expected one call, got {self.calls}"
        return self.calls[0]["cmd"]


@pytest.fixture()
def fake(monkeypatch):
    f = _Fake()
    monkeypatch.setattr(subprocess, "run", f)
    return f


def _repo_flag(cmd):
    """The value of -R in an argv, or None if the flag is missing."""
    if "-R" not in cmd:
        return None
    return cmd[cmd.index("-R") + 1]


# --- run() ------------------------------------------------------------------

def test_run_returns_stdout_and_forwards_cwd_and_timeout(fake, tmp_path):
    from harness import gh
    fake.stdout = "hello\n"
    assert gh.run(["git", "status"], cwd=tmp_path, timeout=30) == "hello\n"
    assert fake.calls[0]["cwd"] == tmp_path
    assert fake.calls[0]["timeout"] == 30


def test_a_failed_command_raises_cmderror_carrying_the_detail(fake):
    """Callers catch CmdError and put its parts in front of a human (a hold
    reason, a PR comment): the command, the exit code and both streams all
    have to survive the raise."""
    from harness import gh
    fake.returncode = 128
    fake.stdout = "partial\n"
    fake.stderr = "fatal: no such remote\n"

    with pytest.raises(gh.CmdError) as excinfo:
        gh.run(["git", "push", "origin", "dev"])

    err = excinfo.value
    assert err.cmd == ["git", "push", "origin", "dev"]
    assert err.code == 128
    assert err.out == "partial\n"
    assert err.err == "fatal: no such remote\n"
    assert "git push origin dev" in str(err)
    assert "fatal: no such remote" in str(err)


def test_check_false_returns_stdout_instead_of_raising(fake):
    """repo.run_tests runs the project's own setup command with check=False:
    a failure there is reported, not raised."""
    from harness import gh
    fake.returncode = 1
    fake.stdout = "1 failed\n"
    assert gh.run(["sh", "-c", "false"], check=False) == "1 failed\n"


def test_cmderror_falls_back_to_stdout_when_stderr_is_empty(fake):
    from harness import gh
    fake.returncode = 1
    fake.stdout = "everything the tool had to say\n"
    with pytest.raises(gh.CmdError) as excinfo:
        gh.run(["gh", "pr", "merge", "1"])
    assert "everything the tool had to say" in str(excinfo.value)


def test_env_none_inherits_and_an_explicit_env_is_passed_through_untouched(
        fake, monkeypatch):
    """The sandbox contract in repo._sandbox_env rests on this: git and gh
    need harness's own environment (and so its token), while anything the
    project supplies gets a scrubbed env that must arrive exactly as built.
    Merging the two, or defaulting env to os.environ, would hand the token to
    a contributor's test command."""
    from harness import gh
    monkeypatch.setenv("GH_TOKEN", "ghp_secret_token")

    gh.run(["gh", "issue", "list"])
    assert fake.calls[0]["env"] is None, (
        "env must stay None so the child inherits harness's environment")

    scrubbed = {"PATH": "/usr/bin", "HOME": "/scratch"}
    gh.run(["sh", "-c", "true"], env=scrubbed)
    assert fake.calls[1]["env"] == {"PATH": "/usr/bin", "HOME": "/scratch"}
    assert "GH_TOKEN" not in fake.calls[1]["env"]


# --- gh_json ----------------------------------------------------------------

def test_gh_json_parses_a_payload_and_appends_the_repo(fake):
    from harness import gh
    fake.stdout = '[{"number": 7, "title": "a"}]'
    got = gh.gh_json("owner/repo", ["issue", "list", "--state", "open"])
    assert got == [{"number": 7, "title": "a"}]
    assert fake.cmd == ["gh", "issue", "list", "--state", "open",
                        "-R", "owner/repo"]


@pytest.mark.parametrize("out", ["", "\n", "   \n"])
def test_gh_json_returns_an_empty_list_for_empty_output(fake, out):
    """gh prints nothing at all for some empty results. Callers iterate the
    return value, so it has to be a list and not a JSONDecodeError."""
    from harness import gh
    fake.stdout = out
    assert gh.gh_json("owner/repo", ["pr", "list"]) == []


def test_gh_json_reads_an_object_payload_too(fake):
    from harness import gh
    fake.stdout = '{"number": 12, "title": "t"}'
    assert gh.gh_json("owner/repo", ["issue", "view", "12"]) == {
        "number": 12, "title": "t"}


# --- reading ----------------------------------------------------------------

def test_pr_diff_passes_a_short_diff_through_unchanged(fake):
    from harness import gh
    fake.stdout = "diff --git a/x b/x\n+one line\n"
    assert gh.pr_diff("owner/repo", 4) == "diff --git a/x b/x\n+one line\n"
    assert fake.cmd == ["gh", "pr", "diff", "4", "-R", "owner/repo"]


def test_pr_diff_truncates_at_max_chars_and_says_so(fake):
    """A huge diff would otherwise fill a review prompt's context. The marker
    matters as much as the cut: without it the reviewer reads a diff that
    stops mid-hunk and has no way to know it was clipped."""
    from harness import gh
    fake.stdout = "x" * 500
    out = gh.pr_diff("owner/repo", 4, max_chars=100)
    assert out.startswith("x" * 100)
    assert not out.startswith("x" * 101)
    assert out.endswith("... [diff truncated] ...")


def test_pr_diff_leaves_a_diff_exactly_at_the_limit_alone(fake):
    from harness import gh
    fake.stdout = "x" * 100
    assert gh.pr_diff("owner/repo", 4, max_chars=100) == "x" * 100


# --- reading: CI ------------------------------------------------------------
#
# commit_ci is what stops a release being announced as shipped over a red
# build (#112). It is a read, but a read that lost -R would report the CI of
# whatever repo harness itself is checked out in — green, always, and about
# the wrong project.

def test_commit_ci_asks_about_the_commit_in_the_named_repo(fake):
    from harness import gh
    fake.stdout = ('[{"databaseId": 1, "name": "ci", "status": "completed", '
                   '"conclusion": "success", "url": "u"}]')
    gh.commit_ci("owner/repo", "abc123")
    assert fake.cmd == ["gh", "run", "list", "--commit", "abc123",
                        "--limit", "20", "--json",
                        "databaseId,name,status,conclusion,url",
                        "-R", "owner/repo"]


def test_commit_ci_is_done_and_green_when_every_run_passed(fake):
    from harness import gh
    fake.stdout = ('[{"status": "completed", "conclusion": "success", '
                   '"url": "u1"},'
                   ' {"status": "completed", "conclusion": "skipped", '
                   '"url": "u2"}]')
    assert gh.commit_ci("owner/repo", "abc") == {
        "state": "done", "conclusion": "success", "url": "u1"}


def test_commit_ci_reports_the_run_that_went_red(fake):
    """The url has to be the failing run's, not the first run's: it is what
    the operator and the follow-up issue are pointed at."""
    from harness import gh
    fake.stdout = ('[{"status": "completed", "conclusion": "success", '
                   '"url": "green"},'
                   ' {"status": "completed", "conclusion": "failure", '
                   '"url": "red"}]')
    assert gh.commit_ci("owner/repo", "abc") == {
        "state": "done", "conclusion": "failure", "url": "red"}


@pytest.mark.parametrize("conclusion", [
    "failure", "timed_out", "cancelled", "startup_failure", "action_required",
])
def test_commit_ci_treats_anything_but_a_pass_as_red(fake, conclusion):
    """Only success, skipped and neutral are builds that did what they
    normally do. A cancelled or timed-out run published nothing either."""
    from harness import gh
    fake.stdout = ('[{"status": "completed", "conclusion": "%s", '
                   '"url": "u"}]' % conclusion)
    got = gh.commit_ci("owner/repo", "abc")
    assert got["state"] == "done" and got["conclusion"] == conclusion


def test_commit_ci_is_pending_while_any_run_is_unfinished(fake):
    """An unfinished run has conclusion null — reading that as a verdict
    would call every release green a second after the tag was pushed."""
    from harness import gh
    fake.stdout = ('[{"status": "completed", "conclusion": "success", '
                   '"url": "u1"},'
                   ' {"status": "in_progress", "conclusion": null, '
                   '"url": "u2"}]')
    assert gh.commit_ci("owner/repo", "abc") == {
        "state": "pending", "conclusion": "", "url": "u1"}


def test_commit_ci_says_none_when_no_run_exists_for_the_commit(fake):
    from harness import gh
    fake.stdout = "[]"
    assert gh.commit_ci("owner/repo", "abc") == {
        "state": "none", "conclusion": "", "url": ""}


# --- acting: argv -----------------------------------------------------------

def test_comment_issue(fake):
    from harness import gh
    gh.comment_issue("owner/repo", 12, "a body")
    assert fake.cmd == ["gh", "issue", "comment", "12", "-R", "owner/repo",
                        "--body", "a body"]


def test_comment_pr(fake):
    from harness import gh
    gh.comment_pr("owner/repo", 12, "a body")
    assert fake.cmd == ["gh", "pr", "comment", "12", "-R", "owner/repo",
                        "--body", "a body"]


def test_create_issue(fake):
    from harness import gh
    fake.stdout = "https://github.com/owner/repo/issues/31\n"
    assert gh.create_issue("owner/repo", "a title", "a body") == 31
    assert fake.cmd == ["gh", "issue", "create", "-R", "owner/repo",
                        "--title", "a title", "--body", "a body"]


def test_close_issue_without_a_comment_omits_the_flag(fake):
    """Not `--comment ""`: gh would post an empty comment on the issue."""
    from harness import gh
    gh.close_issue("owner/repo", 12)
    assert fake.cmd == ["gh", "issue", "close", "12", "-R", "owner/repo"]
    assert "--comment" not in fake.cmd


def test_close_issue_with_a_comment(fake):
    from harness import gh
    gh.close_issue("owner/repo", 12, "done here")
    assert fake.cmd == ["gh", "issue", "close", "12", "-R", "owner/repo",
                        "--comment", "done here"]


def test_retarget_pr(fake):
    from harness import gh
    gh.retarget_pr("owner/repo", 12, "dev")
    assert fake.cmd == ["gh", "pr", "edit", "12", "-R", "owner/repo",
                        "--base", "dev"]


def test_merge_pr_squashes_by_default(fake):
    """Every fix and community PR lands squashed onto dev; only the release
    PR (pipeline passes squash=False) keeps its commits."""
    from harness import gh
    gh.merge_pr("owner/repo", 12)
    assert fake.cmd == ["gh", "pr", "merge", "12", "-R", "owner/repo",
                        "--squash"]


def test_merge_pr_without_squash_is_a_merge_commit(fake):
    from harness import gh
    gh.merge_pr("owner/repo", 12, squash=False)
    assert fake.cmd == ["gh", "pr", "merge", "12", "-R", "owner/repo",
                        "--merge"]
    assert "--squash" not in fake.cmd


def test_close_pr_comments_before_it_closes(fake):
    """The order is the whole point: a comment posted after the close reads
    as a note on a dead PR, and if the close fails the author is left with
    nothing explaining why the PR is still open."""
    from harness import gh
    gh.close_pr("owner/repo", 12, "not this one, sorry")
    assert [c["cmd"] for c in fake.calls] == [
        ["gh", "pr", "comment", "12", "-R", "owner/repo",
         "--body", "not this one, sorry"],
        ["gh", "pr", "close", "12", "-R", "owner/repo"],
    ]


def test_close_pr_without_a_comment_only_closes(fake):
    from harness import gh
    gh.close_pr("owner/repo", 12)
    assert fake.cmd == ["gh", "pr", "close", "12", "-R", "owner/repo"]


def test_publish_release_verifies_the_tag(fake):
    """--verify-tag makes gh fail if the tag was never pushed, rather than
    quietly creating one at whatever the default branch points at."""
    from harness import gh
    gh.publish_release("owner/repo", "v1.2.3", "v1.2.3", "the notes")
    assert fake.cmd == ["gh", "release", "create", "v1.2.3", "-R", "owner/repo",
                        "--verify-tag", "--title", "v1.2.3",
                        "--notes", "the notes"]


def test_create_pr(fake):
    from harness import gh
    fake.stdout = "https://github.com/owner/repo/pull/44\n"
    got = gh.create_pr("owner/repo", "dev", "harness/issue-1", "t", "b")
    assert got == 44
    assert fake.cmd == ["gh", "pr", "create", "-R", "owner/repo",
                        "--base", "dev", "--head", "harness/issue-1",
                        "--title", "t", "--body", "b"]


# --- acting: the repo flag --------------------------------------------------

def test_every_outward_action_names_the_repo(fake):
    """gh falls back to the repo of the current directory when -R is absent.
    Harness runs from its own checkout, so an action that lost the flag would
    not fail -- it would comment on, close or merge something in
    dannymcc/harness instead of the project it was working on."""
    from harness import gh
    fake.stdout = "https://github.com/owner/repo/pull/1\n"
    actions = [
        lambda: gh.comment_issue("owner/repo", 1, "b"),
        lambda: gh.comment_pr("owner/repo", 1, "b"),
        lambda: gh.create_issue("owner/repo", "t", "b"),
        lambda: gh.close_issue("owner/repo", 1),
        lambda: gh.close_issue("owner/repo", 1, "c"),
        lambda: gh.retarget_pr("owner/repo", 1, "dev"),
        lambda: gh.merge_pr("owner/repo", 1),
        lambda: gh.close_pr("owner/repo", 1, "c"),
        lambda: gh.publish_release("owner/repo", "v1", "v1", "n"),
        lambda: gh.create_pr("owner/repo", "dev", "h", "t", "b"),
    ]
    for act in actions:
        fake.calls.clear()
        act()
        assert fake.calls, "action ran no command at all"
        for call in fake.calls:
            assert call["cmd"][0] == "gh"
            assert _repo_flag(call["cmd"]) == "owner/repo", (
                f"{call['cmd']} does not name the repo it acts on")


# --- number parsing ---------------------------------------------------------
#
# create_issue and create_pr both read the number out of the URL gh prints.
# gh's exact output has moved around between versions (trailing newline,
# occasional trailing slash, a preamble line before the URL), and the number
# is what the harness then stores against the item, so a mis-parse is not a
# crash but a wrong row pointing at somebody else's issue.

@pytest.mark.parametrize("out, expected", [
    ("https://github.com/owner/repo/issues/31", 31),
    ("https://github.com/owner/repo/issues/31\n", 31),
    ("https://github.com/owner/repo/issues/31/", 31),
    ("https://github.com/owner/repo/issues/31/\n", 31),
    ("  https://github.com/owner/repo/issues/31  \n", 31),
    ("https://github.com/owner/repo/issues/1234567", 1234567),
])
def test_create_issue_reads_the_number_from_the_url(fake, out, expected):
    from harness import gh
    fake.stdout = out
    assert gh.create_issue("owner/repo", "t", "b") == expected


@pytest.mark.parametrize("out, expected", [
    ("https://github.com/owner/repo/pull/44", 44),
    ("https://github.com/owner/repo/pull/44\n", 44),
    ("https://github.com/owner/repo/pull/44/\n", 44),
    ("Creating pull request for h into dev in owner/repo\n\n"
     "https://github.com/owner/repo/pull/44\n", 44),
])
def test_create_pr_reads_the_number_from_the_url(fake, out, expected):
    from harness import gh
    fake.stdout = out
    assert gh.create_pr("owner/repo", "dev", "h", "t", "b") == expected


@pytest.mark.parametrize("out", [
    "",
    "\n",
    "https://github.com/owner/repo/pull/",
    "no url here at all",
    "https://github.com/owner/repo/pull/44-and-a-suffix",
])
def test_unparseable_output_raises_rather_than_inventing_a_number(fake, out):
    """The failure has to be loud. A silently wrong number would be recorded
    against the item and every later comment, close or merge would land on
    whatever PR happens to hold it."""
    from harness import gh
    fake.stdout = out
    with pytest.raises(ValueError):
        gh.create_pr("owner/repo", "dev", "h", "t", "b")
    with pytest.raises(ValueError):
        gh.create_issue("owner/repo", "t", "b")
