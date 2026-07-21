import threading
import unittest
from unittest import mock

import AFX_reinit


class _FakeChannel:
    def __init__(self, chunks):
        self._chunks = [str(c) for c in chunks]
        self.sent = []

    def recv_ready(self):
        return bool(self._chunks)

    def recv(self, _n):
        if not self._chunks:
            return b""
        return self._chunks.pop(0).encode("utf-8")

    def send(self, data):
        self.sent.append(data)


class FirmwareUpdateLoaderWaitRootTests(unittest.TestCase):
    def test_progress_pattern_detection(self):
        stream = ".+." * 80 + "-" * 120
        self.assertTrue(AFX_reinit._looks_like_firmware_update_progress(stream))

    def test_separator_only_is_not_progress_pattern(self):
        stream = "-" * 300
        self.assertFalse(AFX_reinit._looks_like_firmware_update_progress(stream))

    def test_wait_helper_intercepts_autoboot_after_firmware_complete(self):
        ch = _FakeChannel(
            [
                ".+." * 80,
                "Firmware update complete",
                "Starting AUTOBOOT press Ctrl-C to abort",
                "LOADER-A>",
            ]
        )
        old_shutdown = AFX_reinit._shutdown_event
        try:
            AFX_reinit._shutdown_event = threading.Event()
            with mock.patch.object(AFX_reinit, "_session_log", None), \
                 mock.patch("AFX_reinit.time.sleep", return_value=None):
                ok = AFX_reinit._wait_for_firmware_update_then_loader(
                    ch,
                    label="10.0.0.10",
                    poll_interval=1,
                    firmware_timeout=30,
                    reboot_timeout=30,
                    status_cb=lambda _msg: None,
                )
            self.assertTrue(ok)
            self.assertTrue(any(s == "\x03" for s in ch.sent))
        finally:
            AFX_reinit._shutdown_event = old_shutdown

    def test_battery_warning_is_acknowledged_with_c_enter(self):
        ch = _FakeChannel([])
        state = {}
        warning = (
            "WARNING: One or more batteries are experiencing a critical failure\n"
            "Status of batteries unknown\n"
            "To ignore this failure and boot the system in a mode\n"
            "where data loss might occur, press 'c' followed by 'Enter'\n"
        )
        sent = AFX_reinit._maybe_handle_battery_boot_warning(
            ch,
            warning,
            label="10.0.0.11",
            status_cb=lambda _msg: None,
            state=state,
        )
        self.assertTrue(sent)
        self.assertIn("c\r", ch.sent)
        # Re-processing same warning should not send again for the same state.
        sent_again = AFX_reinit._maybe_handle_battery_boot_warning(
            ch,
            warning,
            label="10.0.0.11",
            status_cb=lambda _msg: None,
            state=state,
        )
        self.assertFalse(sent_again)

    def test_nvram_caution_is_acknowledged_with_y_enter(self):
        ch = _FakeChannel([])
        state = {"battery_ack_sent": True}
        warning = (
            "CAUTION: Using this controller without NVRAM\n"
            "battery backup coupled with a power\n"
            "failure condition CAN CAUSE DATA LOSS.\n"
            "Are you sure you want to continue (y or n)?\n"
        )
        sent = AFX_reinit._maybe_handle_battery_boot_warning(
            ch,
            warning,
            label="10.0.0.12",
            status_cb=lambda _msg: None,
            state=state,
        )
        self.assertTrue(sent)
        self.assertIn("y\r", ch.sent)

    def test_option4_node_add_flow_handles_battery_warning_and_continues(self):
        ch = _FakeChannel([])
        battery_warning = (
            "WARNING: One or more batteries are experiencing a critical failure\n"
            "Status of batteries unknown\n"
            "To ignore this failure and boot the system in a mode\n"
            "where data loss might occur, press 'c' followed by 'Enter'\n"
        )
        nvram_warning = (
            "CAUTION: Using this controller without NVRAM\n"
            "battery backup coupled with a power\n"
            "failure condition CAN CAUSE DATA LOSS.\n"
            "Are you sure you want to continue (y or n)?\n"
        )
        read_results = iter([
            ("", None),  # pre-probe
            (
                "this will erase all the data on the disks",
                "this will erase all the data on the disks",
            ),  # zero-disks stage sees erase prompt first
            (battery_warning, None),  # type-yes wait sees battery warning
            (nvram_warning, None),    # then sees NVRAM caution
            (
                "type yes to confirm and continue",
                "type yes to confirm and continue",
            ),  # finally gets confirmation prompt
        ])

        old_shutdown = AFX_reinit._shutdown_event
        old_checkpoint = AFX_reinit._checkpoint
        old_enable_asup = AFX_reinit._enable_autosupport
        old_session_log = AFX_reinit._session_log
        try:
            AFX_reinit._shutdown_event = threading.Event()
            AFX_reinit._checkpoint = None
            AFX_reinit._enable_autosupport = True
            AFX_reinit._session_log = None
            with mock.patch.object(
                AFX_reinit,
                "direct_read_until_any",
                side_effect=lambda *_a, **_k: next(read_results),
            ), mock.patch("AFX_reinit.time.sleep", return_value=None), \
                 mock.patch("builtins.print"), \
                 mock.patch.object(AFX_reinit, "_ts_print"):
                AFX_reinit._auto_answer_disk_erase_prompts(
                    ch,
                    node_log=None,
                    label="192.168.0.97",
                    is_node_add=True,
                    reconnect_ctx=None,
                )
        finally:
            AFX_reinit._shutdown_event = old_shutdown
            AFX_reinit._checkpoint = old_checkpoint
            AFX_reinit._enable_autosupport = old_enable_asup
            AFX_reinit._session_log = old_session_log

        self.assertIn("c\r", ch.sent)
        self.assertIn("y\r", ch.sent)
        self.assertGreaterEqual(ch.sent.count("yes\r"), 2)

    def test_option4_node_add_flow_handles_type_yes_before_zero_disks(self):
        ch = _FakeChannel([])
        read_results = iter([
            ("", None),  # pre-probe
            (
                "Type yes to confirm and continue {yes}:",
                "type yes to confirm and continue",
            ),  # zero-disks stage sees type-yes early
            (
                "Welcome to the cluster setup wizard.",
                "welcome to the cluster setup wizard",
            ),  # type-yes stage fast-advances
        ])

        old_shutdown = AFX_reinit._shutdown_event
        old_checkpoint = AFX_reinit._checkpoint
        old_enable_asup = AFX_reinit._enable_autosupport
        old_session_log = AFX_reinit._session_log
        try:
            AFX_reinit._shutdown_event = threading.Event()
            AFX_reinit._checkpoint = None
            AFX_reinit._enable_autosupport = True
            AFX_reinit._session_log = None
            with mock.patch.object(
                AFX_reinit,
                "direct_read_until_any",
                side_effect=lambda *_a, **_k: next(read_results),
            ), mock.patch("AFX_reinit.time.sleep", return_value=None), \
                 mock.patch("builtins.print"), \
                 mock.patch.object(AFX_reinit, "_ts_print"):
                AFX_reinit._auto_answer_disk_erase_prompts(
                    ch,
                    node_log=None,
                    label="192.168.0.97",
                    is_node_add=True,
                    reconnect_ctx=None,
                )
        finally:
            AFX_reinit._shutdown_event = old_shutdown
            AFX_reinit._checkpoint = old_checkpoint
            AFX_reinit._enable_autosupport = old_enable_asup
            AFX_reinit._session_log = old_session_log

        self.assertIn("yes\r", ch.sent)


if __name__ == "__main__":
    unittest.main()
