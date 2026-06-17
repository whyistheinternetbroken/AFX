# Task Dependency & Execution DAG

## Task IDs and Waves

```
WAVE 1 (Parallel)
├─ hardening-failover-state      (T1) [Medium Effort]
└─ hardening-cluster-nodes        (T3) [Medium Effort]

WAVE 2 (Sequential, depends on Wave 1)
└─ hardening-cluster-healthy      (T2) [Large Effort]

WAVE 3 (Parallel, depends on Wave 2)
├─ integration-test-polling       (T4) [Medium Effort]
└─ documentation-polling-retry    (T5) [Small Effort]
```

---

## Detailed Dependency Graph

```
T1: hardening-failover-state
    ├─ Covers: Single-command polling with 3×60s retry
    ├─ Agent: gem-implementer
    ├─ Effort: Medium (50 lines, 1 file)
    ├─ No dependencies
    └─ Unblocks: T2 (pattern validation)

T3: hardening-cluster-nodes
    ├─ Covers: Long-polling (300s intervals) with 3×60s retry
    ├─ Agent: gem-implementer
    ├─ Effort: Medium (50 lines, 1 file)
    ├─ No dependencies
    └─ Unblocks: T2 (pattern validation)

T2: hardening-cluster-healthy          ◄── DEPENDS ON T1 + T3
    ├─ Covers: 3-command atomic block with 3×60s retry
    ├─ Agent: gem-implementer
    ├─ Effort: Large (60 lines, 1 file)
    ├─ Dependencies: [hardening-failover-state, hardening-cluster-nodes]
    └─ Unblocks: T4, T5

T4: integration-test-polling          ◄── DEPENDS ON T1 + T2 + T3
    ├─ Covers: Cross-function integration, socket mocking, timing validation
    ├─ Agent: gem-implementer
    ├─ Effort: Medium (150 lines, 1 file)
    ├─ Dependencies: [hardening-failover-state, hardening-cluster-nodes, hardening-cluster-healthy]
    └─ No unblocking (final validation)

T5: documentation-polling-retry       ◄── DEPENDS ON T1 + T2 + T3
    ├─ Covers: Docstrings, README updates, implementation notes
    ├─ Agent: gem-documentation-writer
    ├─ Effort: Small (50 lines, 2 files)
    ├─ Dependencies: [hardening-failover-state, hardening-cluster-nodes, hardening-cluster-healthy]
    └─ No unblocking (final documentation)
```

---

## Contract Matrix (Interfaces Between Tasks)

### Contract 1: T1 ↔ T3 (Shared Pattern Validation)
- **From**: T1 (hardening-failover-state)
- **To**: T3 (hardening-cluster-nodes)
- **Interface**: "Both use same 3×60s retry pattern"
- **Format**: 
  - Input: `channel`, socket-prone command call
  - Output: `bool` (success/timeout)
  - Behavior: Try → socket.error → 3 retries (60s apart) → return bool
- **Validation**: T1 unit tests pass; T3 applies same pattern; both use identical retry structure

### Contract 2: T1 → T2 (Simpler-to-Complex Progression)
- **From**: T1 + T3 (both validated)
- **To**: T2 (hardening-cluster-healthy)
- **Interface**: "Pattern extends from 1-command to 3-command atomic block"
- **Format**:
  - Input: T1/T3 validated 3×60s retry loop code
  - Output: T2 applies same loop structure but wraps 3-command block instead of 1
  - Behavior: All 3 commands run within try/except; failure triggers same 3×60s retry
- **Validation**: T2 unit tests verify each command triggers retry independently

### Contract 3: T1 + T2 + T3 → T4 (Integration Validation)
- **From**: T1, T2, T3 (all implementations complete)
- **To**: T4 (integration-test-polling)
- **Interface**: "All three functions return `bool`, support socket error retry"
- **Format**:
  - Input: Three hardened functions
  - Output: Integration test result (pass/fail)
  - Behavior: Call all three in sequence; inject socket errors; verify retry → success flow
- **Validation**: T4 confirms all three coexist, no deadlocks, log output is correct

### Contract 4: T1 + T2 + T3 → T5 (Source Material)
- **From**: T1, T2, T3 (implementation complete, function signatures known)
- **To**: T5 (documentation-polling-retry)
- **Interface**: "Functions and retry behavior documented for maintainers"
- **Format**:
  - Input: Function names, line numbers, retry structure, exception types
  - Output: Updated docstrings, README section, implementation notes
  - Behavior: Docstrings reference 3×60s retry, socket.error handling, `_safe_poll()` pattern
- **Validation**: T5 updates docstrings with concrete example; README mentions retry for all three

---

## Execution Timeline (Estimated)

```
Day 1:
  │
  ├─ [WAVE 1 START] 08:00
  │  ├─ T1 (gem-implementer): _wait_for_failover_state → 3×60s retry
  │  │   └─ Unit test: socket error → retry → success ✓  [3 hours]
  │  │
  │  ├─ T3 (gem-implementer): _wait_for_cluster_nodes → 3×60s retry
  │  │   └─ Unit test: socket error → retry → success ✓  [3 hours, parallel with T1]
  │  │
  │  └─ [WAVE 1 GATE] 14:00 — Both T1 and T3 pass unit tests
  │
  │
Day 2:
  │
  ├─ [WAVE 2 START] 09:00
  │  └─ T2 (gem-implementer): _wait_for_cluster_healthy → 3-command atomic retry
  │      ├─ Implement: Wrap failover show + giveback + port show block
  │      ├─ Unit test: Each command triggers retry independently
  │      └─ Final test: Atomic block retry works (all 3 commands re-run)  [6 hours]
  │
  └─ [WAVE 2 GATE] 15:00 — T2 passes all unit tests


Day 3:
  │
  ├─ [WAVE 3 START] 09:00
  │  │
  │  ├─ T4 (gem-implementer): Integration test
  │  │   ├─ Mock socket errors, verify retry flow across all three
  │  │   ├─ Validate log output, timing, no deadlocks
  │  │   └─ Test with upgrade flows (4a, 4b contexts)  [4 hours, parallel with T5]
  │  │
  │  └─ T5 (gem-documentation-writer): Documentation
  │      ├─ Update docstrings for all three functions
  │      ├─ Add comment block explaining pattern
  │      └─ Update README.md with retry behavior  [2 hours, parallel with T4]
  │
  └─ [WAVE 3 GATE] 13:00 — T4 passes integration test, T5 updates merged


[PLAN COMPLETE] 13:00 Day 3
```

