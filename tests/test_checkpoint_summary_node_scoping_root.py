import os
import tempfile
import unittest

import AFX_reinit


class CheckpointSummaryNodeScopingRootTests(unittest.TestCase):
    def test_mode3_summary_scopes_nodes_by_role_and_orders_primary_first(self):
        old_mode = AFX_reinit._operation_mode
        try:
            AFX_reinit._operation_mode = 3
            with tempfile.TemporaryDirectory() as td:
                cp_path = os.path.join(td, "cp.json")
                cp = AFX_reinit.CheckpointManager(path=cp_path)
                cp._data = {
                    "mode": "3",
                    "created": "2026-07-20T23:00:00",
                    "updated": "2026-07-20T23:10:00",
                    "bmc_ips": ["192.168.0.96", "192.168.0.97", "192.168.0.98", "192.168.0.99"],
                    "node_phases": {
                        "cp_1_1": {"node_primary": {"done": True}},
                        "cp_1_3": {"node_primary": {"done": True}},
                        "cp_1_4": {"node_primary": {"done": True}},
                        "cp_1_5": {},
                        "cp_1_6": {},
                        "cp_2_1": {
                            "node_peer:192.168.0.97": {"done": True},
                            "node_peer:192.168.0.98": {"done": True},
                            "node_peer:192.168.0.99": {"done": True},
                        },
                        "cp_2_2": {
                            "node_peer:192.168.0.97": {"done": True},
                            "node_peer:192.168.0.98": {"done": True},
                            "node_peer:192.168.0.99": {"done": True},
                        },
                        "cp_2_3": {
                            "node_peer:192.168.0.97": {"done": True},
                            "node_peer:192.168.0.98": {"done": True},
                            "node_peer:192.168.0.99": {"done": True},
                        },
                        "cp_2_4": {},
                        "cp_2_5": {},
                    },
                }

                summary = cp.summary()

            primary_idx = summary.index("[primary_node | 192.168.0.96]")
            sec97_idx = summary.index("[secondary_node | 192.168.0.97]")
            self.assertLess(primary_idx, sec97_idx)
            self.assertNotIn("[node | node_primary]", summary)

            self.assertIn("current : cp_1_5 - Option 4 completed", summary)
            self.assertIn("next    : cp_1_6 - AutoSupport confirmation answered", summary)
            self.assertIn("current : cp_2_4 - Option 4 completed", summary)
            self.assertIn("next    : cp_2_5 - Node management configured", summary)

            primary_block = summary.split("[primary_node | 192.168.0.96]")[1].split("\n  [", 1)[0]
            self.assertIn("pending : cp_1_", primary_block)
            self.assertNotIn("cp_2_", primary_block)

            sec_block = summary.split("[secondary_node | 192.168.0.97]")[1].split("\n  [", 1)[0]
            self.assertIn("pending : cp_2_", sec_block)
            self.assertNotIn("cp_1_", sec_block)
        finally:
            AFX_reinit._operation_mode = old_mode


if __name__ == "__main__":
    unittest.main()
