"""Azure adapter guards; cloud calls are mocked and do not establish Azure qualification."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import azure_argocd as adapter


class ArgoAzureGuards(unittest.TestCase):
    def setUp(self):
        self.target = {"environment": "pprd", "region": "uks", "slot": "aks02"}
        self.native = {"targets": [dict(self.target, authorization_mode="kubernetes_rbac",
            deploy_principal_object_ids=["00000000-0000-0000-0000-000000000101"],
            deploy_kubernetes_usernames={"00000000-0000-0000-0000-000000000101": "observed-app-user"})]}
        self.wanted = {"metadata": {"name": "platform-demo-pprd-uks-aks02", "namespace": "argocd"},
            "spec": {"project": "platform-demo", "source": {"repoURL": "https://github.com/example/private.git",
                    "path": "gitops/releases/pprd/uks/aks02", "targetRevision": "main"},
                    "destination": {"server": "https://kubernetes.default.svc", "namespace": "platform-demo"}}}
        self.current = copy.deepcopy(self.wanted)
        self.current["metadata"].update(uid="one-exact-uid", resourceVersion="123")

    def test_sync_identity_can_only_get_and_patch_one_application(self):
        role, binding = adapter.access_resources(self.native, self.target, "argocd", self.wanted["metadata"]["name"])
        self.assertEqual(role["rules"], [{"apiGroups": ["argoproj.io"], "resources": ["applications"],
            "resourceNames": ["platform-demo-pprd-uks-aks02"], "verbs": ["get", "patch"]}])
        self.assertEqual(binding["subjects"], [{"kind": "User", "apiGroup": "rbac.authorization.k8s.io",
                                                "name": "observed-app-user"}])
        self.assertEqual(binding["roleRef"]["kind"], "Role")
        self.assertEqual(binding["metadata"]["namespace"], "argocd")

    def test_unobserved_identity_and_wrong_cluster_are_rejected(self):
        for mutate in (
            lambda n: n["targets"][0].update(slot="aks01"),
            lambda n: n["targets"][0].update(authorization_mode="azure_rbac"),
            lambda n: n["targets"][0].update(deploy_kubernetes_usernames={}),
            lambda n: n["targets"][0]["deploy_kubernetes_usernames"].update(
                {"00000000-0000-0000-0000-000000000101": "system:masters"}),
        ):
            native = copy.deepcopy(self.native); mutate(native)
            with self.assertRaises(ValueError):
                adapter.access_resources(native, self.target, "argocd", self.wanted["metadata"]["name"])

    def test_retirement_preserves_workloads_and_binds_uid_and_resource_version(self):
        options = adapter.retirement_options(self.current, self.wanted)
        self.assertEqual(options["propagationPolicy"], "Orphan")
        self.assertEqual(options["preconditions"], {"uid": "one-exact-uid", "resourceVersion": "123"})

    def test_retirement_rejects_foreign_application_running_operation_and_finalizers(self):
        mutations = [
            lambda x: x["spec"]["source"].update(path="gitops/releases/pprd/uks/aks01"),
            lambda x: x["spec"]["destination"].update(namespace="another-app"),
            lambda x: x["spec"].update(syncPolicy={"automated": {}}),
            lambda x: x["metadata"].update(finalizers=["resources-finalizer.argocd.argoproj.io"]),
            lambda x: x.update(operation={"sync": {"revision": "a"*40}}),
            lambda x: x.update(status={"operationState": {"phase": "Running"}}),
        ]
        for mutate in mutations:
            value = copy.deepcopy(self.current); mutate(value)
            with self.assertRaises(ValueError):
                adapter.retirement_options(value, self.wanted)

    def test_existing_repository_credential_is_never_replaced(self):
        import base64
        secret = {"metadata": {"labels": {"argocd.argoproj.io/secret-type": "repository"}},
                  "data": {"url": base64.b64encode(b"https://github.com/example/private.git").decode()}}
        config = {"argo_namespace": "argocd", "repository_url": "https://github.com/example/private.git"}
        with patch.object(adapter, "command", return_value=json.dumps(secret)) as call:
            adapter.repository_secret(["kubectl"], config)
        self.assertEqual(call.call_count, 1)
        config["repository_url"] = "https://github.com/other/private.git"
        with patch.object(adapter, "command", return_value=json.dumps(secret)) as call:
            with self.assertRaises(ValueError):
                adapter.repository_secret(["kubectl"], config)
        self.assertEqual(call.call_count, 1)

    def test_new_repository_credential_uses_stdin_and_does_not_print_provider_secrets(self):
        config = {"argo_namespace": "argocd", "repository_url": "https://github.com/example/private.git"}
        with patch.dict(adapter.os.environ, {"ARGO_GIT_USERNAME": "git", "ARGO_GIT_READ_TOKEN": "private-test-token"}), \
             patch.object(adapter, "command", side_effect=["", "created"]) as call:
            adapter.repository_secret(["kubectl"], config)
        args, secret = call.call_args.args
        self.assertNotIn("private-test-token", " ".join(args))
        self.assertEqual(secret["stringData"]["password"], "private-test-token")
        failed = subprocess.CompletedProcess(["kubectl"], 1, stdout="private-test-token", stderr="private-test-token")
        with patch.object(adapter.subprocess, "run", return_value=failed):
            with self.assertRaises(ValueError) as error:
                adapter.command(["kubectl"])
        self.assertNotIn("private-test-token", str(error.exception))

    def test_stale_commit_and_dirty_checkout_are_rejected(self):
        for outputs in (["b"*40], ["a"*40, " M delivery.json"]):
            with patch.object(adapter, "command", side_effect=outputs):
                with self.assertRaises(ValueError):
                    adapter.validate_source(Path("."), "a"*40)

    def test_uncommitted_gitops_configuration_stops_before_cloud_access(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as folder:
            args = SimpleNamespace(source=Path(folder), gitops_commit="a"*40,
                                   environment="pprd", region="uks")
            shared = MagicMock()
            shared.load_config.return_value = {"revision": "main", "repository_url": "changed"}
            with patch.object(adapter, "validate_source"), \
                 patch.object(adapter, "committed", return_value={"revision": "main", "repository_url": "approved"}), \
                 patch.object(adapter, "command") as cloud:
                with self.assertRaisesRegex(ValueError, "committed version"):
                    adapter.execute(args, shared)
            cloud.assert_not_called()

    def test_azure_gitops_callers_use_cloud_profile_and_new_target_names(self):
        for host in ("azure", "github"):
            proposal = (ROOT / f"examples/delivery/{host}-azure-workload-gitops-propose.yml").read_text()
            lifecycle = (ROOT / f"examples/delivery/{host}-azure-workload-argocd.yml").read_text()
            self.assertIn("delivery.azure-workload.apps.json", proposal)
            self.assertNotIn("delivery.gateway.apps.json", proposal)
            self.assertIn("targetCluster" if host == "azure" else "target-cluster", proposal)
            self.assertIn("scripts/azure_argocd.py", lifecycle)
            self.assertNotIn("--admin", lifecycle)
            self.assertIsInstance(yaml.safe_load(lifecycle), dict)
