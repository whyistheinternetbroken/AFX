---
name: update-version
description: "Bump AFX_reinit.py SCRIPT_VERSION on command phrases like: update version major, update version minor."
---

# Update Version

Updates `SCRIPT_VERSION` in `AFX_reinit.py`.

## Triggers

- `update version major`
- `update version minor`

## Rules

1. Read `SCRIPT_VERSION = "X.Y.Z"` from `AFX_reinit.py`.
2. If request is **major**:
   - bump `X` by 1
   - set `Y=0`, `Z=0`
3. If request is **minor**:
   - bump `Y` by 1
   - set `Z=0`
4. Keep patch updates manual unless user asks.
5. Do not edit any unrelated code.
6. Run `python -m py_compile AFX_reinit.py` after editing.

## Notes

- `--version` prints version and last update timestamp.
- Last update timestamp is derived from git history (`git log -1 --format=%cI -- AFX_reinit.py`), so each new commit/push updates it automatically.
