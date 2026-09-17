"""Credential-free tests for Argo acceptance orchestration and real dumb-HTTP Git."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import acceptance_argocd as argo
import test_argocd_dual_cluster as harness
from test_dual_cluster import run

A, B = "a" * 40, "b" * 40


def state(revision=A, phase="Succeeded", sync="Synced", health="Healthy"):
    return {"metadata": {"name": "platform-demo-pprd-uks-aks02"},
            "status": {"sync": {"revision": revision, "status": sync}, "health": {"status": health},
                       "operationState": {"phase": phase, "syncResult": {"revision": revision}}}}


class RevisionTests(unittest.TestCase):
    def test_healthy_requires_current_operation_revision_and_health(self):
        value = state()
        self.assertTrue(argo.evaluate(value, A, "healthy"))
        self.assertFalse(argo.evaluate(value, B, "healthy"))
        for changed in (state(sync="OutOfSync"), state(health="Progressing"), state(phase="Running")):
            self.assertFalse(argo.evaluate(changed, A, "healthy"))
        pending = state()
        pending["operation"] = argo.sync_patch(A)["operation"]
        self.assertFalse(argo.evaluate(pending, A, "healthy"))

    def test_stale_failure_does_not_block_new_revision_but_current_does(self):
        value = state(phase="Failed")
        value["status"]["conditions"] = [{"type": "SyncError", "message": "invalid old release"}]
        self.assertFalse(argo.evaluate(value, B, "healthy"))
        with self.assertRaisesRegex(RuntimeError, "invalid old release"):
            argo.evaluate(value, A, "healthy")

    def test_recovery_waits_for_previous_sync_error_to_clear(self):
        value = state(B, phase="Running")
        value["status"]["conditions"] = [{"type": "SyncError", "message": "old rejected release"}]
        self.assertFalse(argo.evaluate(value, B, "healthy"))
        value["status"]["operationState"]["phase"] = "Succeeded"
        self.assertFalse(argo.evaluate(value, B, "healthy"))
        value["status"]["conditions"] = []
        self.assertTrue(argo.evaluate(value, B, "healthy"))

    def test_negative_requires_explicit_deployment_rejection_at_exact_revision(self):
        value = state(phase="Failed", sync="OutOfSync")
        self.assertFalse(argo.evaluate(value, A, "rejected"))
        value["status"]["operationState"]["syncResult"]["resources"] = [{
            "kind": "Deployment", "name": "platform-demo", "status": "SyncFailed",
            "message": "containerPort is out of range"}]
        self.assertTrue(argo.evaluate(value, A, "rejected"))
        self.assertFalse(argo.evaluate(value, B, "rejected"))
        with self.assertRaisesRegex(AssertionError, "unexpectedly synced"):
            argo.evaluate(state(), A, "rejected")

    def test_negative_timeout_is_failure_not_success(self):
        with self.assertRaises(TimeoutError):
            argo.wait_application(lambda: state(phase="Running"), A, "rejected", timeout=0)

    def test_drift_requires_expected_git_revision_and_out_of_sync(self):
        value = state(sync="OutOfSync")
        self.assertTrue(argo.evaluate(value, A, "drift"))
        self.assertFalse(argo.evaluate(value, B, "drift"))
        self.assertFalse(argo.evaluate(state(), A, "drift"))

    def test_auth_denial_requires_both_expected_exit_and_explicit_no(self):
        self.assertTrue(argo.authorization_answer(0, "yes\n"))
        self.assertFalse(argo.authorization_answer(1, "no\n"))
        for code, answer in ((1, ""), (1, "connection refused"), (0, "no"), (1, "yes"), (2, "no")):
            with self.assertRaises(RuntimeError):
                argo.authorization_answer(code, answer)

    def test_sync_is_exact_manual_non_pruning_non_forced(self):
        self.assertEqual(argo.sync_patch(A)["operation"]["sync"],
                         {"revision": A, "prune": False, "syncStrategy": {"apply": {"force": False}}})
        for revision in ("main", A[:7], "../main"):
            with self.assertRaises(ValueError):
                argo.sync_patch(revision)

    def test_wait_ignores_stale_success_and_returns_only_new_success(self):
        values = iter([state(A), state(B, phase="Running"), state(B)])
        result = argo.wait_application(lambda: next(values), B, sleep=lambda _: None)
        self.assertEqual(result["status"]["sync"]["revision"], B)


class GitFixtureTests(unittest.TestCase):
    def test_gateway_rejects_broad_or_ambiguous_bindings(self):
        self.assertEqual(argo.bridge_gateway([{"Gateway": "172.18.0.1"}]), "172.18.0.1")
        for values in ([{"Gateway": "0.0.0.0"}], [{"Gateway": "127.0.0.1"}],
                       [{"Gateway": "8.8.8.8"}], [{"Gateway": "::1"}],
                       [{"Gateway": "172.18.0.1"}, {"Gateway": "172.19.0.1"}]):
            with self.assertRaises(ValueError):
                argo.bridge_gateway(values)

    def test_real_http_clone_promotion_and_git_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = argo.GitFixture(root / "server", "127.0.0.1", run)
            try:
                release = root / "release"
                release.mkdir()
                (release / "manifest.yaml").write_text("initial release bytes\n")
                (release / "kustomization.yaml").write_text("resources: [manifest.yaml]\n")
                fixture.replace("aks01", release)
                fixture.replace("aks02", release)
                first = fixture.commit("Initial fixture")
                clone = root / "client"
                run(["git", "clone", "--quiet", fixture.url, clone], timeout=15)
                self.assertEqual(run(["git", "-C", clone, "rev-parse", "HEAD"], capture=True), first)
                (release / "manifest.yaml").write_text("second release bytes\n")
                fixture.replace("aks02", release)
                updated = fixture.commit("Inactive promotion")
                run(["git", "-C", clone, "pull", "--quiet", "--ff-only"], timeout=15)
                self.assertEqual(run(["git", "-C", clone, "rev-parse", "HEAD"], capture=True), updated)
                self.assertEqual((clone / "gitops/releases/pprd/uks/aks01/manifest.yaml").read_text(),
                                 "initial release bytes\n")
                (release / "manifest.yaml").write_text("initial release bytes\n")
                fixture.replace("aks02", release)
                rollback = fixture.commit("Explicit Git rollback")
                self.assertNotEqual(rollback, first)
                run(["git", "-C", clone, "pull", "--quiet", "--ff-only"], timeout=15)
                self.assertEqual((clone / "gitops/releases/pprd/uks/aks02/manifest.yaml").read_text(),
                                 "initial release bytes\n")
            finally:
                fixture.close()

    def test_fixture_refuses_symlink_or_unexpected_materialized_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = argo.GitFixture(root / "server", "127.0.0.1", run)
            try:
                release = root / "release"
                release.mkdir()
                (release / "private-key.pem").write_text("not a valid release file")
                with self.assertRaises(ValueError):
                    fixture.replace("aks01", release)
                (release / "private-key.pem").unlink()
                (release / "manifest.yaml").symlink_to(root / "outside")
                with self.assertRaises(ValueError):
                    fixture.replace("aks01", release)
                with self.assertRaises(ValueError):
                    fixture.slot_path("../escape")
            finally:
                fixture.close()


class MutationTests(unittest.TestCase):
    def test_invalid_desired_state_is_port_validation_error_not_ignored_replica(self):
        import yaml
        original = yaml.safe_load((SCRIPTS.parent / "deploy/base/deployment.yaml").read_text())
        result = yaml.safe_load(argo.invalid_deployment(yaml.safe_dump(original)))
        expected = copy.deepcopy(original)
        expected["spec"]["template"]["spec"]["containers"][0]["ports"][0]["containerPort"] = 70000
        self.assertEqual(result, expected)
        self.assertEqual(result["spec"]["replicas"], 2)

    def test_live_drift_patch_targets_existing_slot_only(self):
        import yaml
        obj = yaml.safe_load((SCRIPTS.parent / "deploy/base/deployment.yaml").read_text())
        self.assertEqual(argo.drift_patch(obj), [{"op": "replace",
            "path": "/spec/template/spec/containers/0/env/1/value", "value": "local"}])
        obj["spec"]["template"]["spec"]["containers"][0]["env"] = []
        with self.assertRaises(ValueError):
            argo.drift_patch(obj)

    def test_diagnostics_redact_keys_and_tokens(self):
        self.assertNotIn("secret", argo.sanitize_diagnostic(
            "-----BEGIN PRIVATE KEY-----secret-----END PRIVATE KEY-----"))
        self.assertNotIn("abc", argo.sanitize_diagnostic("Authorization: Bearer abc"))
        self.assertLessEqual(len(argo.sanitize_diagnostic("x" * 300000)), 200000)


class OrchestrationTests(unittest.TestCase):
    def test_create_application_applies_only_argo_resources(self):
        obj = object.__new__(harness.ArgoAcceptance)
        obj.git = Mock(url="http://172.18.0.1:43210/releases.git")
        obj.gitops = Mock()
        obj.gitops.project.return_value = {"kind": "AppProject", "spec": {"sourceRepos": []}}
        obj.gitops.application.return_value = {"kind": "Application", "spec": {
            "source": {"path": "gitops/releases/pprd/uks/aks01", "repoURL": ""}}}
        obj.kubectl = Mock()
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            (bundle / "release.json").write_text(json.dumps({"target": {"slot": "aks01"}}))
            obj.create_application("only-test-cluster", "aks01", bundle)
        objects = [json.loads(call.kwargs["input"]) for call in obj.kubectl.call_args_list]
        self.assertEqual([value["kind"] for value in objects], ["AppProject", "Application"])
        self.assertEqual(objects[1]["spec"]["source"]["repoURL"], obj.git.url)
        supplied = obj.gitops.application.call_args.args[0]["repository_url"]
        self.assertTrue(supplied.startswith("https://"))

    def test_rbac_probe_uses_controller_impersonation_and_records_actual_denials(self):
        from types import SimpleNamespace
        obj = object.__new__(harness.ArgoAcceptance)
        obj.args, obj.kubeconfig, obj.record = SimpleNamespace(kubectl="kubectl"), Path("/tmp/test-kubeconfig"), {}
        def response(command, **kwargs):
            resource = command[command.index("can-i") + 2]
            allowed = resource in ("deployments.apps", "httproutes.gateway.networking.k8s.io",
                                   "secretproviderclasses.secrets-store.csi.x-k8s.io") and command[-1] == "platform-demo"
            return SimpleNamespace(returncode=0 if allowed else 1, stdout="yes\n" if allowed else "no\n")
        with patch.object(harness.subprocess, "run", side_effect=response) as invoke:
            obj.check_controller_rbac("isolated-test")
        self.assertEqual(len(obj.record["rbac_checks"]), 12)
        self.assertEqual(sum(value["allowed"] for value in obj.record["rbac_checks"]), 6)
        for call in invoke.call_args_list:
            self.assertIn("system:serviceaccount:argocd:argocd-application-controller", call.args[0])
            self.assertIn("kind-isolated-test", call.args[0])
        with patch.object(harness.subprocess, "run",
                          return_value=SimpleNamespace(returncode=0, stdout="yes")):
            with self.assertRaisesRegex(AssertionError, "RBAC differs"):
                obj.check_controller_rbac("isolated-test")

    def test_sync_rejects_automated_or_concurrent_operations(self):
        obj = object.__new__(harness.ArgoAcceptance)
        for application in ({"operation": {"sync": {}}},
                            {"spec": {"syncPolicy": {"automated": {}}}},
                            {"spec": {"syncPolicy": {"automated": {"enabled": False}}}},
                            {"spec": {"syncPolicy": {"automated": {"selfHeal": True}}}}):
            obj.fetch_application = Mock(return_value=application)
            obj.kubectl, obj.refresh = Mock(), Mock()
            with self.assertRaises(ValueError):
                obj.sync("cluster", "aks02", A, label="test")
            obj.kubectl.assert_not_called()

    def test_cleanup_closes_git_server_even_if_base_cleanup_fails(self):
        obj = object.__new__(harness.ArgoAcceptance)
        obj.git = Mock()
        with patch.object(harness.Acceptance, "cleanup", side_effect=OSError("fixture failure")):
            with self.assertRaises(OSError):
                obj.cleanup()
        obj.git.close.assert_called_once()


    def test_metrics_manifest_is_hash_bound_and_only_test_platform_is_applied(self):
        import yaml
        from types import SimpleNamespace
        data = yaml.safe_dump({"apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": "metrics-server", "namespace": "kube-system"},
            "spec": {"template": {"spec": {"containers": [{"name": "metrics-server",
                "image": "registry.k8s.io/metrics-server/metrics-server:v0.9.0",
                "args": ["--secure-port=10250"]}]}}}}).encode()
        obj = object.__new__(harness.ArgoAcceptance)
        obj.clusters, obj.record, obj.kubectl = ["isolated-one", "isolated-two"], {}, Mock()
        with tempfile.TemporaryDirectory() as directory:
            obj.work = root = Path(directory)
            pins = {"url": "https://example.test/components.yaml", "sha256": hashlib.sha256(data).hexdigest(),
                    "image": "registry.k8s.io/metrics-server/metrics-server:v0.9.0@sha256:" + "a" * 64}
            (root / "argocd-ci-tools.json").write_text(json.dumps({"metrics_server": pins}))
            response = Mock()
            response.url, response.read.return_value = pins["url"], data
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            with patch.object(harness, "ROOT", root), patch.object(harness, "urlopen", return_value=response):
                obj.install_metrics()
            manifest = yaml.safe_load((root / "kind-metrics-server.yaml").read_text())
            container = manifest["spec"]["template"]["spec"]["containers"][0]
            self.assertEqual(container["image"], pins["image"])
            self.assertIn("--kubelet-insecure-tls", container["args"])
            calls = obj.kubectl.call_args_list
            self.assertEqual([call.args[0] for call in calls], ["isolated-one"] * 3 + ["isolated-two"] * 3)
            obj.kubectl.reset_mock()
            response.read.return_value = data + b"# changed"
            with patch.object(harness, "ROOT", root), patch.object(harness, "urlopen", return_value=response):
                with self.assertRaisesRegex(ValueError, "checksum"):
                    obj.install_metrics()
            obj.kubectl.assert_not_called()

    def test_no_application_apply_in_argo_execute(self):
        # Regression guard for an accidentally inherited direct deployment path.
        import inspect
        source = inspect.getsource(harness.ArgoAcceptance.execute)
        self.assertNotIn("apply_and_check", source)
        self.assertNotIn('"apply"', source)
        self.assertIn('self.check_release(self.clusters[0]', source)


if __name__ == "__main__":
    unittest.main()
