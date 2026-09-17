"""Focused local acceptance-harness safeguards; these are not Kubernetes acceptance."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "acceptance", Path(__file__).resolve().parents[1] / "scripts/test_dual_cluster.py")
acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceptance)


class RegistryDigestTests(unittest.TestCase):
    def setUp(self):
        self.body = b'{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json"}'
        self.digest = "sha256:" + hashlib.sha256(self.body).hexdigest()
        owner = self
        class Registry(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Docker-Content-Digest", owner.digest)
                self.end_headers()
                self.wfile.write(owner.body)
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Registry)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_digest_is_verified_against_actual_registry_content(self):
        digest = acceptance.registry_digest(self.server.server_port, "aks-platform-demo", "initial")
        self.assertEqual(digest, self.digest)

    def test_tampered_registry_digest_is_rejected(self):
        self.digest = "sha256:" + "f" * 64
        with self.assertRaisesRegex(ValueError, "mismatched"):
            acceptance.registry_digest(self.server.server_port, "aks-platform-demo", "initial")

    def test_mutable_test_images_are_rejected(self):
        with self.assertRaises(ValueError):
            acceptance.pinned_image("registry:latest")
        self.assertEqual(acceptance.pinned_image("registry:3@" + self.digest), "registry:3@" + self.digest)


class CheckOnlyTests(unittest.TestCase):
    def test_changed_active_slot_is_detected_without_repair(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = SimpleNamespace(artifacts=root/"artifacts", report=None, kind="kind", kubectl="kubectl")
            run = acceptance.Acceptance(args, root/"work")
            wrong = {"spec": {"template": {"spec": {"containers": [{"image": "unexpected/image:tag"}]}}}}
            calls = []
            def kubectl(cluster, *arguments, **kwargs):
                calls.append(arguments)
                return json.dumps(wrong) if "get" in arguments else ""
            with patch.object(run, "kubectl", side_effect=kubectl):
                with self.assertRaisesRegex(ValueError, "selected immutable image"):
                    run.check_release("aks-demo-test-aks01", "aks01", "a"*40, "sha256:"+"b"*64, "active-slot-unchanged")
            self.assertTrue(calls)
            self.assertFalse(any("apply" in command or "patch" in command for command in calls))
            self.assertEqual(run.record["checks"], [])


class CleanupTests(unittest.TestCase):
    def test_cleanup_continues_after_failure_and_records_failed_result(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = SimpleNamespace(artifacts=root/"artifacts", report=root/"report.json", kind="kind", kubectl="kubectl")
            run = acceptance.Acceptance(args, root/"work")
            run.clusters = ["aks-demo-test-aks01", "aks-demo-test-aks02"]
            run.registry_created = True
            run.record["result"] = "passed"
            calls = []
            def cleanup(command, **kwargs):
                calls.append(command)
                if len(calls) == 1:
                    raise subprocess.TimeoutExpired(command, 180)
                return subprocess.CompletedProcess(command, 0)
            with patch.object(acceptance.subprocess, "run", side_effect=cleanup):
                run.cleanup()
            self.assertEqual(len(calls), 3)
            self.assertEqual(calls[-1][:3], ["docker", "rm", "-f"])
            report = json.loads(args.report.read_text())
            self.assertEqual(report["result"], "failed")
            self.assertEqual(len(report["cleanup_errors"]), 1)
            self.assertEqual(report["checks"], [])

    def test_incomplete_execution_never_reports_passed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = SimpleNamespace(artifacts=root/"artifacts", report=root/"report.json", kind="kind", kubectl="kubectl")
            run = acceptance.Acceptance(args, root/"work")
            run.cleanup()
            self.assertEqual(json.loads(args.report.read_text())["result"], "failed")


if __name__ == "__main__":
    unittest.main()
