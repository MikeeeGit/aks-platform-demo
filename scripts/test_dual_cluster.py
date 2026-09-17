#!/usr/bin/env python3
"""Real, credential-free two-cluster Kubernetes acceptance using kind and a local registry.

Runs only explicitly named ephemeral test clusters. Registry mirrors are written
inside those containers, never to the host or a real AKS node.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import acceptance_gateway as gateway

ROOT = Path(__file__).resolve().parents[1]
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
MIRROR_NAME = "exampleplatformacr.azurecr.io"


def run(args, *, capture=False, cwd=None, input=None, timeout=600, env=None):
    result = subprocess.run(
        list(map(str, args)), check=True, text=True, cwd=cwd, input=input,
        stdout=subprocess.PIPE if capture else None,
        stderr=None, timeout=timeout, env=env,
    )
    return result.stdout.strip() if capture else ""


def pinned_image(value):
    if not isinstance(value, str) or "@sha256:" not in value or not DIGEST.fullmatch(value.rsplit("@", 1)[1]):
        raise ValueError("Every test infrastructure image must be pinned by sha256.")
    return value


def registry_digest(port, repository, tag):
    request = Request(
        f"http://127.0.0.1:{port}/v2/{repository}/manifests/{tag}",
        headers={"Accept": "application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json"},
    )
    with urlopen(request, timeout=15) as response:
        body = response.read()
        digest = response.headers.get("Docker-Content-Digest", "")
    if not DIGEST.fullmatch(digest) or digest != "sha256:" + hashlib.sha256(body).hexdigest():
        raise ValueError("Local registry returned a missing or mismatched content digest.")
    return digest


@contextmanager
def service_forward(kubectl, kubeconfig, context, *, service="platform-demo", remote_port=80):
    process = subprocess.Popen(
        [str(kubectl), "--kubeconfig", str(kubeconfig), "--context", context,
         "-n", "platform-demo", "port-forward", "--address", "127.0.0.1",
         "service/" + service, "0:" + str(remote_port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        deadline = time.monotonic() + 30
        port = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Service port-forward exited before becoming ready.")
            for key, _ in selector.select(timeout=0.5):
                line = key.fileobj.readline()
                found = re.search(r"Forwarding from 127\.0\.0\.1:(\d+)", line)
                if found:
                    port = int(found[1])
                    break
            if port:
                break
        selector.close()
        if not port:
            raise TimeoutError("Service port-forward did not become ready.")
        yield f"http://127.0.0.1:{port}"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()


class Acceptance:
    def __init__(self, args, work):
        self.args, self.work = args, work
        self.prefix = "aks-demo-" + uuid.uuid4().hex[:8]
        self.registry = self.prefix + "-registry"
        self.clusters = []
        self.image_tags = []
        self.kubeconfig = work / "kubeconfig"
        self.fixture = work / "source"
        self.pins = json.loads((ROOT / "ci-tools.json").read_text())
        self.artifacts = args.artifacts.resolve() / self.prefix
        self.artifacts.mkdir(parents=True)
        self.registry_created = False
        self.record = {"kind": "local-kubernetes-acceptance", "result": "running", "releases": [], "checks": [], "tool_versions": {}, "cleanup_errors": [], "infrastructure_pins": self.pins}

    def kubectl(self, cluster, *args, **kwargs):
        return run([self.args.kubectl, "--kubeconfig", self.kubeconfig,
                    "--context", "kind-" + cluster, *args], **kwargs)

    def prepare(self):
        for tool in ("docker", "git", "node", "openssl", self.args.kind, self.args.kubectl, self.args.helm):
            if not shutil.which(str(tool)):
                raise ValueError("Missing required local tool: " + str(tool))
        if run(["docker", "info", "--format", "{{.OSType}}"], capture=True) != "linux":
            raise ValueError("A Linux Docker engine is required.")
        self.record["tool_versions"] = {
            "kind": run([self.args.kind, "version"], capture=True),
            "kubectl": json.loads(run([self.args.kubectl, "version", "--client", "-o", "json"], capture=True))["clientVersion"]["gitVersion"],
            "node": run(["node", "--version"], capture=True),
            "helm": run([self.args.helm, "version", "--short"], capture=True),
            "python": sys.version.split()[0],
            "docker": run(["docker", "version", "--format", "{{.Server.Version}}"], capture=True),
        }
        for key in ("registry_image", "kind_node_image"):
            pinned_image(self.pins[key])
        # Work from an isolated clone; no app checkout files or commits are changed.
        run(["git", "clone", "--quiet", "--no-hardlinks", str(ROOT), self.fixture])
        run(["git", "-C", self.fixture, "checkout", "--quiet", "--detach",
             run(["git", "-C", ROOT, "rev-parse", "HEAD"], capture=True)])
        self.original_commit = run(["git", "-C", self.fixture, "rev-parse", "HEAD"], capture=True)
        config = json.loads((self.fixture / "delivery.apps.json").read_text())
        if config["registry"]["login_server"] != MIRROR_NAME or config["registry"]["repository"] != "aks-platform-demo":
            raise ValueError("Acceptance must use the synthetic sample registry contract.")
        self.record["public_source_commit"] = self.original_commit
        gateway.prepare_files(self, run)
        run(["git", "-C", self.fixture, "add", "deploy", "delivery.gateway.apps.json"])
        run(["git", "-C", self.fixture, "-c", "user.name=Acceptance Fixture",
             "-c", "user.email=fixture@example.test", "-c", "commit.gpgsign=false",
             "-c", "core.hooksPath=/dev/null", "commit", "-qm", "Temporary local TLS acceptance fixture"])
        self.original_commit = run(["git", "-C", self.fixture, "rev-parse", "HEAD"], capture=True)
        self.record["fixture_changes"] = ["Replace Azure CSI mount with temporary local TLS Secret",
                                         "Use local Gateway ClusterIP transport", "Temporary public CA trust"]
        run(["docker", "run", "-d", "--name", self.registry, "--publish", "127.0.0.1::5000",
             self.pins["registry_image"]])
        self.registry_created = True
        address = run(["docker", "port", self.registry, "5000/tcp"], capture=True)
        self.port = int(address.rsplit(":", 1)[1])
        for slot in ("aks01", "aks02"):
            cluster = self.prefix + "-" + slot
            # Track before create so a partially created cluster is also cleaned.
            self.clusters.append(cluster)
            run([self.args.kind, "create", "cluster", "--name", cluster,
                 "--image", self.pins["kind_node_image"], "--kubeconfig", self.kubeconfig,
                 "--wait", "180s"], timeout=600)
            nodes = run([self.args.kind, "get", "nodes", "--name", cluster], capture=True).splitlines()
            for node in nodes:
                directory = "/etc/containerd/certs.d/" + MIRROR_NAME
                run(["docker", "exec", node, "mkdir", "-p", directory])
                # Set the server itself to the local registry: no remote ACR fallback.
                hosts = (f'server = "http://{self.registry}:5000"\n'
                         f'[host."http://{self.registry}:5000"]\n'
                         '  capabilities = ["pull", "resolve"]\n')
                run(["docker", "exec", "-i", node, "cp", "/dev/stdin", directory + "/hosts.toml"], input=hosts)
            if len(self.clusters) == 1:
                run(["docker", "network", "connect", "kind", self.registry])
            self.kubectl(cluster, "apply", "-f", self.fixture / "deploy/bootstrap/namespace.yaml")
            deadline = time.monotonic() + 60
            while True:
                try:
                    self.kubectl(cluster, "-n", "platform-demo", "get", "serviceaccount/default", capture=True, timeout=10)
                    break
                except subprocess.CalledProcessError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Platform namespace default ServiceAccount was not created.")
                    time.sleep(1)
            gateway.install(self, cluster, run)

    def build(self, commit, tag):
        image = f"localhost:{self.port}/aks-platform-demo:{self.prefix}-{tag}"
        self.image_tags.append(image)
        run(["docker", "build", "--build-arg", "BUILD_REVISION=" + commit,
             "--label", "org.opencontainers.image.revision=" + commit,
             "--tag", image, self.fixture], timeout=900)
        run(["docker", "push", image], timeout=300)
        digest = registry_digest(self.port, "aks-platform-demo", self.prefix + "-" + tag)
        self.record["releases"].append({"source_commit": commit, "image_digest": digest})
        return digest

    def render(self, commit, digest, slot, name):
        output = self.work / name
        run([sys.executable, self.args.templates / "scripts/delivery.py", "render",
             "--source", self.fixture, "--config", "delivery.gateway.apps.json", "--commit", commit, "--digest", digest,
             "--environment", "pprd", "--region", "uks", "--slot", slot, "--output", output],
            env=dict(os.environ, PATH=str(Path(self.args.kubectl).resolve().parent) + os.pathsep + os.environ["PATH"])
            if Path(self.args.kubectl).is_absolute() else None)
        receipt = json.loads((output / "release.json").read_text())
        if receipt["source_commit"] != commit or receipt["image_digest"] != digest or receipt["target"]["slot"] != slot:
            raise ValueError("Shared renderer returned the wrong release target.")
        return output

    def apply_and_check(self, cluster, slot, bundle, commit, digest, label):
        manifest = bundle / "manifest.yaml"
        self.kubectl(cluster, "apply", "--dry-run=server", "--validate=strict", "-f", manifest)
        self.kubectl(cluster, "apply", "--validate=strict", "-f", manifest)
        self.check_release(cluster, slot, commit, digest, label)

    def check_release(self, cluster, slot, commit, digest, label):
        # Read-only qualification: never repair a slot before checking its state.
        self.kubectl(cluster, "-n", "platform-demo", "rollout", "status",
                     "deployment/platform-demo", "--timeout=300s")
        deployment = json.loads(self.kubectl(cluster, "-n", "platform-demo", "get",
                                             "deployment/platform-demo", "-o", "json", capture=True))
        actual = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
        if actual != MIRROR_NAME + "/aks-platform-demo@" + digest:
            raise ValueError("Cluster Deployment is not using the selected immutable image.")
        with service_forward(self.args.kubectl, self.kubeconfig, "kind-" + cluster) as url:
            for path, host in (("", "web.example.test"), ("/api", "api.example.test")):
                run(["node", ROOT / "scripts/smoke.mjs", "--url", url + path, "--host", host,
                     "--expected-slot", slot, "--expected-revision", commit], timeout=30)
        ingress = gateway.check(self, cluster, slot, commit, service_forward)
        self.record["checks"].append({"step": label, "slot": slot, "source_commit": commit,
                                     "image_digest": digest, "ingress": ingress})
        print("PASS " + label + " " + slot, flush=True)

    def execute(self):
        self.prepare()
        first = self.build(self.original_commit, "initial")
        originals = {}
        for slot, cluster in zip(("aks01", "aks02"), self.clusters):
            originals[slot] = self.render(self.original_commit, first, slot, "initial-" + slot)
            self.apply_and_check(cluster, slot, originals[slot], self.original_commit, first, "initial")
        # A second real source revision and build are isolated to the disposable clone.
        package = json.loads((self.fixture / "package.json").read_text())
        package["version"] = "0.0.0-acceptance." + self.prefix
        (self.fixture / "package.json").write_text(json.dumps(package, indent=2) + "\n")
        lock = json.loads((self.fixture / "package-lock.json").read_text())
        lock["version"] = package["version"]
        lock["packages"][""]["version"] = package["version"]
        (self.fixture / "package-lock.json").write_text(json.dumps(lock, indent=2) + "\n")
        run(["git", "-C", self.fixture, "add", "package.json", "package-lock.json"])
        run(["git", "-C", self.fixture, "-c", "user.name=Acceptance Fixture",
             "-c", "user.email=fixture@example.test", "-c", "commit.gpgsign=false",
             "-c", "core.hooksPath=/dev/null", "commit", "-qm", "Synthetic acceptance update"])
        updated = run(["git", "-C", self.fixture, "rev-parse", "HEAD"], capture=True)
        second = self.build(updated, "updated")
        if second == first:
            raise ValueError("Distinct source revisions unexpectedly produced the same image.")
        update = self.render(updated, second, "aks02", "updated-aks02")
        self.apply_and_check(self.clusters[1], "aks02", update, updated, second, "update-inactive")
        self.apply_and_check(self.clusters[1], "aks02", originals["aks02"], self.original_commit, first, "restore-approved-release")
        self.check_release(self.clusters[0], "aks01", self.original_commit, first, "active-slot-unchanged")
        self.record["result"] = "passed"

    def cleanup(self):
        if self.record["result"] == "running":
            self.record["result"] = "failed"
        if self.record["result"] != "passed":
            for cluster in self.clusters:
                try:
                    diagnostics = self.kubectl(cluster, "-n", "platform-demo", "get",
                                               "pods,deployments,services,events", "-o", "wide", capture=True, timeout=20)
                    (self.artifacts / (cluster + "-diagnostics.txt")).write_text(diagnostics)
                except (OSError, subprocess.SubprocessError):
                    pass
        cleanup_commands = [[self.args.kind, "delete", "cluster", "--name", cluster,
                             "--kubeconfig", str(self.kubeconfig)] for cluster in reversed(self.clusters)]
        if self.registry_created:
            cleanup_commands.append(["docker", "rm", "-f", self.registry])
        cleanup_commands.extend([["docker", "image", "rm", image] for image in self.image_tags])
        for command in cleanup_commands:
            try:
                subprocess.run(command, check=True, timeout=180)
            except (OSError, subprocess.SubprocessError) as error:
                self.record["cleanup_errors"].append(str(error))
                self.record["result"] = "failed"
        report = json.dumps(self.record, indent=2) + "\n"
        (self.artifacts / "result.json").write_text(report)
        if self.args.report:
            self.args.report.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.args.report.with_name(self.args.report.name + ".tmp-" + self.prefix)
            temporary.write_text(report)
            temporary.replace(self.args.report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--templates", required=True, type=Path, help="Reviewed local aks-delivery-templates checkout")
    parser.add_argument("--report", type=Path, help="Persist a JSON acceptance report, including failures and cleanup")
    parser.add_argument("--kind", default="kind")
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--helm", default="helm")
    parser.add_argument("--artifacts", type=Path, default=ROOT / ".delivery/kind-test")
    args = parser.parse_args()
    args.templates = args.templates.resolve()
    if not (args.templates / "scripts/delivery.py").is_file():
        parser.error("--templates must contain the shared application delivery helper.")
    def interrupted(signum, frame):
        raise KeyboardInterrupt("Acceptance interrupted; cleaning test resources.")
    signal.signal(signal.SIGTERM, interrupted)
    with tempfile.TemporaryDirectory(prefix="aks-demo-acceptance-") as temp:
        acceptance = Acceptance(args, Path(temp))
        try:
            acceptance.execute()
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as error:
            acceptance.record["result"] = "failed"
            acceptance.record["failure"] = str(error)
            print("Acceptance failed: " + str(error), file=sys.stderr)
            return 1
        finally:
            acceptance.cleanup()
    if acceptance.record["result"] != "passed":
        return 1
    print("Two-cluster Kubernetes acceptance passed; Azure/AKS network and identity qualification remains separate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
