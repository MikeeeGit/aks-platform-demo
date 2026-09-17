#!/usr/bin/env python3
"""Run real Argo CD reconciliation in two disposable kind clusters.

Application manifests are applied only by Argo controllers from an isolated Git
server. Platform bootstrap and an intentional live-drift patch are test-owned.
Requires Linux Docker, pinned kind/kubectl/Helm and the shared template checkout.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
import acceptance_argocd as argo
from test_dual_cluster import Acceptance, ROOT, run


class ArgoAcceptance(Acceptance):
    def __init__(self, args, work):
        super().__init__(args, work)
        self.git = None
        self.record.update(kind="local-argocd-kubernetes-acceptance", argocd_checks=[], git_revisions=[])
        self.record["qualification_limits"] = [
            "No Azure identity, internal LoadBalancer, CSI or DNS-cutover qualification",
            "kind default CNI does not prove NetworkPolicy enforcement",
            "Evaluation Argo profile; production HA and external Git authentication are separate",
            "Metrics-server kubelet certificate verification is disabled only in disposable kind",
            "Runtime fixtures do not produce a scanned production build receipt",
        ]
        sys.path.insert(0, str(args.templates / "scripts"))
        self.gitops = importlib.import_module("gitops")

    def install_metrics(self):
        import yaml
        pins = json.loads((ROOT / "argocd-ci-tools.json").read_text())["metrics_server"]
        with urlopen(pins["url"], timeout=60) as response:
            if not response.url.startswith("https://"):
                raise ValueError("Metrics manifest redirected away from HTTPS.")
            data = response.read(2_000_001)
        if len(data) > 2_000_000 or hashlib.sha256(data).hexdigest() != pins["sha256"]:
            raise ValueError("Metrics-server upstream manifest checksum mismatch.")
        objects = list(yaml.safe_load_all(data))
        deployments = [obj for obj in objects if obj and obj["kind"] == "Deployment"
                       and obj["metadata"]["name"] == "metrics-server"]
        if len(deployments) != 1:
            raise ValueError("Expected one metrics-server Deployment.")
        containers = deployments[0]["spec"]["template"]["spec"]["containers"]
        if len(containers) != 1 or containers[0]["image"] != pins["image"].split("@")[0]:
            raise ValueError("Unexpected metrics-server upstream image.")
        containers[0]["image"] = pins["image"]
        # kind kubelet serving certificates are not trusted by upstream metrics-server.
        # This never changes public application YAML or any real AKS setting.
        containers[0]["args"].append("--kubelet-insecure-tls")
        path = self.work / "kind-metrics-server.yaml"
        path.write_text(yaml.safe_dump_all(objects, sort_keys=False))
        self.record["metrics_server"] = pins
        for cluster in self.clusters:
            self.kubectl(cluster, "apply", "--server-side", "-f", path)
            self.kubectl(cluster, "-n", "kube-system", "rollout", "status",
                         "deployment/metrics-server", "--timeout=180s")
            self.kubectl(cluster, "wait", "--for=condition=Available",
                         "apiservice/v1beta1.metrics.k8s.io", "--timeout=180s")

    def prepare_argo(self):
        self.prepare()
        self.install_metrics()
        network = json.loads(run(["docker", "network", "inspect", "kind", "--format",
                                  "{{json .IPAM.Config}}"], capture=True))
        self.git = argo.GitFixture(self.work / "git-fixture", argo.bridge_gateway(network), run)
        self.record["test_git_transport"] = {
            "protocol": "isolated HTTP", "bind_address": self.git.server.server_address[0],
            "port": self.git.port, "public_production_configs_unchanged": True}
        bundle = self.work / "argocd-bootstrap"
        script = self.args.templates / "scripts/argocd_bootstrap.py"
        run([sys.executable, script, "prepare", "--output", bundle,
             "--git-egress-port", str(self.git.port)])
        receipt = bundle / "bootstrap.json"
        receipt_sha = hashlib.sha256(receipt.read_bytes()).hexdigest()
        self.record["argocd_bootstrap"] = json.loads(receipt.read_text())
        for cluster in self.clusters:
            run([sys.executable, script, "apply", "--output", bundle,
                 "--receipt-sha256", receipt_sha, "--kubeconfig", self.kubeconfig,
                 "--context", "kind-" + cluster, "--timeout", "600", "--yes"],
                env=dict(os.environ, KUBECTL_BIN=str(self.args.kubectl)), timeout=1500)
            self.check_controller_rbac(cluster)

    def check_controller_rbac(self, cluster):
        subject = "system:serviceaccount:argocd:argocd-application-controller"
        checks = [(verb, resource, "platform-demo", True)
                  for verb in ("create", "patch")
                  for resource in ("deployments.apps", "httproutes.gateway.networking.k8s.io",
                                   "secretproviderclasses.secrets-store.csi.x-k8s.io")]
        checks += [("create", resource, namespace, False) for resource, namespace in (
            ("secrets", "platform-demo"), ("roles.rbac.authorization.k8s.io", "platform-demo"),
            ("gateways.gateway.networking.k8s.io", "platform-demo"), ("deployments.apps", "default"),
            ("namespaces", None), ("clusterroles.rbac.authorization.k8s.io", None))]
        for verb, resource, namespace, expected in checks:
            command = [str(self.args.kubectl), "--kubeconfig", str(self.kubeconfig),
                       "--context", "kind-" + cluster, "--as", subject, "auth", "can-i", verb, resource]
            if namespace:
                command += ["--namespace", namespace]
            result = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=30, check=False)
            allowed = argo.authorization_answer(result.returncode, result.stdout)
            self.record.setdefault("rbac_checks", []).append({
                "cluster": cluster, "subject": subject, "verb": verb, "resource": resource,
                "namespace": namespace, "expected_allowed": expected, "allowed": allowed})
            if allowed != expected:
                raise AssertionError(f"Argo RBAC differs from intended scope: {verb} {resource} in {namespace}.")
        print("PASS namespace-scoped Argo controller authorization " + cluster, flush=True)

    def materialize(self, bundle, name):
        output = self.work / name
        self.gitops.materialize(bundle, hashlib.sha256((bundle / "release.json").read_bytes()).hexdigest(), output)
        if (output / "manifest.yaml").read_bytes() != (bundle / "manifest.yaml").read_bytes():
            raise ValueError("GitOps materialization changed the validated manifest.")
        return output

    def commit_git(self, message):
        revision = self.git.commit(message)
        self.record["git_revisions"].append({"step": message, "revision": revision})
        return revision

    def application_name(self, slot):
        return "platform-demo-pprd-uks-" + slot

    def create_application(self, cluster, slot, bundle):
        target = json.loads((bundle / "release.json").read_text())["target"]
        config = {"schema_version": 1, "repository_url": "https://example.test/platform-gitops.git",
                  "revision": "main", "argo_namespace": "argocd", "project": "platform-demo",
                  "application_prefix": "platform-demo"}
        project = self.gitops.project(config, target)
        application = self.gitops.application(config, target)
        # Only this disposable test uses unauthenticated bridge HTTP. The production
        # config validator remains HTTPS-only, and source/path/resource scope is retained.
        project["spec"]["sourceRepos"] = [self.git.url]
        application["spec"]["source"]["repoURL"] = self.git.url
        for resource in (project, application):
            self.kubectl(cluster, "apply", "-f", "-", input=json.dumps(resource))

    def fetch_application(self, cluster, slot):
        return json.loads(self.kubectl(cluster, "-n", "argocd", "get", "application",
                                       self.application_name(slot), "-o", "json", capture=True))

    def refresh(self, cluster, slot):
        self.kubectl(cluster, "-n", "argocd", "annotate", "application", self.application_name(slot),
                     "argocd.argoproj.io/refresh=hard", "--overwrite")

    def sync(self, cluster, slot, revision, *, expectation="healthy", label):
        application = self.fetch_application(cluster, slot)
        if (application.get("operation") or
                application.get("spec", {}).get("syncPolicy", {}).get("automated") is not None):
            raise ValueError("Acceptance requires idle manual Argo synchronization.")
        self.refresh(cluster, slot)
        self.kubectl(cluster, "-n", "argocd", "patch", "application", self.application_name(slot),
                     "--type=merge", "-p", json.dumps(argo.sync_patch(revision)))
        value = argo.wait_application(lambda: self.fetch_application(cluster, slot), revision,
                                     expectation, timeout=600)
        summary = dict(argo.status_summary(value), step=label, slot=slot, expected_git_revision=revision)
        self.record["argocd_checks"].append(summary)
        print("PASS Argo " + label + " " + slot + " " + revision, flush=True)
        return value

    def updated_source(self):
        package = json.loads((self.fixture / "package.json").read_text())
        package["version"] = "0.0.0-argocd-acceptance." + self.prefix
        (self.fixture / "package.json").write_text(json.dumps(package, indent=2) + "\n")
        lock = json.loads((self.fixture / "package-lock.json").read_text())
        lock["version"] = lock["packages"][""]["version"] = package["version"]
        (self.fixture / "package-lock.json").write_text(json.dumps(lock, indent=2) + "\n")
        run(["git", "-C", self.fixture, "add", "package.json", "package-lock.json"])
        run(["git", "-C", self.fixture, "-c", "user.name=Acceptance Fixture",
             "-c", "user.email=fixture@example.test", "-c", "commit.gpgsign=false",
             "-c", "core.hooksPath=/dev/null", "commit", "-qm", "Synthetic Argo acceptance update"])
        return run(["git", "-C", self.fixture, "rev-parse", "HEAD"], capture=True)

    def check_hpa(self, cluster, slot):
        import time
        deadline = time.monotonic() + 180
        while True:
            hpa = json.loads(self.kubectl(cluster, "-n", "platform-demo", "get",
                                        "hpa/platform-demo", "-o", "json", capture=True))
            conditions = {item["type"]: item["status"] for item in hpa.get("status", {}).get("conditions", [])}
            if conditions.get("AbleToScale") == "True" and conditions.get("ScalingActive") == "True":
                self.record.setdefault("hpa_checks", []).append({
                    "slot": slot, "conditions": conditions,
                    "current_replicas": hpa["status"].get("currentReplicas"),
                    "desired_replicas": hpa["status"].get("desiredReplicas"),
                    "metrics_available": bool(hpa["status"].get("currentMetrics"))})
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("HPA has not obtained usable metrics: " + json.dumps(conditions))
            time.sleep(2)

    def execute(self):
        self.prepare_argo()
        first = self.build(self.original_commit, "initial")
        originals, bundles = {}, {}
        for slot in argo.SLOTS:
            bundles[slot] = self.render(self.original_commit, first, slot, "initial-" + slot)
            originals[slot] = self.materialize(bundles[slot], "git-initial-" + slot)
            self.git.replace(slot, originals[slot])
        initial_git = self.commit_git("Initial approved release for both slots")
        for slot, cluster in zip(argo.SLOTS, self.clusters):
            self.create_application(cluster, slot, bundles[slot])
            self.sync(cluster, slot, initial_git, label="initial")
            self.check_release(cluster, slot, self.original_commit, first, "argocd-initial")
            self.check_hpa(cluster, slot)

        updated = self.updated_source()
        second = self.build(updated, "updated")
        if second == first:
            raise ValueError("Distinct source revisions unexpectedly produced the same image.")
        update = self.render(updated, second, "aks02", "updated-aks02")
        self.git.replace("aks02", self.materialize(update, "git-updated-aks02"))
        update_git = self.commit_git("Promote candidate release to inactive slot")
        self.sync(self.clusters[1], "aks02", update_git, label="update-inactive")
        self.check_release(self.clusters[1], "aks02", updated, second, "argocd-update-inactive")
        self.check_release(self.clusters[0], "aks01", self.original_commit, first, "active-unchanged-after-update")

        # A new Git commit restores the original exact rendered files, rather than
        # applying an old manifest directly or issuing kubectl rollout undo.
        self.git.replace("aks02", originals["aks02"])
        rollback_git = self.commit_git("Roll back inactive slot to original approved release")
        self.sync(self.clusters[1], "aks02", rollback_git, label="git-rollback")
        self.check_release(self.clusters[1], "aks02", self.original_commit, first, "argocd-git-rollback")

        bad_manifest = self.git.slot_path("aks02") / "manifest.yaml"
        bad_manifest.write_text(argo.invalid_deployment(bad_manifest.read_text()))
        broken_git = self.commit_git("Intentional invalid desired Deployment acceptance case")
        self.sync(self.clusters[1], "aks02", broken_git, expectation="rejected", label="invalid-desired-state-rejected")
        # A timeout is never accepted as rejection; the exact operation must contain
        # an explicit Deployment SyncFailed. The last working release must survive.
        self.check_release(self.clusters[1], "aks02", self.original_commit, first, "rejected-release-preserves-runtime")
        self.git.replace("aks02", originals["aks02"])
        recovered_git = self.commit_git("Restore valid desired state after negative test")
        self.sync(self.clusters[1], "aks02", recovered_git, label="recover-valid-git")

        deployment = json.loads(self.kubectl(self.clusters[1], "-n", "platform-demo", "get",
                                             "deployment/platform-demo", "-o", "json", capture=True))
        self.kubectl(self.clusters[1], "-n", "platform-demo", "patch", "deployment/platform-demo",
                     "--type=json", "-p", json.dumps(argo.drift_patch(deployment)))
        self.kubectl(self.clusters[1], "-n", "platform-demo", "rollout", "status",
                     "deployment/platform-demo", "--timeout=300s")
        drifted = json.loads(self.kubectl(self.clusters[1], "-n", "platform-demo", "get",
                                          "deployment/platform-demo", "-o", "json", capture=True))
        values = [v["value"] for c in drifted["spec"]["template"]["spec"]["containers"] if c["name"] == "app"
                  for v in c["env"] if v["name"] == "APP_SLOT"]
        if values != ["local"]:
            raise ValueError("Intentional live drift was not established.")
        self.refresh(self.clusters[1], "aks02")
        drift = argo.wait_application(lambda: self.fetch_application(self.clusters[1], "aks02"),
                                     recovered_git, "drift", timeout=180)
        self.record["argocd_checks"].append(dict(argo.status_summary(drift), step="live-drift-detected",
                                                slot="aks02", expected_git_revision=recovered_git))
        self.sync(self.clusters[1], "aks02", recovered_git, label="manual-drift-reconciliation")
        self.check_release(self.clusters[1], "aks02", self.original_commit, first, "argocd-drift-repaired")
        self.check_hpa(self.clusters[1], "aks02")
        self.check_release(self.clusters[0], "aks01", self.original_commit, first, "active-slot-unchanged")
        active = argo.status_summary(self.fetch_application(self.clusters[0], "aks01"))
        if active["operation_revision"] != initial_git or active["operation_phase"] != "Succeeded":
            raise ValueError("Active slot was unexpectedly synchronized again.")
        self.record["argocd_checks"].append(dict(active, step="active-operation-unchanged",
                                                slot="aks01", expected_operation_revision=initial_git))
        self.record["result"] = "passed"

    def collect_diagnostics(self):
        # Never fetch Secrets, kubeconfig, full environment or Argo account passwords.
        for cluster in self.clusters:
            commands = [
                ("applications", ["-n", "argocd", "get", "applications", "-o", "json"]),
                ("resources", ["-n", "argocd", "get", "pods,deployments,statefulsets,services", "-o", "wide"]),
                ("pods", ["-n", "argocd", "describe", "pods"]),
                ("events", ["-n", "argocd", "get", "events", "--sort-by=.lastTimestamp"]),
                ("hpa", ["-n", "platform-demo", "get", "hpa", "-o", "json"]),
                ("metrics", ["-n", "kube-system", "logs", "deployment/metrics-server", "--tail=100"]),
            ]
            for component in ("argocd-application-controller", "argocd-repo-server", "argocd-redis", "argocd-server"):
                commands.append((component, ["-n", "argocd", "logs", "-l", "app.kubernetes.io/name=" + component,
                    "--all-containers=true", "--tail=150", "--prefix=true", "--max-log-requests=5",
                    "--ignore-errors=true", "--pod-running-timeout=5s"]))
            for label, arguments in commands:
                filename = cluster + "-argocd-" + label + ".txt"
                try:
                    result = subprocess.run(
                        [str(self.args.kubectl), "--kubeconfig", str(self.kubeconfig),
                         "--context", "kind-" + cluster, *arguments],
                        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20, check=False)
                    output, success = result.stdout or "", result.returncode == 0
                except (OSError, subprocess.SubprocessError) as error:
                    output, success = "Diagnostic failed: " + str(error), False
                safe = argo.sanitize_diagnostic(output)
                (self.artifacts / filename).write_text(safe + "\n")
                self.record.setdefault("diagnostics", []).append({"file": filename, "collected": success})
                print("=== " + filename + " ===\n" + safe, flush=True)
        super().collect_diagnostics()

    def cleanup(self):
        try:
            super().cleanup()
        finally:
            if self.git:
                self.git.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--templates", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--artifacts", type=Path, default=ROOT / ".delivery/argocd-test")
    parser.add_argument("--kind", default="kind")
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--helm", default="helm")
    args = parser.parse_args()
    args.templates = args.templates.resolve()
    for filename in ("gitops.py", "argocd_bootstrap.py", "delivery.py"):
        if not (args.templates / "scripts" / filename).is_file():
            parser.error("--templates is missing the shared helper " + filename)

    def interrupted(signum, frame):
        raise KeyboardInterrupt("Argo acceptance interrupted; cleaning resources.")
    signal.signal(signal.SIGTERM, interrupted)
    with tempfile.TemporaryDirectory(prefix="aks-argocd-acceptance-") as tmp:
        acceptance = ArgoAcceptance(args, Path(tmp))
        try:
            acceptance.execute()
        except (OSError, ValueError, RuntimeError, AssertionError, subprocess.SubprocessError, KeyboardInterrupt) as error:
            acceptance.record.update(result="failed", failure=str(error))
            print("Argo acceptance failed: " + str(error), file=sys.stderr)
        finally:
            acceptance.cleanup()
        if acceptance.record["result"] != "passed":
            return 1
    print("Two-cluster Argo acceptance passed; Azure and production Git qualification remain separate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
