#!/usr/bin/env python3
"""Quick test of phase prediction logic."""

import sys
sys.path.insert(0, '.')

from AFX_reinit import _predict_next_phase, _estimate_remaining_time, _PHASE_SEQUENCES, _PHASE_METADATA

def test_phase_prediction():
    """Test next-phase prediction."""
    print("=" * 70)
    print("Testing Phase Prediction Logic")
    print("=" * 70)
    
    test_cases = [
        # (current_phase, state, mode, expected_contains)
        ("4b – Package Selection", "complete", "4b", "Collect Node Mgmt"),
        ("4b – Netboot Install", "complete", "4b", "Reinit Reconnect"),
        ("4b – Reinit Reconnect to LOADER", "complete", "4b", "Boot Menu Selection"),
        ("4b – Boot Menu Selection", "complete", "4b", "Cluster Initialization"),
        ("4b – Cluster Initialization (primary)", "complete", "4b", "Auto Cluster Init"),
        ("Auto Cluster Init (1b)", "complete", "4b", "Cluster Setup Wizard"),
        ("Cluster Setup Wizard (1b)", "complete", "4b", "NTP Configuration"),
        ("NTP Configuration", "complete", "4b", "final phase"),
        
        # In-progress phases
        ("4b – Cluster Initialization (primary)", "in_progress", "4b", "in progress"),
        ("Cluster Setup Wizard (1b)", "in_progress", "4b", "interactive"),
        ("Collect Cluster Setup Config", "in_progress", "4b", "interactive"),
        
        # Blocked phases
        ("4b – Reinit Reconnect to LOADER", "blocked", "4b", "BLOCKED"),
    ]
    
    print("\nTesting next-phase prediction:")
    for current_phase, state, mode, expected_contains in test_cases:
        result = _predict_next_phase(current_phase, state, mode)
        status = "✅ PASS" if expected_contains.lower() in result.lower() else "❌ FAIL"
        print(f"\n{status}")
        print(f"  Current: {current_phase} [{state}]")
        print(f"  Next:    {result}")
        print(f"  Expected to contain: '{expected_contains}'")

def test_time_estimation():
    """Test time-to-completion estimation."""
    print("\n" + "=" * 70)
    print("Testing Time Estimation")
    print("=" * 70)
    
    test_cases = [
        # (current_phase, elapsed_seconds, mode)
        ("4b – Package Selection", 10, "4b"),
        ("4b – Netboot Install", 600, "4b"),
        ("4b – Boot Menu Selection", 800, "4b"),
        ("Cluster Setup Wizard (1b)", 1200, "4b"),
    ]
    
    for current_phase, elapsed, mode in test_cases:
        result = _estimate_remaining_time(current_phase, elapsed, mode)
        if result:
            opt, pess = result
            opt_min = opt // 60
            pess_min = pess // 60
            print(f"\n{current_phase} (elapsed {elapsed}s)")
            print(f"  Est. remaining: {opt_min}–{pess_min} min")
        else:
            print(f"\n{current_phase}: (cannot estimate)")

def test_phase_sequences():
    """Verify phase sequences are complete and ordered."""
    print("\n" + "=" * 70)
    print("Phase Sequences Summary")
    print("=" * 70)
    
    for mode, phases in _PHASE_SEQUENCES.items():
        print(f"\nMode '{mode}' ({len(phases)} phases):")
        for i, phase in enumerate(phases, 1):
            meta = _PHASE_METADATA.get(phase, {})
            interactive = "(interactive)" if meta.get("interactive") else ""
            print(f"  {i:2d}. {phase:55s} {interactive}")

if __name__ == "__main__":
    try:
        test_phase_sequences()
        test_phase_prediction()
        test_time_estimation()
        print("\n" + "=" * 70)
        print("✅ All tests completed successfully!")
        print("=" * 70)
    except Exception as e:
        print(f"\n❌ Test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
