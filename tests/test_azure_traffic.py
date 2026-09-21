import copy
import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location("traffic_qualification", Path(__file__).resolve().parents[1] / "scripts/qualify_azure_traffic.py")
traffic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(traffic)
ID = "/subscriptions/11111111-1111-4111-8111-111111111111/resourceGroups/lab/providers/Microsoft.Network/applicationGateways/lab"


class TrafficQualificationTests(unittest.TestCase):
    def test_frontend_must_belong_to_selected_gateway(self):
        gateway = {"id": ID, "provisioningState": "Succeeded",
                   "frontendIPConfigurations": [{"privateIPAddress": "10.81.12.10"}]}
        traffic.validate_gateway(gateway, ID, "10.81.12.10", lambda _: self.fail("Unexpected read"))
        with self.assertRaises(ValueError):
            traffic.validate_gateway(gateway, ID, "10.81.12.11", lambda _: {})
        with self.assertRaises(ValueError):
            traffic.validate_gateway(gateway, ID + "-wrong", "10.81.12.10", lambda _: {})

    def test_public_ip_resource_identity_verified(self):
        public = "/subscriptions/example/resourceGroups/lab/providers/Microsoft.Network/publicIPAddresses/waf"
        gateway = {"id": ID, "provisioningState": "Succeeded",
                   "frontendIPConfigurations": [{"publicIPAddress": {"id": public}}]}
        traffic.validate_gateway(gateway, ID, "203.0.113.10",
                                 lambda _: {"id": public, "ipAddress": "203.0.113.10"})
        with self.assertRaises(ValueError):
            traffic.validate_gateway(gateway, ID, "203.0.113.10",
                                     lambda _: {"id": "another", "ipAddress": "203.0.113.10"})

    def test_backend_requires_each_selected_pool_and_all_servers_healthy(self):
        health = {"backendAddressPools": [{
            "backendAddressPool": {"id": ID + "/backendAddressPools/stable"},
            "backendHttpSettingsCollection": [{
                "backendHttpSettings": {"id": ID + "/backendHttpSettingsCollection/web"},
                "servers": [{"health": "Healthy"}]}]}]}
        self.assertEqual(traffic.validate_health(health, ["stable"], gateway_id=ID)[0]["healthy_servers"], 1)
        for missing in ("other",):
            with self.assertRaises(ValueError):
                traffic.validate_health(health, [missing], gateway_id=ID)
        for servers in ([], [{"health": "Unknown"}], [{"health": "Healthy"}, {"health": "Unhealthy"}]):
            bad = copy.deepcopy(health)
            bad["backendAddressPools"][0]["backendHttpSettingsCollection"][0]["servers"] = servers
            with self.assertRaises(ValueError):
                traffic.validate_health(bad, ["stable"], gateway_id=ID)

    def test_release_checks_app_slot_and_full_revision(self):
        payload = {"application": "aks-platform-demo", "slot": "aks02", "revision": "a" * 40}
        traffic.validate_release(payload, "aks02", "a" * 40)
        for field in payload:
            bad = dict(payload, **{field: "wrong"})
            with self.assertRaises(ValueError):
                traffic.validate_release(bad, "aks02", "a" * 40)


