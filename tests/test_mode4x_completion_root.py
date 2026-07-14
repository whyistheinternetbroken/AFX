import unittest

import AFX_reinit


class _FakeCheckpoint:
    def __init__(self, *, mode, bmc_ips, phases=None, node_phases=None):
        self.mode = mode
        self.bmc_ips = list(bmc_ips)
        self._data = {
            "phases": phases or {},
            "node_phases": node_phases or {},
        }
        self._phases = self._data["phases"]
        self._node_phases = self._data["node_phases"]

    def is_done(self, phase):
        return bool(self._phases.get(phase, {}).get("done"))

    def nodes_done_for(self, phase):
        return [
            node_id
            for node_id, meta in self._node_phases.get(phase, {}).items()
            if meta.get("done")
        ]

    def _save(self):
        return None


def _done_nodes(*node_ids):
    return {node_id: {"done": True} for node_id in node_ids}


class Mode4xCompletionRootTests(unittest.TestCase):
    def test_42_final_phase_stays_pending_without_all_runtime_evidence(self):
        cp = _FakeCheckpoint(
            mode="4.2",
            bmc_ips=["10.0.0.11", "10.0.0.12"],
            phases={"cp_42_6": {"done": True}},
            node_phases={
                **{
                    f"cp_42_{idx}": _done_nodes("10.0.0.11", "10.0.0.12")
                    for idx in range(1, 6)
                },
                "install_done": _done_nodes("10.0.0.11"),
            },
        )

        self.assertFalse(
            AFX_reinit._checkpoint_phase_effectively_done(cp, 42, "cp_42_6")
        )
        self.assertEqual("cp_42_6", AFX_reinit._checkpoint_resume_phase_id(cp))

    def test_42_final_phase_uses_install_and_reinit_loader_runtime_evidence(self):
        cp = _FakeCheckpoint(
            mode="4.2",
            bmc_ips=["10.0.0.21", "10.0.0.22"],
            node_phases={
                **{
                    f"cp_42_{idx}": _done_nodes("10.0.0.21", "10.0.0.22")
                    for idx in range(1, 6)
                },
                "install_done": _done_nodes("10.0.0.21"),
                "reinit_loader": _done_nodes("10.0.0.22"),
            },
        )

        self.assertTrue(
            AFX_reinit._checkpoint_phase_effectively_done(cp, 42, "cp_42_6")
        )
        self.assertEqual("", AFX_reinit._checkpoint_resume_phase_id(cp))

    def test_42_final_phase_stays_pending_with_all_install_done_but_no_reinit_loader(self):
        cp = _FakeCheckpoint(
            mode="4.2",
            bmc_ips=["10.0.0.23", "10.0.0.24"],
            node_phases={
                **{
                    f"cp_42_{idx}": _done_nodes("10.0.0.23", "10.0.0.24")
                    for idx in range(1, 6)
                },
                "install_done": _done_nodes("10.0.0.23", "10.0.0.24"),
            },
        )

        self.assertFalse(
            AFX_reinit._checkpoint_phase_effectively_done(cp, 42, "cp_42_6")
        )
        self.assertEqual("cp_42_6", AFX_reinit._checkpoint_resume_phase_id(cp))

    def test_42_manual_override_to_final_phase_keeps_cp42_6_pending_until_reinit_transition(self):
        cp = _FakeCheckpoint(
            mode="4.2",
            bmc_ips=["10.0.0.25", "10.0.0.26"],
        )

        self.assertTrue(
            AFX_reinit._checkpoint_apply_manual_resume_target(cp, "cp_42_6")
        )
        self.assertEqual(
            ["10.0.0.25", "10.0.0.26"],
            AFX_reinit._checkpoint_4x_nodes_done_for_runtime_phase(cp, "install_done"),
        )
        self.assertEqual(
            [],
            AFX_reinit._checkpoint_4x_nodes_done_for_runtime_phase(cp, "reinit_loader"),
        )
        self.assertFalse(
            AFX_reinit._checkpoint_phase_effectively_done(cp, 42, "cp_42_6")
        )
        self.assertEqual("cp_42_6", AFX_reinit._checkpoint_resume_phase_id(cp))

    def test_43_final_phase_stays_pending_until_all_nodes_reach_option6_done(self):
        cp = _FakeCheckpoint(
            mode="4.3",
            bmc_ips=["10.0.0.31", "10.0.0.32"],
            phases={"cp_43_6": {"done": True}},
            node_phases={
                **{
                    f"cp_43_{idx}": _done_nodes("10.0.0.31", "10.0.0.32")
                    for idx in range(1, 6)
                },
                "option6_done": _done_nodes("10.0.0.31"),
            },
        )

        self.assertFalse(
            AFX_reinit._checkpoint_phase_effectively_done(cp, 43, "cp_43_6")
        )
        self.assertEqual("cp_43_6", AFX_reinit._checkpoint_resume_phase_id(cp))

    def test_43_final_phase_uses_option6_done_runtime_evidence_for_all_targets(self):
        cp = _FakeCheckpoint(
            mode="4.3",
            bmc_ips=["10.0.0.41", "10.0.0.42"],
            node_phases={
                **{
                    f"cp_43_{idx}": _done_nodes("10.0.0.41", "10.0.0.42")
                    for idx in range(1, 6)
                },
                "option6_done": _done_nodes("10.0.0.41", "10.0.0.42"),
            },
        )

        self.assertTrue(
            AFX_reinit._checkpoint_phase_effectively_done(cp, 43, "cp_43_6")
        )
        self.assertEqual("", AFX_reinit._checkpoint_resume_phase_id(cp))


if __name__ == "__main__":
    unittest.main()
