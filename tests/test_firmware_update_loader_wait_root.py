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


if __name__ == "__main__":
    unittest.main()