class TrafficFailureEvidenceTests(unittest.TestCase):
    def test_foreign_gateway_health_and_duplicate_settings_rejected(self):
        setting = {"backendHttpSettings": {"id": ID + "/backendHttpSettingsCollection/web"},
                   "servers": [{"health": "Healthy", "address": "10.81.0.21"}]}
        health = {"backendAddressPools": [{
            "backendAddressPool": {"id": ID + "/backendAddressPools/stable"},
            "backendHttpSettingsCollection": [setting]}]}
        foreign = copy.deepcopy(health)
        foreign["backendAddressPools"][0]["backendAddressPool"]["id"] = ID + "-foreign/backendAddressPools/stable"
        with self.assertRaisesRegex(ValueError, "selected gateway"):
            traffic.validate_health(foreign, ["stable"], gateway_id=ID)
        foreign = copy.deepcopy(health)
        foreign["backendAddressPools"][0]["backendHttpSettingsCollection"][0]["backendHttpSettings"]["id"] = ID + "-foreign/backendHttpSettingsCollection/web"
        with self.assertRaisesRegex(ValueError, "selected gateway"):
            traffic.validate_health(foreign, ["stable"], gateway_id=ID)
        duplicate = copy.deepcopy(health)
        duplicate["backendAddressPools"][0]["backendHttpSettingsCollection"].append(setting)
        with self.assertRaisesRegex(ValueError, "distinct"):
            traffic.validate_health(duplicate, ["stable"], gateway_id=ID)

    def test_nonobject_response_is_a_validation_failure(self):
        for payload in ([], None, "unexpected", 7):
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, "JSON object"):
                traffic.validate_release(payload, "aks01", "a" * 40)

    def run_main(self, malformed=False, changed=False):
        import json
        import stat
        import sys
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "report.json"
            gateway = {"id": ID, "etag": "stable-etag", "provisioningState": "Succeeded",
                       "frontendIPConfigurations": [{"privateIPAddress": "10.81.12.10"}]}
            health = {"backendAddressPools": [{
                "backendAddressPool": {"id": ID + "/backendAddressPools/stable"},
                "backendHttpSettingsCollection": [{
                    "backendHttpSettings": {"id": ID + "/backendHttpSettingsCollection/web"},
                    "servers": [{"health": "Healthy", "address": "10.81.0.21"}]}]}]}
            final = dict(gateway, etag="changed" if changed else "stable-etag")
            payload = [] if malformed else {"application": "aks-platform-demo", "slot": "aks01", "revision": "a" * 40}
            argv = ["qualify", "--gateway-id", ID, "--endpoint-ip", "10.81.12.10",
                    "--web-host", "web.example.test", "--api-host", "api.example.test",
                    "--backend-pool", "stable", "--expected-slot", "aks01", "--revision", "a" * 40,
                    "--step", "initial-active", "--samples", "1", "--report", str(report)]
            with patch.object(sys, "argv", argv), patch.object(traffic, "azure", side_effect=[gateway, health, final]), \
                    patch.object(traffic, "request", return_value=payload):
                if malformed or changed:
                    with self.assertRaises(ValueError):
                        traffic.main()
                else:
                    traffic.main()
            self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o600)
            return json.loads(report.read_text())

    def test_malformed_response_retains_failed_private_evidence(self):
        report = self.run_main(malformed=True)
        self.assertEqual(report["result"], "failed")
        self.assertEqual(report["error_type"], "ValueError")
        self.assertIn("finished_at", report)

    def test_concurrent_gateway_update_invalidates_samples(self):
        report = self.run_main(changed=True)
        self.assertEqual(report["result"], "failed")
        self.assertNotIn("gateway_configuration_unchanged", report)

    def test_stable_gateway_retains_configuration_evidence(self):
        report = self.run_main()
        self.assertEqual(report["result"], "passed")
        self.assertTrue(report["gateway_configuration_unchanged"])
        self.assertEqual(report["backend_health"][0]["addresses"], ["10.81.0.21"])


class RealTrafficTLSRequests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import json
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import ssl
        import subprocess
        import tempfile
        import threading
        cls.temporary = tempfile.TemporaryDirectory(prefix="gateway-qualifier-tls-")
        cls.directory = Path(cls.temporary.name)
        cls.certificate = cls.directory / "cert.pem"
        key = cls.directory / "key.pem"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
            "-subj", "/CN=web.example.test", "-addext",
            "subjectAltName=DNS:web.example.test,DNS:api.example.test",
            "-keyout", str(key), "-out", str(cls.certificate),
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.requests, cls.sni = [], []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                cls.requests.append((self.headers.get("Host"), self.path))
                payload = json.dumps({"application": "aks-platform-demo", "slot": "aks02",
                                      "revision": "a" * 40}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *arguments):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cls.certificate), str(key))
        context.set_servername_callback(lambda connection, name, initial: cls.sni.append(name))
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.temporary.cleanup()

    def local_request(self, hostname, context):
        import socket
        from unittest.mock import patch
        original = socket.create_connection
        calls = []

        def route(endpoint, timeout):
            calls.append(endpoint)
            return original(("127.0.0.1", self.server.server_port), timeout=timeout)

        with patch.object(traffic.socket, "create_connection", side_effect=route):
            result = traffic.request("203.0.113.10", hostname, "/api/version", context)
        self.assertEqual(calls, [("203.0.113.10", 443)])
        return result

    def test_actual_tls_validates_explicit_ca_sni_and_http_host(self):
        import ssl
        payload = self.local_request("api.example.test",
                                     ssl.create_default_context(cafile=str(self.certificate)))
        traffic.validate_release(payload, "aks02", "a" * 40)
        self.assertEqual(self.sni[-1], "api.example.test")
        self.assertEqual(self.requests[-1], ("api.example.test", "/api/version"))

    def test_actual_tls_rejects_wrong_hostname_and_untrusted_ca(self):
        import ssl
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.local_request("wrong.example.test",
                               ssl.create_default_context(cafile=str(self.certificate)))
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.local_request("web.example.test", ssl.create_default_context())

    def test_disabled_certificate_verification_is_rejected_before_connecting(self):
        import ssl
        from unittest.mock import patch
        with patch.object(traffic.socket, "create_connection") as connection:
            with self.assertRaisesRegex(ValueError, "verification"):
                traffic.request("203.0.113.10", "web.example.test", "/version",
                                ssl._create_unverified_context())
        connection.assert_not_called()


if __name__ == "__main__":
    unittest.main()
