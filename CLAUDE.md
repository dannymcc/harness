# Wilman — project rules

## Git commits

- Never include AI attribution lines (no "Co-Authored-By: Claude", no
  "Generated with Claude Code").
- Conventional commits: `feat:`, `fix:`, `docs:`, `chore:`, `security:`.

## Architecture rules

- Agents judge; deterministic code acts. Any new outward action (push, merge,
  comment, tag, close) must live in `pipeline.py`/`gh.py` behind a policy
  gate — never inside an agent session.
- Agent sessions must keep `disallowed_tools` blocking `git push`, `gh`, and
  network tools. Prompts alone are not a security boundary.
- The harness re-runs tests itself before pushing or merging anything.
  Never trust an agent's claim that tests pass.
- The GUI has no auth: it must stay tailnet-only. Don't add features that
  assume public exposure.
- New persistent state needs a column default that keeps old `data/wilman.db`
  files loading (SQLite `ALTER TABLE ... ADD COLUMN` with defaults, or the
  CREATE TABLE guards in `db.py`).

## Checks before committing

```sh
python -m compileall -q wilman run.py
```
