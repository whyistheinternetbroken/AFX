# Polling Function Hardening Plan — Summary

**Plan ID**: `hardening-polling-functions-001`  
**Status**: `pending` (ready for execution)  
**Created**: 2024-01-20  
**Risk Level**: **MEDIUM**

---

## Overview

This plan hardens **3 critical polling functions** with a proven reconnect-retry pattern (3 rounds × 60s) used successfully in option 4a's `_safe_poll()` implementation. When socket closures occur during LIF migrations, the functions will attempt 3 reconnect retries spaced 60s apart before falling back to timeout behavior.

**Impact**: Extends recovery window from single timeout to **up to 180s of retry overhead + existing timeout**, gracefully recovering from transient network failures during cluster operations.

---

## Functions Being Hardened

| Function | Location | Used In | Change |
|----------|----------|---------|--------|
| `_wait_for_failover_state()` | Line 15904 | 4a upgrade, 4b netboot | Wrap polling loop + 3×60s retry on socket error |
| `_wait_for_cluster_healthy()` | Line 15990 | 4a post-takeover | Wrap 3-command block + 3×60s retry on socket error |
| `_wait_for_cluster_nodes_healthy()` | Line 19712 | 4b netboot, add-node | Wrap cluster-show + 3×60s retry on socket error |

---

## Reference Implementation

The retry pattern is established in **`_safe_poll()`** (lines 17398–17464):

```python
for _attempt in range(2):  # 1st attempt + 1 retry opportunity
    try:
        # Polling logic here
        return success
    except Exception as _e:
        if _attempt == 0:  # First failure → try 3-round reconnect retry
            _reconnected = False
            for _r in range(3):
                if _r > 0:
                    print(f"  ⏳ Waiting 60s for migration... (round {_r + 1}/3)")
                    if log:
                        log.log(f"Retry round {_r + 1}/3; waiting 60s", prefix="WARN")
                    time.sleep(60)
                if _open_poll_channel():  # Reconnect attempt
                    _reconnected = True
                    break
            if not _reconnected:
                if log:
                    log.log("Reconnect failed after 3 rounds", prefix="ERROR")
        else:  # Second failure → unrecoverable
            if log:
                log.log(f"Poll unrecoverable: {_e}", prefix="ERROR")
return None, ""  # Fallback
```

**Key traits**:
- **Socket-aware**: Catches `socket.error`, `socket.timeout`, and general exceptions
- **Bounded retries**: Exactly 3 rounds of reconnect, 60s apart
- **Logged**: Each attempt is logged with round number
- **User-friendly**: Console messages show retry progress
- **Graceful fallback**: Returns to caller if all retries exhausted

---

## Execution Plan (3 Waves)

### WAVE 1 (Parallel): Harden Simple Functions
- **T1**: `hardening-failover-state` — Single-command retry wrapper
- **T3**: `hardening-cluster-nodes` — Cluster-show + validation retry wrapper

**Effort**: Medium each | **Duration**: ~4–6 hours parallel  
**Gate**: Both must pass unit tests before Wave 2

### WAVE 2 (Sequential): Harden Complex Function
- **T2**: `hardening-cluster-healthy` — Triple-command atomic retry wrapper

**Effort**: Large | **Duration**: ~6–8 hours  
**Gate**: Must build on T1/T3 pattern validation; handles 3-command atomic block  
**Complexity**: Highest (3 sequential commands = larger retry scope)

### WAVE 3 (Parallel): Validate & Document
- **T4**: `integration-test-polling` — Cross-function integration test
- **T5**: `documentation-polling-retry` — Docstrings + README updates

**Effort**: Medium + Small | **Duration**: ~4–6 hours  
**Gate**: All prior tasks must pass

---

## Key Decisions (Already Locked)

