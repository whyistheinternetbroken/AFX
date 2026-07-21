import io
import os
import tempfile
import unittest
from unittest import mock

import AFX_reinit


class BmcPeerConsoleRoutingRootTests(unittest.TestCase):
    def test_peer_console_output_stays_out_of_primary_log(self):
        with tempfile.TemporaryDirectory() as td:
            fake_script_path = os.path.join(td, "AFX_reinit.py")
            with open(fake_script_path, "w", encoding="utf-8") as fh:
                fh.write("# test helper\n")

            with mock.patch.object(AFX_reinit, "__file__", fake_script_path):
                with mock.patch("builtins.print"), mock.patch("sys.stdout", new=io.StringIO()):
                    logger = AFX_reinit.SessionLogger(bg_mode=True, label="test")
                    try:
                        peer_ip = "192.168.0.97"
                        peer_path = logger.open_peer_log(peer_ip)
                        logger.log_console("PRIMARY-CONTENT\n")
                        logger.log_console("PEER-CONTENT-A\n", source_label=f"peer/{peer_ip}")
                        logger.log_console(f"[peer/{peer_ip}] PEER-CONTENT-B\n")
                    finally:
                        logger.close()

            with open(logger.log_file, "r", encoding="utf-8") as primary_fh:
                primary_text = primary_fh.read()
            with open(peer_path, "r", encoding="utf-8") as peer_fh:
                peer_text = peer_fh.read()

            self.assertIn("PRIMARY-CONTENT", primary_text)
            self.assertNotIn("PEER-CONTENT-A", primary_text)
            self.assertNotIn("PEER-CONTENT-B", primary_text)
            self.assertIn("PEER-CONTENT-A", peer_text)
            self.assertIn("PEER-CONTENT-B", peer_text)


if __name__ == "__main__":
    unittest.main()
