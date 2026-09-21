"""Local contract tests cannot substitute for running the qualifier on real AKS."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("qualify_azure_workload", ROOT / "scripts/qualify_azure_workload.py")
qualify = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qualify)


class AzureWorkloadProfile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.delivery = json.loads((ROOT / "delivery.azure-workload.apps.json").read_text())
        cls.workload = json.loads((ROOT / "azure.workload.json").read_text())
        cls.rendered = {}
        for target in cls.delivery["targets"]:
            text = subprocess.check_output(
                [os.environ.get("KUBECTL_BIN", "kubectl"), "kustomize", str(ROOT / target["overlay"])], text=True)
            cls.rendered[target["slot"]] = list(yaml.safe_load_all(text))

    def test_real_render_preserves_gateway_tls_and_adds_file_only_app_secret_on_both_slots(self):
        for slot, objects in self.rendered.items():
            target, expected = qualify.select(self.delivery, self.workload, "pprd", "uks", slot)
            account = next(x for x in objects if x["kind"] == "ServiceAccount")
            provider = next(x for x in objects if x["kind"] == "SecretProviderClass"
                            and x["metadata"]["name"] == expected["secret_provider_class"])
            qualify.validate_binding(account, provider, expected, target["namespace"])
            deployment = next(x for x in objects if x["kind"] == "Deployment")
            pod = deployment["spec"]["template"]["spec"]
            qualify.validate_mount(pod, expected)
            self.assertEqual(len(pod["volumes"]), 2)
            self.assertEqual(sum(x["kind"] == "SecretProviderClass" for x in objects), 2)
            self.assertTrue(any(x["kind"] == "HTTPRoute" for x in objects))
            self.assertFalse(any(x["kind"] in ("Secret", "Gateway", "Namespace") for x in objects))
            env = {x["name"]: x["value"] for x in qualify.container(pod)["env"]}
            self.assertEqual(env["APP_SLOT"], slot)
            self.assertEqual(env["APP_REQUIRED_SECRET_FILE"], "/mnt/app-secrets/qualification")

    def test_key_vault_names_follow_azure_rules_instead_of_kubernetes_labels(self):
        expected = copy.deepcopy(self.workload)
        expected["targets"][0]["secret_name"] = "Qualification-" + "X" * 110
        expected["targets"][0]["vault_name"] = "Example-Platform-App"
        _, selected = qualify.select(self.delivery, expected, "pprd", "uks", "aks01")
        self.assertEqual(selected["secret_name"], "Qualification-" + "X" * 110)
        for invalid in ("has/slash", "x" * 128, ""):
            expected["targets"][0]["secret_name"] = invalid
            with self.assertRaises(ValueError):
                qualify.select(self.delivery, expected, "pprd", "uks", "aks01")

    def test_qualification_callers_use_guard_token_isolated_login_and_exact_release_source(self):
        for flow in ("build-deploy", "promote"):
            data = yaml.safe_load((ROOT / f"examples/delivery/github-azure-workload-{flow}.yml").read_text())
            job = data["jobs"]["qualify"]
            steps = job["steps"]
            guard = next(s for s in steps if "ci_guard.py github --latest" in s.get("run", ""))
            self.assertEqual(guard["env"]["GH_TOKEN"], "${{ github.token }}")
            isolation = next(i for i, s in enumerate(steps) if "mktemp -d" in s.get("run", ""))
            login = next(i for i, s in enumerate(steps) if s.get("uses", "").startswith("azure/login@"))
            self.assertLess(isolation, login)
            self.assertEqual(steps[login]["env"]["AZURE_LOGIN_POST_CLEANUP"], "false")
            cleanup = steps[-1]
            self.assertIn("always()", cleanup["if"])
            self.assertIn("profile.resolve().parent != parent", cleanup["run"])
            live = next(s for s in steps if "scripts/qualify_azure_workload.py" in s.get("run", ""))
            self.assertIn("--acquire-kubeconfig", live["run"])
            self.assertNotIn("--admin", live["run"])
            self.assertIn("source-commit" if flow == "build-deploy" else "source_commit",
                          live["env"]["SOURCE_COMMIT"])
            if flow == "promote":
                self.assertIn("../selected-source/azure.workload.json", live["run"])
                self.assertTrue(any("templates/scripts/releases.py github" in s.get("run", "") for s in steps))
            azure = (ROOT / f"examples/delivery/azure-azure-workload-{flow}.yml").read_text()
            self.assertIn("scripts/qualify_azure_workload.py", azure)
            self.assertIn("$(selectedSourceCommit)", azure)
            self.assertIn("azure-devops --latest", azure)
            if flow == "promote":
                self.assertIn("PromoteApplication_Release.Resolve.outputs['release.sourceCommit']", azure)
                self.assertIn('revision + ":" + filename', azure)

    def test_both_hosts_full_build_deploy_and_promote_callers_use_azure_profile(self):
        for host in ("github", "azure"):
            for flow in ("build-deploy", "promote"):
                path = ROOT / f"examples/delivery/{host}-azure-workload-{flow}.yml"
                data = yaml.safe_load(path.read_text())
                self.assertIsInstance(data, dict)
                self.assertIn("delivery.azure-workload.apps.json", path.read_text())
                self.assertNotIn("delivery.gateway.apps.json", path.read_text())


class QualificationContract(unittest.TestCase):
    def setUp(self):
        self.target = {
            "subscription_id": "00000000-0000-0000-0000-000000000003",
            "resource_group": "uks-pprd-example-aks-rg", "cluster_name": "uks-pprd-example-aks01",
            "namespace": "platform-demo", "deployment": "platform-demo"}
        self.expected = {"tenant_id": "00000000-0000-0000-0000-000000000001",
                         "client_id": "00000000-0000-0000-0000-000000000201",
                         "service_account": "platform-demo", "vault_name": "example-platform-app",
                         "secret_name": "platform-demo-qualification",
                         "secret_provider_class": "platform-demo-app-secret"}
        self.cluster = {"id": "/subscriptions/" + self.target["subscription_id"] +
                        "/resourceGroups/" + self.target["resource_group"] +
                        "/providers/Microsoft.ContainerService/managedClusters/" + self.target["cluster_name"],
                        "privateFqdn": "selected.privatelink.uksouth.azmk8s.io",
                        "aadProfile": {"tenantId": self.expected["tenant_id"]},
                        "oidcIssuerProfile": {"enabled": True},
                        "securityProfile": {"workloadIdentity": {"enabled": True}},
                        "addonProfiles": {"azureKeyvaultSecretsProvider": {"enabled": True}}}
        self.kube = {"server": "https://" + self.cluster["privateFqdn"],
                     "certificate-authority-data": "test-ca"}
        account = yaml.safe_load((ROOT / "deploy/gateway-api/base/service-account.yaml").read_text())
        account["metadata"]["namespace"] = "platform-demo"
        self.account = account
        provider = yaml.safe_load((ROOT / "deploy/azure-workload/base/application-secret-provider-class.yaml").read_text())
        provider["metadata"]["namespace"] = "platform-demo"
        self.provider = provider
        self.pod_spec = {
            "serviceAccountName": "platform-demo",
            "containers": [{"name": "app", "image": "example.azurecr.io/app@sha256:" + "a" * 64,
                            "env": [{"name": key, "value": value} for key, value in {
                                "APP_REQUIRED_SECRET_FILE": qualify.FILE,
                                "AZURE_CLIENT_ID": self.expected["client_id"],
                                "AZURE_TENANT_ID": self.expected["tenant_id"],
                                "AZURE_FEDERATED_TOKEN_FILE": "/var/run/secrets/azure/tokens/azure-identity-token"}.items()],
                            "volumeMounts": [{"name": "app-secret", "mountPath": qualify.MOUNT, "readOnly": True}]}],
            "volumes": [{"name": "app-secret", "csi": {"driver": "secrets-store.csi.k8s.io", "readOnly": True,
                          "volumeAttributes": {"secretProviderClass": "platform-demo-app-secret"}}}]}
        self.deployment = {"metadata": {"uid": "deployment-id", "generation": 2},
                           "spec": {"replicas": 1, "template": {"spec": copy.deepcopy(self.pod_spec)}},
                           "status": {"observedGeneration": 2, "updatedReplicas": 1, "replicas": 1,
                                      "availableReplicas": 1, "readyReplicas": 1}}
        self.replica_sets = [{"metadata": {"uid": "replica-id", "ownerReferences": [
            {"kind": "Deployment", "uid": "deployment-id", "controller": True}]}}]
        self.pods = [{"metadata": {"uid": "pod-id", "name": "demo-pod", "namespace": "platform-demo",
                                  "labels": {"azure.workload.identity/use": "true"}, "ownerReferences": [
                                      {"kind": "ReplicaSet", "uid": "replica-id", "controller": True}]},
                      "spec": copy.deepcopy(self.pod_spec),
                      "status": {"conditions": [{"type": "Ready", "status": "True"}]}}]
        self.statuses = [{"metadata": {"namespace": "platform-demo", "ownerReferences": [
            {"kind": "Pod", "uid": "pod-id"}]}, "status": {
                "podName": "demo-pod", "secretProviderClassName": "platform-demo-app-secret",
                "mounted": True, "targetPath": "/var/lib/kubelet/pods/pod-id/volumes/actual/mount",
                "objects": [{"id": "secret/platform-demo-qualification", "version": "a" * 32}]}}]

    def verify_resources(self):
        return qualify.validate_resources(self.deployment, self.replica_sets, self.pods, self.statuses,
                                          self.expected, "platform-demo")

    def test_pass_binds_real_cluster_identity_mount_owner_and_current_secret_version(self):
        qualify.validate_cluster(self.cluster, self.kube, self.target, self.expected)
        qualify.validate_binding(self.account, self.provider, self.expected, "platform-demo")
        image, pods = self.verify_resources()
        self.assertEqual(pods, [{"pod": "demo-pod", "pod_uid": "pod-id", "secret_version": "a" * 32}])
        self.assertIn("@sha256:", image)

    def test_wrong_cluster_and_tls_bypass_are_rejected(self):
        for mutation in (
            lambda: self.kube.update(server="https://another.privatelink.uksouth.azmk8s.io"),
            lambda: self.kube.update({"insecure-skip-tls-verify": True}),
            lambda: self.cluster["securityProfile"]["workloadIdentity"].update(enabled=False),
        ):
            self.setUp()
            mutation()
            with self.assertRaises(ValueError):
                qualify.validate_cluster(self.cluster, self.kube, self.target, self.expected)

    def test_wrong_workload_identity_and_replaced_csi_authentication_are_rejected(self):
        for mutation in (
            lambda: self.account["metadata"]["annotations"].update({"azure.workload.identity/client-id": "wrong"}),
            lambda: self.provider["spec"]["parameters"].update(useVMManagedIdentity="true"),
            lambda: self.provider["spec"]["parameters"].update(keyvaultName="other-vault"),
            lambda: self.provider["spec"].update(secretObjects=[{"secretName": "unwanted-copy"}]),
        ):
            self.setUp()
            mutation()
            with self.assertRaises(ValueError):
                qualify.validate_binding(self.account, self.provider, self.expected, "platform-demo")

    def test_stale_or_unmounted_or_wrong_pod_status_cannot_qualify(self):
        for mutation in (
            lambda: self.statuses[0]["status"].update(mounted=False),
            lambda: self.statuses[0]["metadata"]["ownerReferences"][0].update(uid="old-pod"),
            lambda: self.statuses[0]["status"]["objects"][0].update(id="secret/another-secret"),
            lambda: self.statuses[0]["status"]["objects"][0].update(version=""),
            lambda: self.statuses[0]["status"].update(targetPath="/var/lib/kubelet/pods/old-pod/volumes/x"),
            lambda: self.deployment["status"].update(observedGeneration=1),
            lambda: self.pods[0]["status"]["conditions"][0].update(status="False"),
            lambda: self.pods[0]["spec"]["volumes"][0].update(csi=None),
            lambda: self.pods[0]["spec"]["containers"][0]["volumeMounts"][0].update(subPath="qualification"),
        ):
            self.setUp()
            mutation()
            with self.assertRaises(ValueError):
                self.verify_resources()

    def test_secret_dependent_readiness_revision_and_slot_must_all_match(self):
        version = {"application": "aks-platform-demo", "revision": "a" * 40, "slot": "aks01"}
        qualify.verify_response({"status": "ready"}, version, "a" * 40, "aks01")
        for readiness, actual, revision, slot in (
            ({"status": "dependency_unavailable"}, version, "a" * 40, "aks01"),
            ({"status": "ready"}, version, "b" * 40, "aks01"),
            ({"status": "ready"}, version, "a" * 40, "aks02"),
        ):
            with self.assertRaises(ValueError):
                qualify.verify_response(readiness, actual, revision, slot)


if __name__ == "__main__":
    unittest.main()
