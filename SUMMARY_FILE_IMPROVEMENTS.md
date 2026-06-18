# Summary File Improvements & Next-Phase Logic

## Problem: "Next expected phase" Always Shows "(pending completion of current phase)"

Currently, the checkpoint status report displays:
```
Current phase   : Cluster Setup Wizard (1b) [in_progress], as of 2026-06-18T00:57:50.789272
Next expected phase: (pending completion of current phase)
```

This is unhelpful because:
1. **Operator can't see what comes next** — no guidance on which phase should complete next
2. **Resume flows are unclear** — when resuming from checkpoint, what phase will be skipped?
3. **Long-running phases hide progress** — operator doesn't know if "wizard" hangs before peer join or during
4. **Multi-node inconsistency** — primary may be in wizard while peers are waiting; we only show primary's perspective

---

## Solution: Define Phase Sequences & Implement Predictive Logic

### Part A: Define 4b Phase Sequence

**4b Phase Order (Mode 4b+3 Netboot+Reinit):**

```
1. 4b – Package Selection
2. Collect Node Mgmt per BMC
3. Collect Cluster Setup Config
4. 4b – BMC SSH Connections
5. 4b – Reset to LOADER
6. 4b – HTTP Server
7. 4b – Netboot Install  (parallel per-node)
   ├─ [node-1] download
   ├─ [node-1] install
   ├─ [node-2] download
   └─ [node-2] install
8. 4b – Reinit Reconnect to LOADER  (parallel per-node)
   ├─ [node-1] SSH + system reset
   ├─ [node-2] SSH + system reset
   └─ (both reach LOADER prompt)
9. 4b – Boot Menu Selection  (parallel per-node)
   ├─ [node-1] wait for menu + select option 9/4
   ├─ [node-2] wait for menu + select option 9/4
   └─ (both sent to LOADER)
10. 4b – Cluster Initialization (primary)  [OR: Resume from checkpoint]
    ├─ Primary: option 9 boot → login → create cluster
    ├─ Peer(s): wait for primary to complete (no action)
    └─ Primary: cluster creation complete
11. Auto Cluster Init (1b)  [triggered if not done]
12. Cluster Setup Wizard (1b)  [interactive, operator-driven]
13. NTP Configuration
14. (END)
```

**Key Observations:**
- Phases 7–9 are per-node but **reported as global** (represented once in checkpoint)
- Phase 10 (Cluster Init) is **primary-only** but blocks peers
- Phases 11–12 are **interactive** (operator input required)
- Some phases skip conditionally (e.g., auto-init skipped if already created)

---

### Part B: Implement Predictive Next-Phase Logic

**Code Change Location:** `AFX_reinit.py`, line ~441

```python
# Current (WRONG):
_next_phase = str(_cur.get("next_phase") or "").strip() or "(not recorded)"

# Proposed (CORRECT):
_next_phase = _predict_next_phase(
    current_phase_name=_cur_name,
    current_state=_cur_state,
    mode=_mode_raw,
    checkpoint_data=self._data,
)
```

**New Function: `_predict_next_phase()`**

```python
def _predict_next_phase(current_phase_name, current_state, mode, checkpoint_data):
    """Predict the next expected phase based on current phase and mode.
    
    Handles:
    - Sequential progression (phase N → phase N+1)
    - Skipped phases (e.g., auto-init already done)
    - Conditional branches (mode 4b+3 vs 1b only)
    - Resume safety (returns "CRITICAL" if unsafe to skip)
    """
    
    # Define phase sequences by mode
    _4b_sequence = [
        "4b – Package Selection",
        "Collect Node Mgmt per BMC",
        "Collect Cluster Setup Config",
        "4b – BMC SSH Connections",
        "4b – Reset to LOADER",
        "4b – HTTP Server",
        "4b – Netboot Install",
        "4b – Reinit Reconnect to LOADER",
        "4b – Boot Menu Selection",
        "4b – Cluster Initialization (primary)",
        "Auto Cluster Init (1b)",
        "Cluster Setup Wizard (1b)",
        "NTP Configuration",
    ]
    
    # Choose sequence based on mode
    if "4b" in mode.lower() or "reinit" in mode.lower():
        sequence = _4b_sequence
    else:
        sequence = _1b_sequence  # Define for mode 1b/2/3
    
    # Find current phase in sequence
    try:
        idx = sequence.index(current_phase_name)
    except ValueError:
        # Unknown phase; try partial match
        idx = -1
        for i, s in enumerate(sequence):
            if current_phase_name in s or s in current_phase_name:
                idx = i
                break
        if idx == -1:
            return "(unknown phase)"
    
    # Current state determines what's next
    if current_state == "complete" or current_state == "done":
        # Move to next phase in sequence
        if idx + 1 < len(sequence):
            return sequence[idx + 1]
        else:
            return "(final phase complete)"
    elif current_state == "in_progress":
        # Still in current phase; return same with hint
        return f"{current_phase_name} (in progress)"
    elif current_state == "blocked":
        # Cannot proceed; return diagnostic
        return f"(BLOCKED: {current_phase_name} failed)"
    else:
        # Unknown state
        return "(state unknown)"
```

