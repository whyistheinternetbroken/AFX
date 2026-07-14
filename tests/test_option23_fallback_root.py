import unittest
from contextlib import ExitStack
from unittest import mock

import AFX_reinit


class _FakeCheckpoint:
    def __init__(self, *, node_phases=None, phases=None):
        self._node_phases = node_phases or {}
        self._phases = phases or {}

    def is_node_done(self, phase, ip):
        return bool(
            self._node_phases.get(phase, {})
            .get(ip, {})
            .get("done")
        )

    def is_done(self, phase):
        return bool(
            self._phases.get(phase, {})
            .get("done")
        )

    def nodes_done_for(self, phase):
        return [
            ip for ip, meta in self._node_phases.get(phase, {}).items()
            if meta.get("done")
        ]


class _FakeCheckpointManager:
    def __init__(self, *, joined=None, option4=None):
        self._joined = set(joined or [])
        self._option4 = set(option4 or [])
        self.log_dir = ""

    def load(self):
        return True

    def nodes_done_for(self, phase):
        if phase == "peer_joined":
            return sorted(self._joined)
        if phase == "peer_option4_done":
            return sorted(self._option4)
        return []

    def is_node_done(self, phase, ip):
        if phase == "peer_joined":
            return ip in self._joined
        if phase == "peer_option4_done":
            return ip in self._option4
        return False

    def clear(self):
        return None


class _FakeSessionLog:
    def __init__(self):
        self.messages = []
        self.log_dir = "."
        self.log_file = "fake-session.log"

    def log(self, message, prefix="INFO"):
        self.messages.append((prefix, message))

    def start_phase(self, *_args, **_kwargs):
        return None

    def end_phase(self):
        return None

    def set_outcome(self, *_args, **_kwargs):
        return None

    def record_completion(self, *_args, **_kwargs):
        return None

    def add_phase_subtiming(self, *_args, **_kwargs):
        return None


class Option23FallbackRootTests(unittest.TestCase):
    def test_cp27_only_marker_counts_as_joined(self):
        checkpoint = _FakeCheckpoint(
            node_phases={
                "cp_2_7": {
                    "10.0.0.11": {"done": True},
                },
            },
        )

        summary = AFX_reinit._classify_option23_cluster_shell_unavailable(
            [{"bmc": "10.0.0.11"}],
            checkpoint,
        )

        self.assertEqual(
            ["10.0.0.11"],
            [node["bmc"] for node in summary["already_joined"]],
        )
        self.assertEqual(
            "checkpoint:cp_2_7",
            summary["already_joined"][0]["resume_evidence"],
        )

    def test_canonical_mode3_cp26_marker_counts_as_staged(self):
        checkpoint = _FakeCheckpoint(
            node_phases={
                "cp_2_6": {
                    "node_peer:10.0.0.12": {"done": True},
                },
            },
        )

        summary = AFX_reinit._classify_option23_cluster_shell_unavailable(
            [{"bmc": "10.0.0.12"}],
            checkpoint,
        )

        self.assertEqual(
            ["10.0.0.12"],
            [node["bmc"] for node in summary["staged_without_cluster_ip"]],
        )
        self.assertEqual(
            "checkpoint:cp_2_6",
            summary["staged_without_cluster_ip"][0]["resume_evidence"],
        )

    def test_canonical_peer_joined_alias_counts_as_joined(self):
        checkpoint = _FakeCheckpoint(
            node_phases={
                "peer_joined": {
                    "node_peer:10.0.0.13": {"done": True},
                },
            },
        )

        summary = AFX_reinit._classify_option23_cluster_shell_unavailable(
            [{"bmc": "10.0.0.13"}],
            checkpoint,
        )

        self.assertEqual(
            ["10.0.0.13"],
            [node["bmc"] for node in summary["already_joined"]],
        )
        self.assertEqual(
            "checkpoint:peer_joined",
            summary["already_joined"][0]["resume_evidence"],
        )


