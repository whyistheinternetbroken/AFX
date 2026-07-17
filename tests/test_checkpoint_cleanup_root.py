import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest import mock

import AFX_reinit


def _write_checkpoint(path, mode):
    payload = {
        "version": 1,
        "created": datetime.now().isoformat(),
        "updated": datetime.now().isoformat(),
        "mode": str(mode),
        "bmc_ips": [],
        "phases": {},
        "node_phases": {},
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)


class CheckpointCleanupRootTests(unittest.TestCase):
    def test_clear_backup_checkpoints_removes_archives_and_keeps_active(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            checkpoints_dir = os.path.join(temp_dir, "checkpoints")
            os.makedirs(checkpoints_dir, exist_ok=True)
            active_path = os.path.join(checkpoints_dir, "afx_checkpoint.json")
            backup_1 = os.path.join(checkpoints_dir, "afx_checkpoint_20260717T101500.json")
            backup_2 = os.path.join(checkpoints_dir, "afx_checkpoint_20260717T101700.json")
            _write_checkpoint(active_path, "1")
            _write_checkpoint(backup_1, "1")
            _write_checkpoint(backup_2, "2")
            fake_module_path = os.path.join(temp_dir, "AFX_reinit.py")
            with open(fake_module_path, "w", encoding="utf-8") as fh:
                fh.write("# test module path\n")

            with mock.patch.object(AFX_reinit, "__file__", fake_module_path):
                removed = AFX_reinit.CheckpointManager.clear_backup_checkpoints()

            self.assertEqual(2, removed)
            self.assertTrue(os.path.isfile(active_path))
            self.assertFalse(os.path.isfile(backup_1))
            self.assertFalse(os.path.isfile(backup_2))

    def test_clear_backup_checkpoints_honors_mode_filter(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            checkpoints_dir = os.path.join(temp_dir, "checkpoints")
            os.makedirs(checkpoints_dir, exist_ok=True)
            backup_mode1 = os.path.join(checkpoints_dir, "afx_checkpoint_20260717T111500.json")
            backup_mode2 = os.path.join(checkpoints_dir, "afx_checkpoint_20260717T111700.json")
            _write_checkpoint(backup_mode1, "1")
            _write_checkpoint(backup_mode2, "2")
            fake_module_path = os.path.join(temp_dir, "AFX_reinit.py")
            with open(fake_module_path, "w", encoding="utf-8") as fh:
                fh.write("# test module path\n")

            with mock.patch.object(AFX_reinit, "__file__", fake_module_path):
                removed = AFX_reinit.CheckpointManager.clear_backup_checkpoints(mode="1")

            self.assertEqual(1, removed)
            self.assertFalse(os.path.isfile(backup_mode1))
            self.assertTrue(os.path.isfile(backup_mode2))


if __name__ == "__main__":
    unittest.main()
