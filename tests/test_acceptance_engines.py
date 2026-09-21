"""Safety checks for the real-engine kind adapters; no cloud or cluster writes."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import acceptance_engines as engines
import acceptance_gateway as gateway
import acceptance_native_rbac as native


class EngineAdapters(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        work = Path(temporary.name)
        prefix = "aks-demo-12345678"
        self.cluster = prefix + "-aks01"
        self.context = "kind-" + self.cluster
        self.test = SimpleNamespace(
            prefix=prefix, clusters=[self.cluster], work=work, kubeconfig=work / "kubeconfig",
            args=SimpleNamespace(templates=work / "templates", kubectl="kubectl", helm="helm"),
            record={}, kubectl=Mock(), gateway_dir=work / "gateway",
            pins=json.loads((ROOT / "ci-tools.json").read_text()),
        )
        self.test.gateway_dir.mkdir()
        self.config = {
            "apiVersion": "v1", "kind": "Config", "current-context": self.context,
            "clusters": [{"name": self.context, "cluster": {
                "server": "https://127.0.0.1:43210", "certificate-authority-data": "private-ca",
            }}],
            "contexts": [{"name": self.context, "context": {
                "cluster": self.context, "user": self.context,
            }}],
            "users": [{"name": self.context, "user": {
                "client-certificate-data": "private-client", "client-key-data": "private-key",
            }}],
        }
        self.write_config()
        self.bundle = work / "application-bundle"
        self.bundle.mkdir()
        self.receipt = {
            "source_commit": "a" * 40, "image_digest": "sha256:" + "b" * 64,
            "target": {"slot": "aks01", "environment": "pprd", "region": "uks",
                       "namespace": "platform-demo"},
        }
        (self.bundle / "release.json").write_text(json.dumps(self.receipt))

    def write_config(self):
        self.test.kubeconfig.write_text(yaml.safe_dump(self.config))

    def test_copy_has_one_context_exact_impersonation_and_private_temporary_permissions(self):
        original = self.test.kubeconfig.read_bytes()
        with engines.synthetic_context(self.test, self.cluster, "aks01", "application") as (path, context, runtime, subject):
            selected = yaml.safe_load(path.read_text())
            self.assertEqual(context, self.context)
            self.assertEqual(subject, self.test.prefix + "-application")
            self.assertEqual(selected["users"][0]["user"]["as"], subject)
            self.assertNotIn("as-groups", selected["users"][0]["user"])
            self.assertEqual(len(selected["contexts"]), 1)
            self.assertTrue(path.is_relative_to(self.test.work))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(runtime.stat().st_mode), 0o700)
        self.assertFalse(path.exists())
        self.assertEqual(self.test.kubeconfig.read_bytes(), original)
        self.assertEqual(self.test.record, {})

    def test_remote_api_insecure_tls_and_external_auth_are_rejected(self):
        original = copy.deepcopy(self.config)
        cases = [
            ("remote", lambda data: data["clusters"][0]["cluster"].update(server="https://azure.example.test:443")),
            ("http", lambda data: data["clusters"][0]["cluster"].update(server="http://127.0.0.1:443")),
            ("credentials", lambda data: data["clusters"][0]["cluster"].update(server="https://u:p@127.0.0.1:443")),
            ("skip-tls", lambda data: data["clusters"][0]["cluster"].update({"insecure-skip-tls-verify": True})),
            ("proxy", lambda data: data["clusters"][0]["cluster"].update({"proxy-url": "http://remote:80"})),
            ("exec", lambda data: data["users"][0]["user"].update({"exec": {"command": "az"}})),
            ("token", lambda data: data["users"][0]["user"].update(token="must-not-be-used")),
            ("external-cert", lambda data: data["users"][0]["user"].update({"client-key": "/outside/key"})),
            ("admin-groups", lambda data: data["users"][0]["user"].update({"as-groups": ["system:masters"]})),
        ]
        for name, mutate in cases:
            with self.subTest(name=name):
                self.config = copy.deepcopy(original)
                mutate(self.config)
                self.write_config()
                with self.assertRaises(ValueError):
                    engines.owned_context(self.test, self.cluster, "aks01")

    def test_foreign_context_reference_duplicate_and_foreign_cluster_are_rejected(self):
        with self.assertRaises(ValueError):
            engines.owned_context(self.test, "production", "aks01")
        self.config["contexts"][0]["context"]["cluster"] = "real-aks"
        self.write_config()
        with self.assertRaisesRegex(ValueError, "matching kind"):
            engines.owned_context(self.test, self.cluster, "aks01")
        self.config["contexts"][0]["context"]["cluster"] = self.context
        self.config["users"].append(copy.deepcopy(self.config["users"][0]))
        self.write_config()
        with self.assertRaisesRegex(ValueError, "exactly one"):
            engines.owned_context(self.test, self.cluster, "aks01")

    def test_symlinked_kubeconfig_is_rejected(self):
        original = self.test.kubeconfig
        link = self.test.work / "linked"
        link.symlink_to(original)
        self.test.kubeconfig = link
        with self.assertRaisesRegex(ValueError, "isolated kubeconfig"):
            engines.owned_context(self.test, self.cluster, "aks01")

    def test_application_executes_shared_core_as_scoped_user_and_retains_only_metadata(self):
        calls = []

        def execute(test, script, *arguments):
            self.assertIn("delivery._deploy_to_context", script)
            bundle, receipt_sha, path, context = arguments
            self.assertEqual(bundle, self.bundle)
            self.assertEqual(receipt_sha, hashlib.sha256((bundle / "release.json").read_bytes()).hexdigest())
            self.assertEqual(context, self.context)
            data = yaml.safe_load(path.read_text())
            self.assertEqual(data["users"][0]["user"]["as"], self.test.prefix + "-application")
            calls.append(path)

        with patch.object(engines, "execute", side_effect=execute), \
                patch.object(engines, "template_evidence", return_value={"repository_commit": "d" * 40}):
            result = engines.deploy(self.test, self.cluster, "aks01", self.bundle)
        self.assertEqual(result, self.receipt)
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0].exists())
        row = self.test.record["tier_execution"][0]
        self.assertEqual(row["identity_source"], "synthetic-kubernetes-impersonation")
        self.assertEqual(row["engine"], "delivery._deploy_to_context")
        self.assertNotIn("private-key", json.dumps(self.test.record))
        self.test.kubectl.assert_not_called()

    def test_wrong_slot_and_foreign_bundle_fail_before_shared_execution(self):
        with patch.object(engines, "execute") as execute:
            self.receipt["target"]["slot"] = "aks02"
            (self.bundle / "release.json").write_text(json.dumps(self.receipt))
            with self.assertRaisesRegex(ValueError, "acceptance target"):
                engines.deploy(self.test, self.cluster, "aks01", self.bundle)
            with self.assertRaisesRegex(ValueError, "private workspace"):
                engines.deploy(self.test, self.cluster, "aks01", ROOT)
            execute.assert_not_called()

    def test_failed_shared_application_execution_leaves_no_pass_record_or_credentials(self):
        paths = []

        def fail(test, script, *arguments):
            paths.append(arguments[2])
            raise subprocess.CalledProcessError(1, ["shared-engine"])

        with patch.object(engines, "execute", side_effect=fail):
            with self.assertRaises(subprocess.CalledProcessError):
                engines.deploy(self.test, self.cluster, "aks01", self.bundle)
        self.assertFalse(paths[0].exists())
        self.assertNotIn("tier_execution", self.test.record)

    def test_platform_prepares_before_binding_and_uses_dedicated_platform_context(self):
        self.test.platform_fixture = self.test.gateway_dir / "platform-source"
        self.test.platform_fixture_commit = "e" * 40
        events = []

        def execute(test, script, *arguments):
            if "p.prepare(" in script:
                events.append("prepare")
                output = arguments[-1]
                output.mkdir()
                (output / "platform.json").write_text(json.dumps({
                    "source_commit": "e" * 40, "crds": ["one", "two"],
                    "crd_policies": ["safe-upgrade"],
                }))
            else:
                events.append("apply")
                self.assertIn("p._apply_to_context", script)
                data = yaml.safe_load(arguments[2].read_text())
                self.assertEqual(data["users"][0]["user"]["as"],
                                 self.test.prefix + "-platform-aks01")
                self.assertEqual(arguments[3], self.context)

        self.test.kubectl.side_effect = lambda *args, **kwargs: events.append("binding")
        with patch.object(engines, "execute", side_effect=execute), \
                patch.object(native, "platform_manifests", return_value="synthetic-declaration") as manifest, \
                patch.object(engines, "template_evidence", return_value={"repository_commit": "d" * 40}):
            engines.install_platform(self.test, self.cluster, "aks01")
        self.assertEqual(events, ["prepare", "binding", "apply"])
        manifest.assert_called_once_with(self.test.args.templates,
                                         self.test.prefix + "-platform-aks01", "aks01")
        row = self.test.record["tier_execution"][0]
        self.assertEqual(row["crd_safe_upgrade_policy_bundle_count"], 1)

    def test_failed_platform_prepare_grants_no_cluster_authority(self):
        self.test.platform_fixture = self.test.gateway_dir / "platform-source"
        self.test.platform_fixture_commit = "e" * 40
        with patch.object(engines, "execute", side_effect=ValueError("invalid package")), \
                patch.object(native, "platform_manifests") as manifest:
            with self.assertRaisesRegex(ValueError, "invalid package"):
                engines.install_platform(self.test, self.cluster, "aks01")
        self.test.kubectl.assert_not_called()
        manifest.assert_not_called()

    def test_gateway_install_delegates_platform_before_local_tls_fixture(self):
        events = []
        self.test.kubectl.side_effect = lambda *args, **kwargs: events.append("tls") or "local-secret"
        with patch.object(engines, "install_platform", side_effect=lambda *args: events.append("platform")) as install:
            gateway.install(self.test, self.cluster, Mock())
        install.assert_called_once_with(self.test, self.cluster, "aks01")
        self.assertEqual(events, ["platform", "tls", "tls"])


class MaintainedPlatformFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        work = Path(temporary.name)
        candidates = [ROOT.parent / "aks-delivery-templates", ROOT.parent / "templates"]
        templates = next((path for path in candidates if (path / "examples/platform-envoy").is_dir()), None)
        self.assertIsNotNone(templates, "The maintained shared-template checkout is required")
        self.test = SimpleNamespace(
            gateway_dir=work / "gateway", prefix="aks-demo-12345678",
            args=SimpleNamespace(templates=templates),
            pins=json.loads((ROOT / "ci-tools.json").read_text()),
        )
        self.test.gateway_dir.mkdir()
        self.original = templates / "examples/platform-envoy"

    def test_kind_fixture_preserves_pins_policy_topology_and_source_files(self):
        before = {str(path.relative_to(self.original)): path.read_bytes()
                  for path in self.original.rglob("*") if path.is_file()}
        engines.prepare_platform_fixture(self.test)
        source = self.test.platform_fixture
        controller = yaml.safe_load((source / "values/controller.yaml").read_text())
        original = yaml.safe_load((self.original / "values/controller.yaml").read_text())
        self.assertEqual(controller["deployment"]["replicas"], 1)
        controller["deployment"]["replicas"] = original["deployment"]["replicas"]
        self.assertEqual(controller, original)
        config = json.loads((source / "platform.services.json").read_text())
        for target in config["targets"]:
            self.assertEqual(target["crd_bundles"], self.test.pins["envoy_gateway"]["crds"])
            self.assertTrue(target["manage_crds"])
            self.assertEqual(target["workload_service_accounts"], [])
            proxy = yaml.safe_load((source / ("manifests/proxy-" + target["slot"] + ".yaml")).read_text())
            kubernetes = proxy["spec"]["provider"]["kubernetes"]
            self.assertEqual(kubernetes["envoyService"], {"type": "ClusterIP"})
            self.assertEqual(kubernetes["envoyDeployment"]["replicas"], 1)
            self.assertNotIn("envoyHpa", kubernetes)
            self.assertIn("envoyPDB", kubernetes)
        self.assertEqual((source / "manifests/gateway.yaml").read_bytes(),
                         (self.original / "manifests/gateway.yaml").read_bytes())
        self.assertEqual(before, {str(path.relative_to(self.original)): path.read_bytes()
                                 for path in self.original.rglob("*") if path.is_file()})
        self.assertEqual(subprocess.check_output(
            ["git", "-C", str(source), "status", "--porcelain"], text=True), "")
        self.assertRegex(self.test.platform_fixture_commit, r"^[0-9a-f]{40}$")

    def test_changed_controller_pin_is_rejected(self):
        self.test.pins["envoy_gateway"]["images"]["envoyGateway"] = "unreviewed"
        with self.assertRaisesRegex(ValueError, "controller images"):
            engines.prepare_platform_fixture(self.test)


if __name__ == "__main__":
    unittest.main()