**Total Duration**: ~16 hours of elapsed time (Wave 1: 3h, Wave 2: 6h, Wave 3: 4h)  
**Parallelism**: Wave 1 (T1 || T3), Wave 3 (T4 || T5)  
**Critical Path**: T1 (or T3) → T2 → T4

---

## Effort Breakdown

| Task | Effort | Hours | Files | Lines | Agent |
|------|--------|-------|-------|-------|-------|
| T1 | Medium | 3–4 | 1 | 50 | gem-implementer |
| T3 | Medium | 3–4 | 1 | 50 | gem-implementer |
| T2 | Large | 6–8 | 1 | 60 | gem-implementer |
| T4 | Medium | 4–6 | 1 | 150 | gem-implementer |
| T5 | Small | 1–2 | 2 | 50 | gem-documentation-writer |
| **Total** | — | **18–24** | **6** | **360** | — |

---

## Parallelism Opportunities

```
Wave 1: T1 and T3 are COMPLETELY INDEPENDENT
    ├─ No shared code changes
    ├─ No shared imports
    ├─ No race conditions
    └─ Can be assigned to same or different developers

Wave 3: T4 and T5 are INDEPENDENT
    ├─ T4 doesn't require updated docs
    ├─ T5 doesn't block testing
    └─ Can run in parallel with small coordination on docstring examples

Wave 2: T2 DEPENDS on T1 ✓ and T3 ✓
    ├─ Must wait for Wave 1 to complete
    ├─ Builds on validated pattern from T1/T3
    └─ Uses same try/except structure as reference
```

---

## Gate Criteria

### Wave 1 Gate (T1 + T3 must pass)
- [ ] T1: Unit tests pass for single-command retry
- [ ] T1: Socket error triggers retry loop (3 rounds, 60s each)
- [ ] T1: Function returns correct bool value
- [ ] T3: Unit tests pass for long-polling retry
- [ ] T3: Socket error triggers retry loop (3 rounds, 60s each)
- [ ] T3: Function returns correct bool value
- [ ] **Approval**: Release to T2

### Wave 2 Gate (T2 must pass)
- [ ] T2: Unit tests pass for 3-command atomic retry
- [ ] T2: Each command independently triggers retry
- [ ] T2: All 3 commands re-run atomically on retry
- [ ] T2: Validation logic still works (rejects unhealthy state)
- [ ] T2: Log output shows which command failed
- [ ] **Approval**: Release to Wave 3

### Wave 3 Gate (T4 + T5 must pass)
- [ ] T4: Integration test passes (all three functions, socket mocking)
- [ ] T4: No deadlocks, race conditions, or unexpected delays
- [ ] T4: Log output is clear and debugging-friendly
- [ ] T5: Docstrings updated for all three functions
- [ ] T5: README/help reflects retry behavior
- [ ] T5: Implementation notes for maintainers added
- [ ] **Approval**: Plan complete, ready for production

---

## Rollback Plan

If any task fails during execution:

1. **T1 or T3 fails** → Fix and re-test (no merge yet)
2. **T2 fails** → Revert T2, keep T1/T3; T2 → new attempt
3. **T4 fails** → Debug integration issue; re-run after T2 fix
4. **T5 fails** → Non-blocking; fix docs in separate PR

Each task is isolated to a single function, enabling targeted rollback without affecting others.

---

## Success Metrics

After execution:

✅ All three functions recover from socket closure via 3×60s retry  
✅ Upgrade flows (4a, 4b) inherit retry robustness automatically  
✅ Zero change to function signatures, return types, or caller behavior  
✅ Log output enables debugging of retry events  
✅ Maintainers understand retry pattern and socket error handling  
✅ Integration test confirms all three coexist without issues  

---

## Related Implementations

**Reference**: `_safe_poll()` at lines 17398–17464  
- **Context**: Failover takeover, option 4a upgrade flow
- **Pattern**: Try → exception → 3-round reconnect retry → fallback
- **Success**: Used in production 4a upgrades with LIF migrations
- **Reuse**: Same pattern, different functions

**Callers**:
- T1 called by: 4a upgrade, 4b netboot
- T2 called by: 4a post-takeover verification
- T3 called by: 4b netboot, cluster add-node

---

## Sign-Off

| Role | Status | Date |
|------|--------|------|
| Planner (PLAN) | ✅ Complete | 2024-01-20 |
| Approver (Management) | ⏳ Pending | — |
| Implementer (T1) | ⏳ Not Started | — |
| Implementer (T3) | ⏳ Not Started | — |
| Implementer (T2) | ⏳ Blocked (awaits T1/T3) | — |
| Implementer (T4) | ⏳ Blocked (awaits T2) | — |
| Documentarian (T5) | ⏳ Blocked (awaits T2) | — |
