---
name: phase-consistency-check
description: Use when fixing behavior in one reinit phase and you must propagate/verify the same fix across phases 1-4 in AFX_reinit.py.
---

# Phase Consistency Check

Use this skill whenever a fix lands in one phase of `AFX_reinit.py` and could also affect other phases.

## Goal

Prevent phase drift where one path is fixed but sibling phase paths still use older logic.

## Mandatory workflow

1. Identify the exact helper/function/block changed.
2. Search all phase paths (1-4) for equivalent logic and direct call-site variants.
3. Apply the same hardening/guardrails to each relevant path, not only the reported failing phase.
4. Keep both script copies in sync:
   - `AFX_reinit.py`
   - `AFX/AFX_reinit.py`
5. Validate with compile checks on both files before concluding.

## Checklist for boot-menu related fixes

When changing boot-menu detection/selection, explicitly audit for:

- direct `send("4\r")`, `send("6\r")`, or `send("9\r")` usage
- waits that assume selection succeeded without confirmation
- per-node parallel workers and primary-only paths
- retry paths that re-enter boot menu handling
- known_hosts cleanup calls tied to menu selection

## Example audit commands

```powershell
rg "send\\(\"[469]\\\\r\"\\)" AFX_reinit.py AFX/AFX_reinit.py
rg "boot menu|option 4|option 6|option 9" AFX_reinit.py AFX/AFX_reinit.py
python -m py_compile AFX_reinit.py AFX\AFX_reinit.py
```

## Completion criteria

- All relevant phase-1..4 call sites updated consistently.
- Root and `AFX/` copy are identical for the modified logic.
- No path can report success for menu selection without a confirmation signal.
