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
        platform = patch.object(native, "platform_check")
        self.platform = platform.start()
        self.addCleanup(platform.stop)

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


class NativePlatformAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        work = Path(self.temporary.name)
        prefix = "aks-demo-12345678"
        self.cluster = prefix + "-aks01"
        self.other = prefix + "-aks02"
        self.subject = prefix + "-platform-aks01"
        self.application = prefix + "-application"
        self.test = SimpleNamespace(
            prefix=prefix, clusters=[self.cluster, self.other], work=work,
            kubeconfig=work / "kubeconfig", args=SimpleNamespace(kubectl="kubectl", templates=work / "templates"),
            record={},
        )
        self.enabled = False
        self.created = False
        self.events = []
        self.test.kubectl = Mock(side_effect=self.admin)
        self.generator = patch.object(native, "platform_manifests", side_effect=self.declaration)
        self.generator.start()
        self.addCleanup(self.generator.stop)

    def declaration(self, templates, subject, slot):
        self.assertEqual(slot, "aks01")
        return json.dumps({"subjects": [subject] if subject else []})

    def admin(self, cluster, *arguments, **kwargs):
        self.assertEqual(cluster, self.cluster)
        self.events.append(("admin", arguments[0], kwargs.get("input")))
        if arguments[0] == "apply":
            self.enabled = bool(json.loads(kwargs["input"])["subjects"])
        else:
            self.assertEqual(arguments[:2], ("delete", "customresourcedefinition"))
            self.assertEqual(arguments[2], "platformchecks." + self.cluster + ".example.test")
            self.assertIn("--ignore-not-found=true", arguments)
            self.created = False

    def api(self, test, cluster, subject, command, **kwargs):
        self.events.append((subject, cluster, list(command)))
        allowed = cluster == self.cluster and subject == self.subject and self.enabled
        if not allowed:
            return completed(1, error="Error from server (Forbidden): synthetic user is not authorized")
        if command[0] == "create":
            fixture = json.loads(kwargs["input"])
            self.assertEqual(fixture["kind"], "CustomResourceDefinition")
            self.assertTrue(fixture["metadata"]["name"].endswith(self.cluster + ".example.test"))
            self.assertNotIn("--dry-run=server", command)
            self.created = True
        else:
            self.assertEqual(command[:2], ["patch", "customresourcedefinition"])
            self.assertTrue(self.created)
        return completed(output="synthetic fixture operation completed")

    def run_check(self, response=None):
        with patch.object(native, "as_user", side_effect=response or self.api):
            native.platform_check(self.test, self.cluster, "aks01")

    def test_actual_operation_sequence_proves_scope_revocation_and_restoration(self):
        self.run_check()
        rows = self.test.record["native_platform_rbac_checks"]
        self.assertEqual(len(rows), 7)
        self.assertEqual([row["step"] for row in rows], [
            "platform-created-crd", "unselected-cluster-rejected", "application-crd-rejected",
            "application-rolebinding-rejected", "platform-revoked-crd-rejected",
            "platform-restored-crd-patched", "platform-final-revocation-confirmed",
        ])
        self.assertEqual([row["allowed"] for row in rows], [True, False, False, False, False, True, False])
        self.assertEqual(rows[1]["context"], "kind-" + self.other)
        self.assertEqual(rows[1]["subject"], self.subject)
        self.assertTrue(all(row["identity_source"] == "synthetic-kind-fixture" for row in rows))
        self.assertFalse(self.created)
        self.assertFalse(self.enabled)
        self.assertEqual(self.test.record["cleanup_errors"], [])
        self.assertEqual([json.loads(event[2])["subjects"] for event in self.events
                          if event[:2] == ("admin", "apply")],
                         [[self.subject], [], [self.subject], []])

    def test_permission_on_unselected_cluster_is_a_failure_and_cleans_up(self):
        def response(test, cluster, subject, command, **kwargs):
            if cluster == self.other:
                return completed(output="created (server dry run)")
            return self.api(test, cluster, subject, command, **kwargs)
        with self.assertRaisesRegex(AssertionError, "Forbidden"):
            self.run_check(response)
        self.assertFalse(self.enabled)
        self.assertFalse(self.created)
        self.assertEqual(self.events[-1][:2], ("admin", "delete"))

    def test_transport_error_is_never_mistaken_for_revocation(self):
        def response(test, cluster, subject, command, **kwargs):
            if cluster == self.other:
                return completed(1, error="Unable to connect to the server")
            return self.api(test, cluster, subject, command, **kwargs)
        with self.assertRaisesRegex(AssertionError, "Forbidden"):
            self.run_check(response)
        self.assertFalse(self.enabled)
        self.assertFalse(self.created)

    def test_application_rolebinding_permission_is_rejected(self):
        def response(test, cluster, subject, command, **kwargs):
            body = json.loads(kwargs["input"]) if kwargs.get("input") else {}
            if subject == self.application and body.get("kind") == "RoleBinding":
                self.assertIn("--dry-run=server", command)
                return completed(output="created (server dry run)")
            return self.api(test, cluster, subject, command, **kwargs)
        with self.assertRaisesRegex(AssertionError, "Forbidden"):
            self.run_check(response)
        self.assertFalse(self.created)
        self.assertFalse(self.enabled)

    def test_cleanup_attempts_fixture_deletion_even_when_binding_clear_fails(self):
        count = 0
        def fail_last_apply(cluster, *arguments, **kwargs):
            nonlocal count
            if arguments[0] == "apply":
                count += 1
                if count == 4:
                    raise subprocess.CalledProcessError(1, "kubectl")
            return self.admin(cluster, *arguments, **kwargs)
        self.test.kubectl.side_effect = fail_last_apply
        with self.assertRaisesRegex(RuntimeError, "binding cleanup failed"):
            self.run_check()
        self.assertFalse(self.created)
        self.assertEqual(self.events[-1][:2], ("admin", "delete"))
        self.assertEqual(len(self.test.record["cleanup_errors"]), 1)

    def test_initial_apply_failure_still_clears_binding_and_deletes_fixture(self):
        count = 0
        def fail_first_apply(cluster, *arguments, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                raise subprocess.CalledProcessError(1, "kubectl", stderr="injected initial failure")
            return self.admin(cluster, *arguments, **kwargs)
        self.test.kubectl.side_effect = fail_first_apply
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_check()
        self.assertFalse(self.enabled)
        self.assertEqual(self.events[-1][:2], ("admin", "delete"))

    def test_requires_both_owned_clusters_before_mutation(self):
        for clusters in [
            [self.cluster], [self.cluster, "production"], [self.cluster, self.cluster],
            [self.cluster, self.other, "third"],
        ]:
            self.test.clusters = clusters
            with self.assertRaises(ValueError):
                self.run_check()
        self.test.kubectl.assert_not_called()

    def test_requires_isolated_kubeconfig_before_mutation(self):
        self.test.kubeconfig = self.test.work.parent / "shared-kubeconfig"
        with self.assertRaisesRegex(ValueError, "isolated kubeconfig"):
            self.run_check()
        self.test.kubectl.assert_not_called()


class PlatformGeneratorFixtureTests(unittest.TestCase):
    def binding(self, subject):
        return {
            "apiVersion": "v1", "kind": "List", "items": [{
                "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
                "metadata": {"name": "aks-delivery-platform",
                     "labels": {"app.kubernetes.io/managed-by": "aks-delivery-templates"}},
                "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "cluster-admin"},
                "subjects": [{"kind": "User", "apiGroup": "rbac.authorization.k8s.io", "name": subject}] if subject else [],
            }],
        }

    def test_generator_gets_selected_synthetic_output_and_exact_discovery_subject(self):
        subject = "aks-demo-12345678-platform-aks02"
        with patch.object(native.subprocess, "run", return_value=completed(output=json.dumps(self.binding(subject)))) as command:
            value = json.loads(native.platform_manifests("/tmp/templates", subject, "aks02"))
        self.assertEqual(value, self.binding(subject))
        settings = json.loads(command.call_args.kwargs["input"])
        self.assertEqual(settings["keys"], ["platform"])
        self.assertEqual(settings["records"][0]["username"], subject)
        contract = settings["outputs"]["delivery_authorization"]["value"]
        self.assertEqual(contract["principals"]["platform"]["clusters"], ["aks02"])
        self.assertEqual(list(contract["targets"]), ["aks02"])
        self.assertEqual(list(contract["cluster_user_assignments"]), ["platform/aks02"])
        script = command.call_args.args[0][2]
        self.assertIn("from native_authorization import platform_resources", script)
        self.assertIn("allow_platform_admin=True", script)
        self.assertNotIn("az ", script)

    def test_revocation_uses_same_generator_with_empty_keys_and_discovery(self):
        with patch.object(native.subprocess, "run", return_value=completed(output=json.dumps(self.binding(None)))) as command:
            native.platform_manifests("/tmp/templates", None, "aks01")
        settings = json.loads(command.call_args.kwargs["input"])
        self.assertEqual(settings["keys"], [])
        self.assertEqual(settings["records"], [])

    def test_rejects_any_binding_to_a_different_or_extra_user(self):
        expected = "aks-demo-12345678-platform-aks01"
        for mutation in ("different-user", "extra-resource", "group", "namespace"):
            binding = self.binding(expected)
            item = binding["items"][0]
            if mutation == "different-user":
                item["subjects"][0]["name"] = "external-user"
            elif mutation == "extra-resource":
                binding["items"].append({"kind": "ClusterRole"})
            elif mutation == "group":
                item["subjects"][0]["kind"] = "Group"
            else:
                item["metadata"]["namespace"] = "default"
            with self.subTest(mutation=mutation), patch.object(
                native.subprocess, "run", return_value=completed(output=json.dumps(binding))
            ):
                with self.assertRaisesRegex(ValueError, "synthetic platform User"):
                    native.platform_manifests("/tmp/templates", expected, "aks01")

    def test_external_or_cross_slot_user_is_rejected_before_generator(self):
        with patch.object(native.subprocess, "run") as command:
            for subject, slot in [
                ("external-user", "aks01"), ("system:admin", "aks01"),
                ("aks-demo-12345678-platform-aks02", "aks01"),
                ("aks-demo-12345678-platform-aks01", "production"),
            ]:
                with self.assertRaises(ValueError):
                    native.platform_manifests("/tmp/templates", subject, slot)
            command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
