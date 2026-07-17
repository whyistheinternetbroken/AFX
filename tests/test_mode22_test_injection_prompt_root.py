import unittest
from unittest import mock

import AFX_reinit


class Mode22TestInjectionPromptRootTests(unittest.TestCase):
    def setUp(self):
        self._saved_auto_add = getattr(AFX_reinit, "_auto_add", False)
        self._saved_enabled = AFX_reinit._checkpoint_test_enabled
        self._saved_target = AFX_reinit._checkpoint_test_target
        self._saved_mode_label = AFX_reinit._checkpoint_test_mode_label
        self._saved_targets_by_node = dict(AFX_reinit._checkpoint_test_targets_by_node)
        AFX_reinit._clear_checkpoint_test_config()

    def tearDown(self):
        AFX_reinit._auto_add = self._saved_auto_add
        AFX_reinit._checkpoint_test_enabled = self._saved_enabled
        AFX_reinit._checkpoint_test_target = self._saved_target
        AFX_reinit._checkpoint_test_mode_label = self._saved_mode_label
        AFX_reinit._checkpoint_test_targets_by_node = dict(self._saved_targets_by_node)

    def test_mode22_skips_global_checkpoint_prompt_and_defers_to_per_node(self):
        AFX_reinit._auto_add = True
        with mock.patch.object(AFX_reinit, "_prompt", side_effect=AssertionError("should not prompt")), \
             mock.patch.object(AFX_reinit, "_print_banner"):
            AFX_reinit._configure_checkpoint_test_for_mode(2, True)

        self.assertTrue(AFX_reinit._checkpoint_test_enabled)
        self.assertEqual("", AFX_reinit._checkpoint_test_target)
        self.assertEqual("2", AFX_reinit._checkpoint_test_mode_label)
        self.assertEqual({}, AFX_reinit._checkpoint_test_targets_by_node)

    def test_mode22_per_node_upgrade_runs_without_global_target(self):
        AFX_reinit._auto_add = True
        AFX_reinit._checkpoint_test_enabled = True
        AFX_reinit._checkpoint_test_target = ""
        AFX_reinit._checkpoint_test_targets_by_node = {}

        with mock.patch.object(
            AFX_reinit,
            "_configure_per_node_checkpoint_injection",
            return_value=True,
        ) as configure_per_node, \
        mock.patch.object(AFX_reinit, "print"):
            AFX_reinit._maybe_offer_per_node_checkpoint_injection_upgrade(
                2,
                ["10.0.0.11", "10.0.0.12"],
            )

        configure_per_node.assert_called_once()


if __name__ == "__main__":
    unittest.main()