class Option23ResumeFallbackRootFlowTests(unittest.TestCase):
    def _run_resume(self, *, manifest_nodes, checkpoint, fallback_summary):
        session_log = _FakeSessionLog()
        ordered_calls = []

        def _fake_make_session_log(_title):
            AFX_reinit._session_log = session_log

        def _fake_ordered_cluster_entries(cluster_ips_out, preferred_bmcs=None):
            ordered_calls.append((cluster_ips_out, preferred_bmcs))
            return [
                {
                    "bmc": bmc,
                    "cluster_ip": entry["cluster_ip"],
                    "node_name": entry.get("node_name", ""),
                }
                for bmc, entry in cluster_ips_out.items()
                if entry.get("cluster_ip")
            ]

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(AFX_reinit, "_make_session_log", side_effect=_fake_make_session_log))
            stack.enter_context(mock.patch.object(AFX_reinit, "_print_banner"))
            stack.enter_context(mock.patch.object(AFX_reinit, "_config_secondary_nodes", return_value=manifest_nodes))
            stack.enter_context(mock.patch.object(AFX_reinit, "_config_data", {
                "cluster": {
                    "clus_mgmt_address": "203.0.113.10",
                    "user": "admin",
                    "password": "clusterpw",
                },
            }))
            stack.enter_context(mock.patch.object(AFX_reinit, "_cluster_config", {}))
            stack.enter_context(mock.patch.object(AFX_reinit, "_cred_lookup", return_value="clusterpw"))
            stack.enter_context(mock.patch.object(AFX_reinit, "_prompt", return_value="y"))
            stack.enter_context(mock.patch("builtins.input", return_value=""))
            stack.enter_context(mock.patch("builtins.print"))
            stack.enter_context(mock.patch("getpass.getpass", return_value="bmcpw"))
            stack.enter_context(mock.patch("glob.glob", return_value=[]))
            stack.enter_context(mock.patch("os.path.isfile", return_value=False))
            stack.enter_context(mock.patch.object(AFX_reinit, "CheckpointManager", return_value=checkpoint))
            stack.enter_context(mock.patch.object(
                AFX_reinit,
                "_classify_option23_cluster_shell_unavailable",
                return_value=fallback_summary,
            ))
            stack.enter_context(mock.patch.object(
                AFX_reinit,
                "_ssh_connect_with_retry",
                side_effect=Exception("cluster ssh unavailable"),
            ))
            stack.enter_context(mock.patch.object(AFX_reinit, "_load_cluster_ip_manifest_entries", return_value=[]))
            ordered_mock = stack.enter_context(mock.patch.object(
                AFX_reinit,
                "_ordered_cluster_entries_for_add",
                side_effect=_fake_ordered_cluster_entries,
            ))
            add_thread_mock = stack.enter_context(mock.patch.object(AFX_reinit, "_add_peer_node_thread"))
            cluster_add_mock = stack.enter_context(mock.patch.object(AFX_reinit, "_cluster_add_nodes_bulk"))
            stack.enter_context(mock.patch.object(AFX_reinit, "_set_checkpoint_test_parallel_scope"))
            stack.enter_context(mock.patch.object(AFX_reinit, "_join_threads_with_deadline", return_value=True))
            stack.enter_context(mock.patch.object(AFX_reinit, "_raise_pending_checkpoint_failure"))
            stack.enter_context(mock.patch.object(AFX_reinit, "_update_node_add_manifest_node"))
            stack.enter_context(mock.patch.object(AFX_reinit, "_record_cluster_ip_manifest_entry"))
            stack.enter_context(mock.patch.object(AFX_reinit, "_recover_cluster_ip_from_checkpoint_log", return_value=""))
            try:
                result = AFX_reinit._run_2c_resume()
            except AFX_reinit._ReturnToMenu:
                result = "returned-to-menu"
        return result, ordered_mock, ordered_calls, add_thread_mock, cluster_add_mock, session_log

    def test_resume_uses_fallback_summary_as_single_source_of_truth(self):
        manifest_nodes = [
            {"bmc": "10.0.0.11", "node_mgmt_ip": "192.0.2.11"},
            {
                "bmc": "10.0.0.12",
                "node_mgmt_ip": "192.0.2.12",
                "cluster_ip": "198.51.100.12",
                "node_name": "node2",
            },
        ]
        checkpoint = _FakeCheckpointManager(
            joined={"10.0.0.11", "10.0.0.12"},
            option4=set(),
        )
        fallback_summary = {
            "already_joined": [
                {
                    "bmc": "10.0.0.11",
                    "node_mgmt_ip": "192.0.2.11",
                    "resume_evidence": "checkpoint:peer_joined",
                },
            ],
            "pending_cluster_add": [
                {
                    "bmc": "10.0.0.12",
                    "node_mgmt_ip": "192.0.2.12",
                    "cluster_ip": "198.51.100.12",
                    "node_name": "node2",
                    "resume_evidence": "manifest:cluster_ip",
                },
            ],
            "staged_without_cluster_ip": [],
            "pre_stage_or_unknown": [],
            "ambiguous": [],
            "all_joined_confirmed": False,
            "has_non_destructive_stage": True,
        }

        result, ordered_mock, ordered_calls, add_thread_mock, cluster_add_mock, _session_log = self._run_resume(
            manifest_nodes=manifest_nodes,
            checkpoint=checkpoint,
            fallback_summary=fallback_summary,
        )

        self.assertFalse(result)
        self.assertTrue(ordered_mock.called)
        self.assertEqual(1, len(ordered_calls))
        self.assertEqual({"10.0.0.12"}, set(ordered_calls[0][0].keys()))
        self.assertEqual("198.51.100.12", ordered_calls[0][0]["10.0.0.12"]["cluster_ip"])
        add_thread_mock.assert_not_called()
        cluster_add_mock.assert_not_called()

    def test_resume_hard_stops_for_pre_stage_or_unknown_nodes(self):
        manifest_nodes = [
            {
                "bmc": "10.0.0.21",
                "node_mgmt_ip": "192.0.2.21",
                "cluster_ip": "198.51.100.21",
            },
        ]
        checkpoint = _FakeCheckpointManager(
            joined=set(),
            option4={"10.0.0.21"},
        )
        fallback_summary = {
            "already_joined": [],
            "pending_cluster_add": [],
            "staged_without_cluster_ip": [],
            "pre_stage_or_unknown": [
                {
                    "bmc": "10.0.0.21",
                    "node_mgmt_ip": "192.0.2.21",
                    "resume_evidence": "none",
                },
            ],
            "ambiguous": [
                {
                    "bmc": "10.0.0.21",
                    "node_mgmt_ip": "192.0.2.21",
                    "resume_evidence": "none",
                },
            ],
            "all_joined_confirmed": False,
            "has_non_destructive_stage": False,
        }

        result, ordered_mock, _ordered_calls, add_thread_mock, cluster_add_mock, _session_log = self._run_resume(
            manifest_nodes=manifest_nodes,
            checkpoint=checkpoint,
            fallback_summary=fallback_summary,
        )

        self.assertFalse(result)
        ordered_mock.assert_not_called()
        add_thread_mock.assert_not_called()
        cluster_add_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
