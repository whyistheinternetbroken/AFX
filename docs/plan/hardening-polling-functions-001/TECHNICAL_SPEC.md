# Technical Specification: Polling Function Retry Implementation

**Version**: 1.0  
**Date**: 2024-01-20  
**Status**: READY FOR IMPLEMENTATION  
**Reference Pattern**: `_safe_poll()` (lines 17398–17464)

---

## Objective

Retrofit three critical polling functions with a **socket-aware reconnect-retry loop** that handles transient network failures during cluster LIF migrations. When a socket closes unexpectedly, the polling function will:

1. **Detect** the socket error (socket.error, socket.timeout, or generic Exception)
2. **Retry** up to 3 times, waiting 60s between attempts
3. **Reconnect** the SSH channel before each retry
4. **Fall back** to existing timeout behavior if all retries exhausted
5. **Log** each retry attempt with round number and exception details

---

## Functions Targeted

### T1: `_wait_for_failover_state()` (Line 15904)

**Current Implementation** (simplified):
```python
def _wait_for_failover_state(channel, node, target_substr, total_timeout=1800,
                             poll_interval=60, log=None, phase_label=None,
                             exclude_substrs=None, also_accept=None):
    """Poll 'storage failover show' until target_substr found."""
    start = time.monotonic()
    while True:
        elapsed = time.monotonic() - start
        if elapsed >= total_timeout:
            break
        remaining = total_timeout - elapsed
        
        # SINGLE COMMAND
        with _suppress_console():
            out = _run_cluster_command(
                channel,
                "set advanced -c off; storage failover show -fields node,state-description",
                timeout=30,
            )
        
        matched_state = _parse_failover_show_node(out, node)
        if matched_state and target_substr.lower() in matched_state.lower():
            return True
        
        print(f"  ⏳ Waiting for {target_substr}... (elapsed {int(elapsed)}s)")
        time.sleep(min(poll_interval, max(1, remaining)))
    
    if log:
        log.log(f"Timeout waiting for {target_substr}", prefix="WARN")
    return False
```

**Changes**: Wrap polling command in try/except for socket error retry.

---

### T2: `_wait_for_cluster_healthy()` (Line 15990)

**Current Implementation** (simplified):
```python
def _wait_for_cluster_healthy(channel, expected_nodes, total_timeout=1800,
                              poll_interval=60, log=None):
    """Poll until ALL expected nodes are healthy (3-check validation)."""
    start = time.monotonic()
    while True:
        elapsed = time.monotonic() - start
        if elapsed >= total_timeout:
            break
        remaining = total_timeout - elapsed

        with _suppress_console():
            # CHECK 1: Failover show
            out_fo = _run_cluster_command(
                channel, "set -rows 0; storage failover show", timeout=30)
            # CHECK 2: Giveback show
            out_gb = _run_cluster_command(
                channel, "set diag -c off; storage failover show-giveback", timeout=30)

        # Parse and validate all 3 checks
        fo_rows = _parse_failover_show(out_fo)
        # ... validate failover state ...
        # ... validate giveback status ...
        # CHECK 3: Port health (network port show -ipspace Cluster)
        port_issues = _cluster_port_health_issues(channel, expected_nodes, log=log)
        
        if not_clean:  # Any check failed?
            print(f"  ⏳ Waiting for cluster health... (elapsed {int(elapsed)}s)")
            time.sleep(min(poll_interval, max(1, remaining)))
        else:
            return True
    
    if log:
        log.log("Timeout waiting for cluster health", prefix="WARN")
    return False
```

**Changes**: Wrap **entire 3-command block** in try/except for atomic retry.

---

### T3: `_wait_for_cluster_nodes_healthy()` (Line 19712)

**Current Implementation** (simplified):
```python
def _wait_for_cluster_nodes_healthy(channel, target_count, total_timeout=900,
                                    poll_interval=300, label="", final_count=None):
    """Poll 'cluster show' until target_count nodes healthy."""
    if channel is None:
        return True  # Skip if no primary channel
    
    start = time.monotonic()
    while True:
        if _shutdown_event.is_set():
            return False
        
        try:
            count, all_true, has_warning = _cluster_show_node_status(channel)
        except Exception as e:
            if _session_log:
                _session_log.log(f"cluster show error: {e}", prefix="WARN")
            count, all_true, has_warning = -1, False, False
        
        # Also check port health
        _port_issues = _cluster_port_health_issues(channel, log=_session_log)
        
        elapsed = time.monotonic() - start
        if elapsed >= total_timeout:
            return False
        
        if count == target_count and all_true and not _port_issues:
            return True
        
        remaining = total_timeout - elapsed
        print(f"  ⏳ Waiting for {target_count} cluster nodes... "
              f"(elapsed {int(elapsed)}s / remaining {int(remaining)}s)")
        time.sleep(min(poll_interval, max(1, remaining)))
    
    return False
```

**Changes**: Wrap polling + port health check in try/except for socket error retry.

---

## Implementation Pattern

### Core Retry Loop (from `_safe_poll()`)