**Example Outputs:**

```
Current: 4b – Netboot Install [complete]
Next:    4b – Reinit Reconnect to LOADER ✅

Current: 4b – Reinit Reconnect to LOADER [in_progress]
Next:    4b – Reinit Reconnect to LOADER (in progress) ⏳

Current: 4b – Cluster Initialization (primary) [complete]
Next:    Auto Cluster Init (1b) ✅

Current: Auto Cluster Init (1b) [complete, already_done]
Next:    Cluster Setup Wizard (1b) ✅

Current: Cluster Setup Wizard (1b) [in_progress]
Next:    Cluster Setup Wizard (1b) (interactive – waiting for operator input) 🤖
```

---

## Part C: Additional Summary File Improvements

### **Improvement #1: Show Per-Node Phase Progress (Not Just Global)**

**Current:**
```
Current phase   : Cluster Setup Wizard (1b) [in_progress]
Next expected phase: (pending completion of current phase)
```

**Improved:**
```
Current phase   : Cluster Setup Wizard (1b) [in_progress]
  └─ Primary [10.192.160.29]  : cluster creation complete → wizard in_progress
  └─ Peer-01 [10.192.160.35]  : waiting for primary cluster init
Next expected phase: Cluster Setup Wizard (1b) → NTP Configuration (once wizard completes)
Bottleneck: Primary wizard interaction (operator input needed)
```

**Code**: Add per-node phase matrix to status output (already partially implemented in `_format_per_node_phases()`)

---

### **Improvement #2: Show Actual vs. Predicted Time to Completion**

**Current:**
```
Age: 43 minute(s)
```

**Improved:**
```
Elapsed: 43 min
Est. time remaining:  10–15 min (based on similar 4b runs)
Est. completion: 2026-06-18 01:10 UTC
```

**Code**: Add estimation logic comparing current phase duration to historical baseline

```python
def _estimate_remaining_time(current_phase, elapsed_so_far, historical_times):
    """Return (min_estimate, max_estimate) tuple."""
    # Look up historical time for this phase in previous successful runs
    avg_phase_time = historical_times.get(current_phase, {}).get("avg", 0)
    remaining_phase_time = avg_phase_time * 1.25  # +25% buffer
    
    # Remaining phases after current
    remaining_phases = get_remaining_phases(current_phase)
    for phase in remaining_phases:
        remaining_phase_time += historical_times.get(phase, {}).get("avg", 0)
    
    return (
        int(remaining_phase_time * 0.8),    # optimistic
        int(remaining_phase_time * 1.2),    # pessimistic
    )
```

---

### **Improvement #3: Highlight Bottlenecks & Decision Points**

**Current:**
```
Current phase: Cluster Setup Wizard (1b) [in_progress]
```

**Improved:**
```
Current phase: Cluster Setup Wizard (1b) [in_progress]
⚠️  INTERACTIVE PHASE: Waiting for operator input at cluster wizard prompt
📋 Expected prompt: "Enter your choice from the menu above..."
📊 Time spent waiting: 5 min (typical 2–3 min for this prompt)
```

**Code**: Add phase metadata for interactive prompts

