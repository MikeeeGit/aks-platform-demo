"""Render local Kustomize overlays and verify the app/network/security contract.

No API server, Azure identity, container runtime or Kubernetes context is used.
"""
from pathlib import Path
import json
import os
import subprocess
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
KUBECTL = os.environ.get("KUBECTL_BIN", "kubectl")


class ManifestContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = json.loads((ROOT / "delivery.apps.json").read_text())
        cls.rendered = {}
        for target in cls.config["targets"]:
            text = subprocess.check_output(
                [KUBECTL, "kustomize", str(ROOT / target["overlay"])], text=True
            )
            cls.rendered[target["slot"]] = (target, list(yaml.safe_load_all(text)))

    def test_both_slot_targets_render_namespace_scoped_objects(self):
        self.assertEqual(set(self.rendered), {"aks01", "aks02"})
        for target, objects in self.rendered.values():
            self.assertEqual({obj["kind"] for obj in objects}, {"Service", "Deployment", "PodDisruptionBudget"})
            for obj in objects:
                self.assertEqual(obj["metadata"]["namespace"], target["namespace"])
            deployment = next(obj for obj in objects if obj["kind"] == "Deployment")
            self.assertEqual(deployment["metadata"]["name"], target["deployment"])

    def test_private_load_balancers_match_gateway_network_contract(self):
        addresses = {"aks01": "10.81.0.20", "aks02": "10.81.4.20"}
        for slot, (_, objects) in self.rendered.items():
            service = next(obj for obj in objects if obj["kind"] == "Service")
            annotations = service["metadata"]["annotations"]
            self.assertEqual(service["spec"]["type"], "LoadBalancer")
            self.assertEqual(annotations["service.beta.kubernetes.io/azure-load-balancer-internal"], "true")
            self.assertEqual(annotations["service.beta.kubernetes.io/azure-load-balancer-ipv4"], addresses[slot])
            self.assertEqual(annotations["service.beta.kubernetes.io/azure-load-balancer-internal-subnet"], "uks-pprd-" + slot)
            self.assertNotIn("loadBalancerIP", service["spec"])
            self.assertEqual(service["spec"]["loadBalancerSourceRanges"], ["10.81.8.0/24", "10.80.2.0/24"])
            self.assertEqual(service["spec"]["ports"][0]["port"], 80)
            self.assertEqual(service["spec"]["ports"][0]["targetPort"], "http")
            target = self.config["targets"][0 if slot == "aks01" else 1]
            self.assertEqual(target["verification"], {"service": "platform-demo", "port": 80, "readiness_path": "/readyz", "version_path": "/version"})

    def test_namespace_bootstrap_is_operator_owned_and_restricted(self):
        namespace = yaml.safe_load((ROOT / "deploy/bootstrap/namespace.yaml").read_text())
        self.assertEqual(namespace["kind"], "Namespace")
        self.assertEqual(namespace["metadata"]["name"], "platform-demo")
        for mode in ("enforce", "audit", "warn"):
            self.assertEqual(namespace["metadata"]["labels"]["pod-security.kubernetes.io/" + mode], "restricted")
        # The app render test separately rejects Namespace in either deployable overlay.

    def test_runtime_security_health_and_slot_are_wired(self):
        images = set()
        for slot, (_, objects) in self.rendered.items():
            deployment = next(obj for obj in objects if obj["kind"] == "Deployment")
            pod = deployment["spec"]["template"]["spec"]
            app = pod["containers"][0]
            env = {item["name"]: item["value"] for item in app["env"]}
            self.assertEqual(env["APP_SLOT"], slot)
            self.assertEqual(env["PORT"], "8080")
            self.assertEqual(app["ports"][0]["containerPort"], 8080)
            self.assertEqual(app["readinessProbe"]["httpGet"]["path"], "/readyz")
            self.assertEqual(app["livenessProbe"]["httpGet"]["path"], "/healthz")
            self.assertTrue(pod["securityContext"]["runAsNonRoot"])
            self.assertEqual(pod["securityContext"]["runAsUser"], 10001)
            self.assertFalse(pod["automountServiceAccountToken"])
            self.assertEqual(pod["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")
            self.assertTrue(app["securityContext"]["readOnlyRootFilesystem"])
            self.assertFalse(app["securityContext"]["allowPrivilegeEscalation"])
            self.assertIn("ALL", app["securityContext"]["capabilities"]["drop"])
            self.assertEqual(deployment["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"], 0)
            images.add(app["image"])
        self.assertEqual(images, {"aks-platform-demo:unbuilt"})


if __name__ == "__main__":
    unittest.main()