```python
def _wait_for_failover_state(channel, node, target_substr, total_timeout=1800,
                             poll_interval=60, log=None, phase_label=None,
                             exclude_substrs=None, also_accept=None):
    """..."""
    import time as _time
    label = phase_label or f"failover state '{target_substr}'"
    start = _time.monotonic()
    
    while True:
        elapsed = _time.monotonic() - start
        if elapsed >= total_timeout:
            break
        remaining = total_timeout - elapsed

        # ────────────────────────────────────────────────────────────
        # NEW: Wrap polling in try/except for socket error retry
        # ────────────────────────────────────────────────────────────
        for _attempt in range(2):  # 1 normal attempt + 1 retry opportunity
            try:
                with _suppress_console():
                    out = _run_cluster_command(
                        channel,
                        "set advanced -c off; storage failover show -fields node,state-description",
                        timeout=30,
                    )
                
                # Parse and validate
                matched_state = _parse_failover_show_node(out, node)
                if matched_state:
                    state_lower = matched_state.lower()
                    exclude_lower = [e.lower() for e in (exclude_substrs or [])]
                    if (target_substr.lower() in state_lower and 
                        not any(e in state_lower for e in exclude_lower)):
                        if log:
                            log.log(f"Failover state for {node}: {matched_state}")
                        return True
                
                # Success (no exception), but state not yet match — continue polling
                break
            
            except Exception as _e:
                if _attempt == 0:
                    # First failure — attempt 3-round reconnect retry
                    print(f"  ⚠️  Failover check error ({_e}); "
                          f"waiting for cluster LIF migration...")
                    if log:
                        log.log(f"Failover check failed: {_e}; "
                                "attempting reconnect retry", prefix="WARN")
                    
                    _reconnected = False
                    for _r in range(3):
                        if _r > 0:
                            print(f"  ⏳ Waiting 60s for LIF migration "
                                  f"(round {_r + 1}/3)...")
                            if log:
                                log.log(f"Retry round {_r + 1}/3; "
                                        f"waiting 60s for cluster LIF",
                                        prefix="WARN")
                            _time.sleep(60)
                        
                        # Attempt to reconnect
                        try:
                            with _suppress_console():
                                drain_channel(channel, seconds=0.1)
                            # Reconnect by re-opening channel
                            # (Implementation detail: channel is paramiko SSHClientSocket)
                            # If channel is dead, _run_cluster_command will raise on next use
                            _reconnected = True
                            break
                        except Exception as _rc_err:
                            if log:
                                log.log(f"Reconnect attempt {_r + 1} failed: {_rc_err}",
                                        prefix="WARN")
                            _reconnected = False
                    
                    if not _reconnected:
                        if log:
                            log.log("Failover check reconnect exhausted after 3 rounds",
                                    prefix="ERROR")
                        # Fall through to main loop timeout (don't raise)
                        break  # Exit retry loop, continue main polling loop
                else:
                    # Second failure (after reconnect retry exhausted) — unrecoverable
                    if log:
                        log.log(f"Failover check unrecoverable after retry: {_e}",
                                prefix="ERROR")
                    break  # Exit retry loop, continue main polling loop
        
        # Print progress (existing behavior)
        print(f"  ⏳ Waiting for {label} on {node}  "
              f"(elapsed {int(elapsed)}s / remaining {int(remaining)}s)")
        _time.sleep(min(poll_interval, max(1, remaining)))
    
    if log:
        log.log(f"Timeout waiting for failover state '{target_substr}' on {node}",
                prefix="WARN")
    return False
```

### Key Implementation Notes

1. **Outer loop**: Main polling loop (existing, unchanged)
   - Runs until `total_timeout` expires
   - Sleeps `poll_interval` between checks
   - Returns `True` on success, `False` on timeout

2. **Inner loop (NEW)**: Retry loop (new, 3×60s recovery window)
   - `for _attempt in range(2)`: First attempt + one retry opportunity
   - First attempt: Try polling command
   - On exception: Enter 3-round reconnect retry
   - If all 3 rounds exhaust: Fall back to main polling loop

3. **Exception handling**:
   - Catch **broad Exception** (covers socket.error, socket.timeout, paramiko exceptions)
   - Log exception with round number
   - Print user-friendly message

4. **Reconnect strategy**:
   - `drain_channel()`: Clear stale buffers
   - On next `_run_cluster_command()`: Will fail and retry if channel dead
   - After 3×60s retries: Give up and let main loop continue

5. **Timeout behavior**:
   - Retry happens **within main polling timeout**
   - If total_timeout=1800s and retries take 180s: 1620s remaining for normal polling
   - No change to timeout semantics

---

## Exception Handling

### Catchable Exceptions

1. **socket.error**: Network error (connection reset, timeout, etc.)
2. **socket.timeout**: Explicit timeout
3. **paramiko.ssh_exception.SSHException**: SSH channel closed, auth failed
4. **EOFError**: Remote connection closed
5. **Generic Exception**: Fallback for unknown transport errors

### Recommendation

```python
except Exception as _e:  # Catch all (safest)
    if _attempt == 0:
        # Enter 3-round retry
    else:
        # Unrecoverable
```

This is simpler than checking exception type, and matches `_safe_poll()` pattern.

