"""Boot wiring in run.py: keeping agent sessions on the data volume."""
from pathlib import Path


def test_claude_home_lands_on_the_data_volume(fresh_db, monkeypatch, tmp_path):
    """Sessions must outlive a container recreate, so ~/.claude points at
    DATA_DIR/claude-home — and doing it twice (every boot) is a no-op."""
    import run as entry
    from harness import config
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    entry.persist_claude_home()
    link = home / ".claude"
    assert link.is_symlink()
    assert link.resolve() == (config.DATA_DIR / "claude-home").resolve()

    # a session written before a restart is still there after the next boot
    (link / "projects").mkdir()
    (link / "projects" / "session.jsonl").write_text("transcript")
    entry.persist_claude_home()
    assert (config.DATA_DIR / "claude-home" / "projects"
            / "session.jsonl").read_text() == "transcript"


def test_a_developers_own_claude_home_is_left_alone(fresh_db, monkeypatch,
                                                    tmp_path):
    """Run outside the container, ~/.claude is the developer's own — moving
    it into the project's data dir is not ours to do."""
    import run as entry
    from harness import config
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "settings.json").write_text("{}")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    entry.persist_claude_home()
    assert not (home / ".claude").is_symlink()
    assert (home / ".claude" / "settings.json").exists()
    assert not (config.DATA_DIR / "claude-home").exists()
