---
name: troubleshoot
description: "Review failed-run logs in script_failures/, summarize root issues, and ask whether to implement fixes. Use when: troubleshoot, investigate failed run, analyze logs, why did this hang, why reconnect failed, why run passed incorrectly."
---

# Troubleshoot

Investigate failed script runs using logs in `script_failures/`, produce a root-cause summary, then ask whether to apply fixes.

## When to Use

- User asks to investigate a failed run from `script_failures/`
- User asks why a run hung, why reconnect logic did not recover, or why a run result was incorrect
- User wants a postmortem summary before deciding on code changes

## Workflow

1. Identify the run folder to inspect:
   - Use the user-specified folder when provided
   - Otherwise inspect the most recent timestamped folder in `script_failures/`

2. Read the high-signal logs first:
   - `summary_*.log`
   - `bmc_session_*.log`
   - `screen_output_*.log`
   - Relevant per-node logs (`4b_node_*`, `4b_opt6_*`, `1b_boot_*`, `loader_env_*`, etc.)

3. Build findings with evidence:
   - Exact symptom timeline (where it stalled or diverged)
   - Why reconnect did/did not trigger
   - Why completion/outcome classification was wrong (if applicable)
   - Specific code-path candidates in `AFX_reinit.py`

4. Produce a concise troubleshooting report:
   - **Issue**
   - **Likely root cause**
   - **Evidence** (file + relevant lines/time window)
   - **Impact**

5. Ask the user whether to fix now (use the ask_user tool):
   - Preferred choices:
     - `Yes, implement fixes now (Recommended)`
     - `No, investigation summary only`

6. If user chooses yes:
   - Implement targeted fixes in `AFX_reinit.py`
   - Run `python -m py_compile AFX_reinit.py`
   - Report what changed and why

## Guardrails

- Do not mark investigation as complete without log-backed evidence.
- Do not claim PASS if health checks or critical phases failed.
- Do not commit or push unless explicitly requested.
- Keep edits surgical and focused on confirmed root causes.