✅ **Integration approach**: Inline retry logic (no separate helper function initially)  
✅ **Retry config**: 3 rounds × 60s for ALL three functions  
✅ **Atomic block for T2**: Retry all 3 commands together (simplest, safest)  
✅ **Fallback behavior**: Return False on timeout (no change to existing behavior)  
✅ **Logging**: Use existing `log` parameter and `_session_log` infrastructure  

---

## Risk Analysis

### Medium-Risk Failure Modes

1. **Socket closure detection fails** (likelihood: MEDIUM, impact: HIGH)
   - *If*: `socket.error` not raised during connection drop
   - *Then*: Polling hangs indefinitely instead of retrying
   - *Mitigation*: Unit test with real socket closure; verify exception type

2. **Stale connection prevents migration** (likelihood: LOW, impact: HIGH)
   - *If*: Reconnect doesn't fully close old socket, blocking LIF migration
   - *Then*: Cluster gets stuck; 60s wait insufficient
   - *Mitigation*: Ensure `_open_cluster_channel()` properly closes old sockets

3. **State inconsistency in T2** (likelihood: LOW, impact: MEDIUM)
   - *If*: Cluster state changes between command 1 and command 3
   - *Then*: Retry may pass validation but cluster is already degraded
   - *Mitigation*: Document that retry is atomic from caller view; caller handles post-check failures

4. **Merge conflicts in Wave 1** (likelihood: LOW, impact: MEDIUM)
   - *If*: T1 and T3 modify same utility function
   - *Then*: Merge conflict during parallel execution
   - *Mitigation*: T1/T3 are isolated; T2 depends on Wave 1 passing

### Assumptions (Must Hold)

- Three functions are in same file and can be edited independently
- Socket closure is primary failure mode (proven by `_safe_poll()` context)
- 3×60s retry window is acceptable per business decision
- Caller tolerates +180s latency before timeout
- `_open_cluster_channel()` properly releases socket resources
- Log infrastructure is available in all three functions

---

## Deliverables

**Location**: `c:\Users\parisi\git_workspace\AFX-reinit\docs\plan\hardening-polling-functions-001\`

- `plan.yaml` — Detailed DAG-based execution plan (30KB, 5 tasks, 3 waves)
- `PLAN_SUMMARY.md` — This document

---

## Next Steps

1. **Approve** the plan and risk analysis
2. **Assign** gem-implementer to T1 and T3 (Wave 1, parallel)
3. **Monitor** Wave 1 completion; unblock T2 when T1/T3 pass unit tests
4. **Execute** T2 (Wave 2, sequential)
5. **Validate** T4 (Wave 3, integration test)
6. **Publish** T5 (Wave 3, documentation)

**Estimated Total Duration**: 12–18 hours across 3 waves  
**Critical Path**: T1 or T3 → T2 → T4

---

## Questions & Clarifications

Q: *Should T2 retry per-command or atomic block?*  
A: **Atomic block** (simpler, matches `_safe_poll()` pattern). Callers expect "cluster is healthy" verdict, not per-command granularity.

Q: *Do we need a reusable helper function?*  
A: **Not initially**. Inline for clarity; refactor to `_retry_with_reconnect()` if duplicated >2× across codebase.

Q: *What happens if all 3 retries fail?*  
A: **Fall back to existing timeout**. Function returns `False` (same as before). Caller sees no difference except +180s of retry attempts were made (logged).

---

## Success Criteria

✅ All 3 functions accept socket errors and attempt 3-round reconnect retry  
✅ Each retry waits exactly 60s (except first attempt)  
✅ Reconnect() is attempted once per retry round  
✅ Log entries show round number for debugging  
✅ User console messages inform operator of retry progress  
✅ Return value (True/False) unchanged; caller behavior transparent  
✅ Integration test confirms all three coexist without conflicts  
✅ Documentation explains retry behavior for future maintainers  

---

**Plan Status**: ✅ Ready for Execution  
**Confidence**: HIGH (pattern proven in `_safe_poll()`)  
**Risk**: MEDIUM (outlined in pre-mortem; mitigations defined)