```python
_interactive_phases = {
    "Cluster Setup Wizard (1b)": {
        "prompts": [
            "cluster name",
            "admin password",
            "create/join",
        ],
        "typical_duration": 180,  # seconds
    },
}
```

---

### **Improvement #4: Show What Will Happen on Resume**

**Current:**
```
Next expected phase: (pending completion of current phase)
```

**Improved (if resuming):**
```
Resume mode: checkpoint detected
Skipped on resume:
  ✅ 4b – Package Selection
  ✅ Collect Node Mgmt per BMC
  ✅ 4b – Netboot Install
  ✅ 4b – Reinit Reconnect to LOADER

Will re-run:
  ⏳ 4b – Boot Menu Selection (partial; some nodes may skip if already at menu)
  ⏳ 4b – Cluster Initialization (primary)

Next phase: 4b – Boot Menu Selection
```

**Code**: Build skip list from checkpoint done phases

```python
def _show_resume_plan(checkpoint_data):
    """Show which phases will be skipped and which re-run on resume."""
    done_phases = checkpoint_data.get("phases", {})
    skipped = [name for name, meta in done_phases.items() if meta.get("done")]
    
    for phase in skipped:
        print(f"  ✅ {phase} (skipping on resume)")
    
    # Show what comes next
    next_phase = _predict_next_phase(...)
    print(f"\nWill resume at: {next_phase}")
```

---

## Part D: Implementation Checklist

- [ ] **Phase 1 (High Priority):**
  - [ ] Define phase sequences for 4b, 1b, 2, 3 modes
  - [ ] Implement `_predict_next_phase()` function
  - [ ] Update checkpoint status printer to use new prediction logic
  - [ ] Test with successful 4b run (verify phase progression correct)

- [ ] **Phase 2 (Medium Priority):**
  - [ ] Add per-node phase breakdown to status (extends current matrix)
  - [ ] Highlight interactive phases with prompt context
  - [ ] Show estimated remaining time (requires historical baseline)

- [ ] **Phase 3 (Nice-to-Have):**
  - [ ] Add phase metadata (duration, prompts, skip conditions)
  - [ ] Show resume plan when checkpoint exists
  - [ ] Detect bottlenecks and surface operator action needed

---

## Expected Improvements

**Before:**
```
Current phase   : Cluster Setup Wizard (1b) [in_progress]
Next expected phase: (pending completion of current phase)
```

**After:**
```
Current phase   : Cluster Setup Wizard (1b) [in_progress]
  └─ Primary [10.192.160.29]      : wizard [awaiting_input]
  └─ Peer-01 [10.192.160.35]      : waiting for primary
Next expected phase: Cluster Setup Wizard (1b) → NTP Configuration (once wizard input received)
Est. time remaining: 10–15 min
⚠️  BOTTLENECK: Operator input needed at cluster wizard prompt (expected prompt: "Enter cluster name...")
```

---

## Testing Scenarios

1. **Successful 4b run:** Verify next-phase predictions match actual phase sequence
2. **Checkpoint resume:** Verify skip list correct and next phase matches
3. **Hung phase:** Verify "in_progress" phase shows expected next phase after completion
4. **Multi-node:** Verify per-node breakdown shows primary vs peer progress correctly
5. **Interactive prompts:** Verify operator-input phases highlight what's needed

---

## Files to Modify

- **AFX_reinit.py**
  - Add `_predict_next_phase()` function (~40 lines)
  - Update `_format_checkpoint_status()` to use new prediction logic (~10 lines)
  - Add phase sequences dict (~50 lines)
  - Add interactive phase metadata (~20 lines)

- **README.md**
  - Document new checkpoint status improvements
  - Show before/after example

- **CHANGELOG.md**
  - Note predictive phase logic addition
  - Note per-node phase breakdown enhancement

---

## Related Prior Work

- **Checkpoint improvements** (commit 3117de5): Added role labeling (primary/secondary-0N) and timing labels
- **Phase timing** (lines 15400–15450): Already tracks phase start/end times
- **Per-node tracking** (lines 466+): Already has per-node phase matrix infrastructure

This enhancement builds on existing checkpoint and phase-tracking infrastructure.