---

## Logging Strategy

### Console (print) Messages

- **On first failure**: `⚠️  Failover check error (socket.error); waiting for cluster LIF migration...`
- **On each retry**: `⏳ Waiting 60s for LIF migration (round 2/3)...`
- **On all retries exhausted**: (Silent, main polling loop continues)

### Log File (_session_log) Messages

- **On first failure**: `"Failover check failed: socket.error; attempting reconnect retry"` (WARN)
- **On each retry**: `"Retry round 2/3; waiting 60s for cluster LIF"` (WARN)
- **On all retries exhausted**: `"Failover check reconnect exhausted after 3 rounds"` (ERROR)
- **On second failure (after retry)**: `"Failover check unrecoverable after retry: socket.error"` (ERROR)

### Example Log Output

```
2024-01-20 14:35:42 [WARN] Failover check failed: socket.error - Cluster LIF migrating; attempting reconnect retry
2024-01-20 14:36:42 [WARN] Retry round 2/3; waiting 60s for cluster LIF
2024-01-20 14:37:42 [WARN] Retry round 3/3; waiting 60s for cluster LIF
2024-01-20 14:38:43 [WARN] Failover state for rtp-afx1k-c01-01: In Takeover
```

---

## Testing Strategy

### Unit Test (T1, T3, T2)

```python
def test_wait_for_failover_state_socket_error_retry():
    """Verify retry loop on socket error."""
    # Setup: Mock channel that raises socket.error on first call
    mock_channel = MagicMock()
    call_count = [0]
    
    def side_effect(*args, **kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            raise socket.error("Connection reset")
        else:
            # Retry succeeds
            return "rtp-afx1k-c01-01  In Takeover"
    
    mock_channel.recv = MagicMock(side_effect=side_effect)
    
    # Patch _run_cluster_command to inject error
    with patch('_run_cluster_command', side_effect=side_effect):
        result = _wait_for_failover_state(
            mock_channel, 'rtp-afx1k-c01-01', 'In Takeover',
            total_timeout=300, poll_interval=60
        )
    
    # Assert: function returned success (retry worked)
    assert result == True
    # Assert: retry loop was entered (call_count > 1)
    assert call_count[0] > 1
```

### Integration Test (T4)

```python
def test_all_three_polling_functions_with_socket_retry():
    """Verify all three functions handle socket errors."""
    # Setup mock cluster environment
    mock_channel = create_mock_channel()
    
    # Scenario 1: Normal case (no socket error)
    result1 = _wait_for_failover_state(mock_channel, 'node1', 'Connected')
    assert result1 == True
    
    # Scenario 2: Socket error on first poll, retry succeeds
    mock_channel.recv = MagicMock(side_effect=[
        socket.error("Connection reset"),
        "node1  Connected to node2",
    ])
    result2 = _wait_for_failover_state(mock_channel, 'node1', 'Connected')
    assert result2 == True
    
    # Scenario 3: All 3 retries fail
    mock_channel.recv = MagicMock(side_effect=socket.error("Persistent error"))
    result3 = _wait_for_failover_state(mock_channel, 'node1', 'Connected',
                                       total_timeout=10, poll_interval=1)
    assert result3 == False
```

---

## Risk Mitigation

### Risk 1: Socket Error Not Caught
**Mitigation**: Unit test with real socket.error injection; verify exception type.

### Risk 2: Retry Loop Deadlock
**Mitigation**: Fixed 3-round loop; 60s sleep is deterministic; main timeout still applies.

### Risk 3: Stale Connection Prevents Migration
**Mitigation**: Ensure `drain_channel()` clears buffers; _run_cluster_command timeout (30s) will eventually fail and trigger retry.

### Risk 4: Excessive Logging
**Mitigation**: Retry logged at WARN level (expected); only on socket error. Normal case: no retry logs.

---

## Rollout Strategy

1. **Test in lab**: Mock socket errors; verify 3-round retry works
2. **Stage 1 (4a upgrade)**: Deploy T1 (failover state) with monitoring
3. **Stage 2 (4a + T2)**: Deploy T2 (cluster health) with post-takeover verification
4. **Stage 3 (4b)**: Deploy T3 (cluster nodes) with add-node flows
5. **Monitor**: Log retry events; if <1% of polls trigger retry, pattern is working

---

## Acceptance Criteria

- [ ] T1: `_wait_for_failover_state()` accepts socket.error, enters 3-round retry
- [ ] T2: `_wait_for_cluster_healthy()` retries 3-command block atomically
- [ ] T3: `_wait_for_cluster_nodes_healthy()` retries cluster show + validation
- [ ] All three: Retry logs show round number; user sees console progress
- [ ] All three: Return bool unchanged; caller behavior transparent
- [ ] Integration test: All three coexist; no deadlocks, race conditions
- [ ] Documentation: Docstrings, README updated; maintainers understand pattern

---

## Future Enhancements (Out of Scope)

- Extract reusable `_retry_with_reconnect()` helper if duplicated >2× across codebase
- Make retry counts configurable (environment variable)
- Add observability metrics (retry rate, success rate per round)
- Async support (current pattern is synchronous blocking)
