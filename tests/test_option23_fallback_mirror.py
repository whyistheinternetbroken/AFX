import importlib.util
import inspect
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

import AFX_reinit


def _load_mirror_module():
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    mirror_path = repo_root / "AFX" / "AFX_reinit.py"
    spec = importlib.util.spec_from_file_location("afx_reinit_mirror", mirror_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Option23FallbackMirrorParityTests(unittest.TestCase):
    def test_mirror_ports_root_cluster_shell_unavailable_helper_unchanged(self):
        mirror = _load_mirror_module()

        self.assertTrue(
            hasattr(mirror, "_classify_option23_cluster_shell_unavailable")
        )
        self.assertEqual(
            inspect.getsource(AFX_reinit._classify_option23_cluster_shell_unavailable),
            inspect.getsource(mirror._classify_option23_cluster_shell_unavailable),
        )

    def test_mirror_checkpoint_manager_methods_match_root_source(self):
        mirror = _load_mirror_module()

        self.assertEqual(
            inspect.getsource(AFX_reinit.CheckpointManager._save),
            inspect.getsource(mirror.CheckpointManager._save),
        )
        self.assertEqual(
            inspect.getsource(AFX_reinit.CheckpointManager.mark_done),
            inspect.getsource(mirror.CheckpointManager.mark_done),
        )
        self.assertEqual(
            inspect.getsource(AFX_reinit.CheckpointManager.mark_node_done),
            inspect.getsource(mirror.CheckpointManager.mark_node_done),
        )

    def test_mirror_direct_mark_done_is_idempotent(self):
        mirror = _load_mirror_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            checkpoint_path = pathlib.Path(temp_dir) / "checkpoint.json"
            checkpoint = mirror.CheckpointManager(str(checkpoint_path))
            checkpoint.init_run("3", ["10.0.0.11"], ".", "config.yaml")

            with mock.patch.object(mirror, "_log_pending_checkpoint_test_target") as log_target, \
                 mock.patch.object(mirror, "_maybe_inject_checkpoint_failure") as inject_failure:
                checkpoint.mark_done("cp_test")
                checkpoint.mark_done("cp_test")

            payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["phases"]["cp_test"]["done"])
            self.assertEqual(1, log_target.call_count)
            self.assertEqual(1, inject_failure.call_count)

    def test_mirror_checkpoint_mark_phase_and_direct_mark_respect_barrier(self):
        mirror = _load_mirror_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            checkpoint_path = pathlib.Path(temp_dir) / "checkpoint.json"
            checkpoint = mirror.CheckpointManager(str(checkpoint_path))
            checkpoint.init_run("3", ["10.0.0.11"], ".", "config.yaml")
            mirror._checkpoint = checkpoint

            try:
                with mock.patch.object(mirror, "print"), mock.patch.object(mirror, "_slog"):
                    self.assertTrue(
                        mirror._checkpoint_mark_phase("cp_2_6", node_id="node_peer:10.0.0.11")
                    )
                    self.assertFalse(
                        mirror._checkpoint_mark_phase("cp_2_6", node_id="node_peer:10.0.0.11")
                    )

                with mock.patch.object(
                    mirror,
                    "_checkpoint_test_should_block_mode3_peer_checkpoint",
                    return_value=True,
                ) as block_checkpoint, \
                mock.patch.object(mirror, "_log_pending_checkpoint_test_target") as log_target, \
                mock.patch.object(mirror, "_maybe_inject_checkpoint_failure") as inject_failure, \
                mock.patch.object(mirror, "_slog") as slog:
                    checkpoint.mark_node_done("cp_2_7", "node_peer:10.0.0.11")

                payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
                self.assertNotIn("cp_2_7", payload.get("node_phases", {}))
                self.assertEqual(1, block_checkpoint.call_count)
                self.assertEqual(0, log_target.call_count)
                self.assertEqual(0, inject_failure.call_count)
                slog.assert_called()
            finally:
                mirror._checkpoint = None


if __name__ == "__main__":
    unittest.main()
