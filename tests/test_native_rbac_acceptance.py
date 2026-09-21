"""Runner checks are not evidence of a successful Kubernetes acceptance run."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("native_acceptance", ROOT / "scripts/acceptance_native_rbac.py")
native = importlib.util.module_from_spec(spec)
spec.loader.exec_module(native)


def completed(code=0, output="", error=""):
    return subprocess.CompletedProcess([], code, output, error)


class NativeAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        work = Path(self.temporary.name)
        bundle = work / "bundle"
        bundle.mkdir()
        (bundle / "manifest.yaml").write_text(json.dumps({
            "apiVersion": "v1", "kind": "Service",
            "metadata": {"name": "platform-demo", "namespace": "platform-demo"},
        }))
        prefix = "aks-demo-12345678"
        self.cluster = prefix + "-aks01"
        self.bundle = bundle
        self.test = SimpleNamespace(
            prefix=prefix, clusters=[self.cluster], work=work, kubeconfig=work / "kubeconfig",
            args=SimpleNamespace(kubectl="kubectl", templates=work / "templates"), record={},
        )
        self.enabled = False
        self.commands = []

        def apply(cluster, *arguments, **kwargs):
            self.assertEqual(cluster, self.cluster)
            self.assertEqual(arguments, ("apply", "--validate=strict", "-f", "-"))
            self.enabled = bool(json.loads(kwargs["input"])["subjects"])
        self.test.kubectl = Mock(side_effect=apply)

    def declaration(self, templates, subject):
        return json.dumps({"subjects": [subject] if subject else []})

    def response(self, test, cluster, subject, command, **kwargs):
        self.commands.append(command)
        self.assertEqual(cluster, self.cluster)
        self.assertEqual(subject, self.test.prefix + "-application")
        if command[:2] == ["auth", "can-i"]:
            verb, resource = command[2:4]
            namespace = command[command.index("--namespace") + 1]
            denied = (
                namespace == "default" or verb == "delete" or
                resource in ("roles.rbac.authorization.k8s.io", "rolebindings.rbac.authorization.k8s.io", "secrets") or
                (verb == "patch" and resource in ("gateways.gateway.networking.k8s.io", "envoyproxies.gateway.envoyproxy.io")) or
                ("--subresource" in command and command[command.index("--subresource") + 1] == "exec")
            )
            allowed = self.enabled and not denied
            return completed(0 if allowed else 1, "yes" if allowed else "no")
        if command[:2] == ["create", "--raw"]:
            self.assertEqual(command[2], "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews")
            body = json.loads(kwargs["input"])
            self.assertEqual(body["spec"]["resourceAttributes"]["group"], "secrets-store.csi.x-k8s.io")
            return completed(output=json.dumps({"status": {"allowed": self.enabled}}))
        if command[0] == "apply" and self.enabled:
            self.assertIn("--dry-run=server", command)
            return completed(output="service/platform-demo configured (server dry run)")
        return completed(1, error="Error from server (Forbidden): test user cannot patch resource")

    def test_records_real_probe_methods_and_empty_binding_revocation_sequence(self):
        with patch.object(native, "manifests", side_effect=self.declaration), patch.object(native, "as_user", side_effect=self.response):
            native.check(self.test, self.cluster, "aks01", self.bundle)
        rows = self.test.record["native_rbac_checks"]
        self.assertEqual(len(rows), 21)
        self.assertTrue(all(r["context"] == "kind-" + self.cluster for r in rows))
        self.assertEqual([r["step"] for r in rows[-3:]],
                         ["removed-principal-revoked", "revoked-bundle-rejected", "restored-bundle-admitted"])
        self.assertEqual(self.test.kubectl.call_count, 3)
        declarations = [json.loads(call.kwargs["input"])["subjects"] for call in self.test.kubectl.call_args_list]
        self.assertEqual(declarations, [[self.test.prefix + "-application"], [], [self.test.prefix + "-application"]])
        self.assertEqual(len([r for r in rows if r["method"] == "explicit-self-subject-access-review"]), 2)
        self.assertTrue(any("--subresource" in c and "portforward" in c for c in self.commands))
        self.assertTrue(any("--subresource" in c and "exec" in c for c in self.commands))

    def test_transport_failure_never_counts_as_authorization_denial(self):
        with patch.object(native, "manifests", side_effect=self.declaration), patch.object(native, "as_user", return_value=completed(1, error="Unable to connect to the server")):
            with self.assertRaises(RuntimeError):
                native.check(self.test, self.cluster, "aks01", self.bundle)

    def test_unexpected_role_write_permission_fails(self):
        def overbroad(*args, **kwargs):
            result = self.response(*args, **kwargs)
            if "roles.rbac.authorization.k8s.io" in args[3]:
                return completed(output="yes")
            return result
        with patch.object(native, "manifests", side_effect=self.declaration), patch.object(native, "as_user", side_effect=overbroad):
            with self.assertRaises(AssertionError):
                native.check(self.test, self.cluster, "aks01", self.bundle)

    def test_forbidden_requires_actual_api_error(self):
        for value in [completed(), completed(1, error="NotFound"), completed(1, error="connection refused")]:
            with self.assertRaises(AssertionError):
                native.forbidden(value)
        native.forbidden(completed(1, error="Error from server (Forbidden): operation denied"))

    def test_foreign_context_rejected_before_any_call(self):
        with patch.object(native, "manifests") as generator:
            with self.assertRaises(ValueError):
                native.check(self.test, "production-cluster", "aks01", self.bundle)
            generator.assert_not_called()
        self.test.kubectl.assert_not_called()

    def test_secret_material_rejected_before_any_cluster_mutation(self):
        (self.bundle / "manifest.yaml").write_text(json.dumps({
            "apiVersion": "v1", "kind": "Secret",
            "metadata": {"name": "not-a-test-fixture", "namespace": "platform-demo"},
        }))
        with self.assertRaises(ValueError):
            native.check(self.test, self.cluster, "aks01", self.bundle)
        self.test.kubectl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
