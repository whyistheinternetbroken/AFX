import unittest
from unittest import mock

import AFX_reinit


class _DummyClient:
    def close(self):
        return None


class _FakeLog:
    def __init__(self):
        self.entries = []

    def log(self, message, prefix="INFO"):
        self.entries.append((prefix, str(message)))

    def start_phase(self, *_args, **_kwargs):
        raise AssertionError("start_phase should not be called when cluster shell is unavailable")


class Mode22ClusterShellRequiredRootTests(unittest.TestCase):
    def test_mode22_aborts_before_peer_workflow_when_cluster_shell_unavailable(self):
        log = _FakeLog()
        peer = "10.0.0.11"

        old_cluster_config = dict(getattr(AFX_reinit, "_cluster_config", {}) or {})
        old_config_data = getattr(AFX_reinit, "_config_data", {})
        old_peer_creds = dict(getattr(AFX_reinit, "_peer_bmc_creds", {}) or {})
        try:
            AFX_reinit._cluster_config = {
                "mgmt_ip": "10.0.0.180",
                "admin_user": "admin",
                "admin_password": "cluster-pass",
            }
            AFX_reinit._config_data = {
                "cluster": {"user": "admin", "password": "cluster-pass"},
                "secondary_nodes": [
                    {
                        "bmc": peer,
                        "node_mgmt_ip": "10.0.0.181",
                        "node_mgmt_port": "e0M",
                        "node_mgmt_netmask": "255.255.255.0",
                        "node_mgmt_gateway": "10.0.0.1",
                    }
                ],
            }
            AFX_reinit._peer_bmc_creds = {peer: {"user": "admin", "password": "peer-pass"}}

            def _mock_ssh_connect(host, user, password, **kwargs):
                label = kwargs.get("label", "")
                if str(label).startswith("auth-check/"):
                    return _DummyClient(), user, password
                raise Exception("Unable to connect to cluster SSH")

            with mock.patch.object(AFX_reinit, "_omit_nodes_by_number", return_value=[peer]), \
                 mock.patch.object(AFX_reinit, "_silent_ping", return_value=True), \
                 mock.patch.object(AFX_reinit, "_prompt", return_value="y"), \
                 mock.patch.object(AFX_reinit, "_print_autopilot_banner"), \
                 mock.patch.object(AFX_reinit, "_write_node_add_manifest"), \
                 mock.patch.object(AFX_reinit, "_ssh_connect_with_retry", side_effect=_mock_ssh_connect), \
                 mock.patch.object(AFX_reinit, "print"):
                ok = AFX_reinit._run_2b_parallel_add(
                    [peer],
                    "admin",
                    {peer: "peer-pass"},
                    log,
                )

            self.assertFalse(ok)
            self.assertTrue(
                any(
                    prefix == "ERROR"
                    and "cluster shell unavailable before peer workflow" in msg
                    for prefix, msg in log.entries
                )
            )
        finally:
            AFX_reinit._cluster_config = old_cluster_config
            AFX_reinit._config_data = old_config_data
            AFX_reinit._peer_bmc_creds = old_peer_creds


if __name__ == "__main__":
    unittest.main()
