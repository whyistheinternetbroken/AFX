import unittest
from unittest import mock

import AFX_reinit


class PrimaryLoaderProbeRecheckRootTests(unittest.TestCase):
    def test_returns_true_when_second_probe_finds_loader(self):
        with mock.patch.object(
            AFX_reinit,
            "_already_at_loader",
            side_effect=[False, True],
        ) as probe_mock, mock.patch.object(AFX_reinit, "_ts_print"):
            ok = AFX_reinit._already_at_loader_with_recheck(
                object(),
                label="10.0.0.10",
            )

        self.assertTrue(ok)
        self.assertEqual(2, probe_mock.call_count)
        first_call = probe_mock.call_args_list[0]
        second_call = probe_mock.call_args_list[1]
        self.assertEqual(25, first_call.kwargs.get("probe_timeout"))
        self.assertEqual(45, second_call.kwargs.get("probe_timeout"))

    def test_skips_second_probe_when_first_probe_succeeds(self):
        with mock.patch.object(
            AFX_reinit,
            "_already_at_loader",
            return_value=True,
        ) as probe_mock:
            ok = AFX_reinit._already_at_loader_with_recheck(object())

        self.assertTrue(ok)
        self.assertEqual(1, probe_mock.call_count)


if __name__ == "__main__":
    unittest.main()
