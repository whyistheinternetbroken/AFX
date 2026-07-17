import unittest
from unittest import mock

import AFX_reinit


class PrimaryPostCreateLifRouteFixRootTests(unittest.TestCase):
    def test_fixes_cluster_mgmt_port_and_missing_default_route(self):
        issued = []

        lif_instance_output = """
Logical Interface Name: cluster_mgmt
Role: cluster-mgmt
Address: 192.168.0.180
Netmask: 255.255.255.0
Home Port: e2a

Logical Interface Name: oam-nvlts-01_mgmt1
Role: node-mgmt
Address: 192.168.0.181
Netmask: 255.255.255.0
Home Port: e0M
"""

        def _mock_run_cluster_command(_channel, cmd, timeout=60):
            issued.append(str(cmd))
            _cmd = str(cmd)
            if "net int show -role node-mgmt,cluster-mgmt" in _cmd:
                return lif_instance_output
            if "route show -vserver oam-nvlts -destination 0.0.0.0/0 -fields gateway" in _cmd:
                return "There are no entries matching your query."
            if "route show -vserver oam-nvlts" in _cmd:
                return "route show output"
            return ""

        cc = {
            "name": "oam-nvlts",
            "admin_password": "pw",
            "mgmt_ip": "192.168.0.180",
            "mgmt_port": "e0M",
            "mgmt_netmask": "255.255.255.0",
            "mgmt_gateway": "192.168.0.1",
        }

        with mock.patch.object(AFX_reinit, "_login_primary_cluster_shell", return_value=True), \
             mock.patch.object(
                 AFX_reinit,
                 "_resolve_node_mgmt_config",
                 return_value={"ip": "192.168.0.181", "port": "e0M", "netmask": "255.255.255.0"},
             ), \
             mock.patch.object(AFX_reinit, "_run_cluster_command", side_effect=_mock_run_cluster_command), \
             mock.patch.object(AFX_reinit, "print"):
            AFX_reinit._verify_primary_mgmt_lifs_and_route(object(), cc, primary_bmc="192.168.0.190")

        self.assertTrue(
            any(
                "net int modify -vserver oam-nvlts -lif cluster_mgmt -home-port e0M" in c
                for c in issued
            )
        )
        self.assertTrue(
            any(
                "route create -vserver oam-nvlts -destination 0.0.0.0/0 -gateway 192.168.0.1" in c
                for c in issued
            )
        )


if __name__ == "__main__":
    unittest.main()
