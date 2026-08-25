"""Issue #42: a session that ends with subtype='success' but never calls the
StructuredOutput tool leaves result.structured_output as None. run_agent must
not report that as ok=True — every caller (pipeline.py, housekeeping.py)
assumes output is a dict once ok is true, and subscripts it unguarded.
"""
import asyncio

import pytest

from claude_agent_sdk import ResultMessage


class _FakeClient:
    """Minimal stand-in for ClaudeSDKClient: yields one ResultMessage and
    nothing else, like a session that finished without using any tools."""

    def __init__(self, result):
        self._result = result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt):
        return None

    async def receive_messages(self):
        yield self._result


def _fake_sdk_client(result):
    def factory(*a, **k):
        return _FakeClient(result)
    return factory


def test_success_with_no_structured_output_is_not_ok(fresh_db, may, monkeypatch):
    """The literal scenario from the bug report: the CLI reports subtype
    'success' but result.structured_output is None. That must not be
    reported as ok=True with output=None — it must fail cleanly with a
    named cause, and it must not raise."""
    from harness import agents

    result = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id="sess-1", total_cost_usd=0.01,
        structured_output=None)
    monkeypatch.setattr(agents, "ClaudeSDKClient", _fake_sdk_client(result))

    res = asyncio.run(agents.run_agent(
        project_name="may", role="ic", item_key="issue#1", task="fix",
        prompt="x", cwd=None, schema={}))

    assert res["ok"] is False
    assert res["output"] is None
    assert res["error"]  # names the cause
    assert "structured output" in res["error"].lower()


def test_missing_structured_output_is_recorded_as_a_failed_run(fresh_db, may,
                                                               monkeypatch):
    """The run row must say the run failed and why, so the console and the
    circuit breaker see it for what it is rather than a clean success."""
    from harness import agents

    result = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id="sess-2", total_cost_usd=0.01,
        structured_output=None)
    monkeypatch.setattr(agents, "ClaudeSDKClient", _fake_sdk_client(result))

    asyncio.run(agents.run_agent(
        project_name="may", role="ic", item_key="issue#2", task="fix",
        prompt="x", cwd=None, schema={}))

    run = fresh_db.get_run(1)
    assert run["ok"] == 0
    assert run["finished_at"]
    assert "structured output" in run["summary"].lower()


def test_non_dict_structured_output_is_not_ok(fresh_db, may, monkeypatch):
    """Same guard for output that is present but not a dict — callers
    subscript it by key, so a string or list is no more usable than None."""
    from harness import agents

    result = ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
        num_turns=1, session_id="sess-3", total_cost_usd=0.01,
        structured_output="not a dict")
    monkeypatch.setattr(agents, "ClaudeSDKClient", _fake_sdk_client(result))

    res = asyncio.run(agents.run_agent(
        project_name="may", role="ic", item_key="issue#3", task="fix",
        prompt="x", cwd=None, schema={}))

    assert res["ok"] is False
    assert res["output"] is None


def test_fix_item_parks_for_retry_instead_of_crashing(fresh_db, may,
                                                       monkeypatch, tmp_path):
    """fix_item must survive a fix_issue call that reports ok=False with no
    output (the corrected behaviour for a missing-structured-output run):
    the item goes back to 'approved' for a normal retry, not an unhandled
    TypeError that drops the item out of the wave."""
    from harness import agents, gh, pipeline, repo

    fresh_db.upsert_item("may", "issue", 40, "t", "a", "open", "x")
    fresh_db.update_item("may", "issue", 40, status="approved", plan="do it")

    monkeypatch.setattr(gh, "issue_detail",
                        lambda repo_, number: {"number": 40, "title": "t",
                                               "body": "b"})
    monkeypatch.setattr(repo, "add_worktree",
                        lambda project, branch: (tmp_path, ""))

    async def fake_fix_issue(project, issue, plan, cwd, resume=None,
                             persona="Malcolm", repro_path=""):
        return {"ok": False, "output": None, "session_id": "",
                "error": "session ended without structured output"}

    monkeypatch.setattr(agents, "fix_issue", fake_fix_issue)

    asyncio.run(pipeline.fix_item(may, fresh_db.get_item("may", "issue", 40)))

    item = fresh_db.get_item("may", "issue", 40)
    assert item["status"] == "approved"  # parked for retry, not crashed
