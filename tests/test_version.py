"""The footer must name the build that is actually running."""
import subprocess

import pytest


@pytest.fixture()
def build(monkeypatch, tmp_path):
    """Resolve a build SHA with env, stamp file and checkout all controlled."""
    def _build(env="", stamp=None, git=""):
        from harness import config
        monkeypatch.setenv("HARNESS_GIT_SHA", env)
        stamp_path = tmp_path / "_build_sha"
        if stamp is None:
            stamp_path.unlink(missing_ok=True)
        else:
            stamp_path.write_text(stamp)
        monkeypatch.setattr(config, "_STAMP_FILE", stamp_path)
        monkeypatch.setattr(config, "_git_head_sha", lambda: git)
        sha = config._build_sha()
        return sha, config._display(sha)
    return _build


def test_env_wins_and_truncates(build):
    sha, display = build(env="abc1234def5678", stamp="9999999", git="1111111")
    assert sha == "abc1234" and display.endswith("(abc1234)")


def test_stamp_file_used_when_env_absent(build):
    assert build(stamp="deadbee\n", git="1111111")[0] == "deadbee"


def test_checkout_head_used_when_env_and_stamp_absent(build):
    assert build(git="1111111")[0] == "1111111"


def test_nothing_known_says_unknown_build(build):
    from harness import config
    sha, display = build()
    assert sha == "" and display == f"v{config.VERSION} (unknown build)"


def test_rubbish_is_not_treated_as_a_sha(build):
    assert build(env="not-a-sha")[0] == ""
    assert build(stamp="fatal: not a git repository")[0] == ""
    assert build(stamp="")[0] == ""


def test_git_lookup_is_guarded(monkeypatch):
    """A missing git, a slow git or a hostile repo must not stop the boot."""
    from harness import config

    def raiser(exc):
        def _run(*a, **k):
            raise exc
        return _run

    for boom in (FileNotFoundError("git"),
                 subprocess.TimeoutExpired("git", 5),
                 OSError("nope")):
        monkeypatch.setattr(subprocess, "run", raiser(boom))
        assert config._git_head_sha() == ""

    class Failed:                       # e.g. "detected dubious ownership"
        returncode = 128
        stdout = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Failed())
    assert config._git_head_sha() == ""


def test_footer_shows_the_resolved_build(client):
    """What the operator reads is exactly what config resolved to."""
    from harness import config
    html = client.get("/").text
    assert config.DISPLAY_VERSION in html
    assert "harness/commit/" not in html   # plain text, never a stale link


def test_footer_admits_an_unknown_build(client, monkeypatch):
    from harness import config
    monkeypatch.setattr(config, "DISPLAY_VERSION", config._display(""))
    html = client.get("/").text
    assert f"v{config.VERSION} (unknown build)" in html
    assert "commit/" not in html
