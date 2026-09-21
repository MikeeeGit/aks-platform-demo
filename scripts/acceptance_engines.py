"""Use the real shared engines on explicitly owned disposable kind contexts.

These adapters substitute local cluster transport and synthetic Kubernetes users,
not Azure login, managed identity, federation, CSI or Terraform provisioning.
"""
from contextlib import contextmanager
import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlparse

import yaml


def owned_context(test, cluster, slot):
    """Read only the run-owned context; reject remote servers or external auth."""
    if (not re.fullmatch(r"aks-demo-[0-9a-f]{8}", test.prefix)
            or slot not in ("aks01", "aks02") or cluster != test.prefix + "-" + slot
            or cluster not in test.clusters):
        raise ValueError("Shared-engine acceptance requires this run's named kind cluster.")
    work, path = Path(test.work).resolve(), Path(test.kubeconfig)
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(work):
        raise ValueError("Shared-engine acceptance requires the run's isolated kubeconfig.")
    data = yaml.safe_load(path.read_text())
    name = "kind-" + cluster

    def selected(key):
        items = [item for item in data.get(key, []) if item.get("name") == name]
        if len(items) != 1:
            raise ValueError("Expected exactly one owned kind context, cluster and user.")
        return copy.deepcopy(items[0])

    context, selected_cluster, user = selected("contexts"), selected("clusters"), selected("users")
    if (context.get("context", {}).get("cluster") != name
            or context.get("context", {}).get("user") != name):
        raise ValueError("Owned context must reference only its matching kind cluster and user.")
    details = selected_cluster.get("cluster", {})
    server = urlparse(details.get("server", ""))
    try:
        loopback = ipaddress.ip_address(server.hostname or "").is_loopback
        port = server.port
    except ValueError as error:
        raise ValueError("Owned kind API must be an explicit HTTPS loopback endpoint.") from error
    if (server.scheme != "https" or not loopback or not port or server.username or server.password
            or server.path not in ("", "/") or server.query or server.fragment
            or set(details) != {"server", "certificate-authority-data"}
            or not details["certificate-authority-data"]):
        raise ValueError("Owned kind API must use loopback HTTPS and embedded CA verification.")
    credentials = user.get("user", {})
    if (set(credentials) != {"client-certificate-data", "client-key-data"}
            or not all(isinstance(value, str) and value for value in credentials.values())):
        raise ValueError("Only embedded kind client certificates without external auth are accepted.")
    return {
        "apiVersion": "v1", "kind": "Config", "current-context": name,
        "clusters": [selected_cluster], "contexts": [context], "users": [user],
    }


@contextmanager
def synthetic_context(test, cluster, slot, purpose):
    data = owned_context(test, cluster, slot)
    subjects = {
        "platform": test.prefix + "-platform-" + slot,
        "application": test.prefix + "-application",
    }
    if purpose not in subjects:
        raise ValueError("Select the synthetic platform or application user.")
    subject = subjects[purpose]
    data["users"][0]["user"]["as"] = subject
    with tempfile.TemporaryDirectory(prefix="engine-context-", dir=test.work) as temporary:
        root = Path(temporary)
        path = root / "kubeconfig"
        with open(path, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as handle:
            yaml.safe_dump(data, handle, sort_keys=False)
        yield path, data["current-context"], root, subject


def environment(test):
    directories = []
    for executable in (test.args.kubectl, test.args.helm):
        resolved = shutil.which(str(executable))
        if not resolved:
            raise ValueError("A checked Kubernetes/Helm client is required.")
        directories.append(str(Path(resolved).resolve().parent))
    return dict(os.environ, PATH=os.pathsep.join(directories + [os.environ.get("PATH", "")]))


def execute(test, script, *arguments):
    """Run the selected shared library without importing another checkout's module."""
    subprocess.run(
        [sys.executable, "-c", "import sys;sys.path.insert(0,sys.argv[1]);" + script,
         str(Path(test.args.templates).resolve() / "scripts"), *map(str, arguments)],
        check=True, env=environment(test), timeout=1800,
    )


def template_evidence(test, engine):
    templates = Path(test.args.templates).resolve()
    commit = subprocess.check_output(
        ["git", "-C", str(templates), "rev-parse", "HEAD"], text=True,
    ).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Shared template revision must be a full Git commit.")
    dirty = bool(subprocess.check_output(
        ["git", "-C", str(templates), "status", "--porcelain"], text=True,
    ).strip())
    files = [engine, "native_authorization.py", "verify_service.py"]
    return {
        "repository_commit": commit, "working_tree_modified": dirty,
        "script_sha256": {name: hashlib.sha256((templates / "scripts" / name).read_bytes()).hexdigest()
                          for name in files},
    }


def prepare_platform_fixture(test):
    """Commit a kind-only copy of the maintained Azure platform profile."""
    source = test.gateway_dir / "platform-source"
    shutil.copytree(Path(test.args.templates) / "examples/platform-envoy", source)
    config = json.loads((source / "platform.services.json").read_text())
    pins = test.pins["envoy_gateway"]
    controller_file = source / "values/controller.yaml"
    controller = yaml.safe_load(controller_file.read_text())
    for name, image in pins["images"].items():
        if controller["global"]["images"][name]["image"] != image:
            raise ValueError("Platform controller images must match the acceptance pins.")
    controller["deployment"]["replicas"] = 1
    controller_file.write_text(yaml.safe_dump(controller, sort_keys=False))
    for target in config["targets"]:
        if target["crd_bundles"] != pins["crds"] or target.get("manage_crds") is not True:
            raise ValueError("Platform CRD lifecycle must match the maintained pinned contract.")
        if len(target["releases"]) != 1:
            raise ValueError("The kind profile expects one reviewed Envoy release.")
        chart = target["releases"][0]["chart"]
        if chart["url"] != pins["chart"] or chart["sha256"] != pins["chart_sha256"]:
            raise ValueError("Platform chart must match the acceptance pin.")
        target["workload_service_accounts"] = []
        slot = target["slot"]
        target["cluster_name"] = test.prefix + "-" + slot
        path = source / ("manifests/proxy-" + slot + ".yaml")
        proxy = yaml.safe_load(path.read_text())
        kubernetes = proxy["spec"]["provider"]["kubernetes"]
        if kubernetes["envoyDeployment"]["container"]["image"] != pins["images"]["envoyProxy"]:
            raise ValueError("Platform proxy image must match the acceptance pin.")
        shutdown = kubernetes["envoyDeployment"]["patch"]["value"]["spec"]["template"]["spec"]["containers"]
        if not any(item.get("name") == "shutdown-manager"
                   and item.get("image") == pins["images"]["envoyGateway"] for item in shutdown):
            raise ValueError("Platform shutdown-manager image must match the acceptance pin.")
        kubernetes["envoyService"] = {"type": "ClusterIP"}
        kubernetes.pop("envoyHpa", None)
        kubernetes["envoyDeployment"]["replicas"] = 1
        kubernetes["envoyDeployment"]["container"]["resources"] = {
            "requests": {"cpu": "100m", "memory": "128Mi"},
            "limits": {"cpu": "500m", "memory": "512Mi"},
        }
        path.write_text(yaml.safe_dump(proxy, sort_keys=False))
    (source / "platform.services.json").write_text(json.dumps(config, indent=2) + "\n")
    subprocess.run(["git", "init", "--quiet", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run([
        "git", "-C", str(source), "-c", "user.name=Acceptance Fixture",
        "-c", "user.email=fixture@example.test", "-c", "commit.gpgsign=false",
        "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m",
        "Disposable kind platform transport and capacity",
    ], check=True)
    test.platform_fixture = source
    test.platform_fixture_commit = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True,
    ).strip()


def install_platform(test, cluster, slot):
    """Bootstrap synthetic authority, then execute the actual platform engine."""
    # Validate before even creating a synthetic cluster-admin binding.
    owned_context(test, cluster, slot)
    import acceptance_native_rbac as native
    bundle = test.work / ("platform-bundle-" + slot)
    script = (
        "from pathlib import Path;import platform_services as p;"
        "p.prepare(Path(sys.argv[2]),'platform.services.json',sys.argv[3],"
        "'pprd','uks',sys.argv[4],Path(sys.argv[5]))"
    )
    execute(test, script, test.platform_fixture, test.platform_fixture_commit, slot, bundle)
    expected = hashlib.sha256((bundle / "platform.json").read_bytes()).hexdigest()
    subject = test.prefix + "-platform-" + slot
    declaration = native.platform_manifests(test.args.templates, subject, slot)
    test.kubectl(cluster, "apply", "--validate=strict", "-f", "-", input=declaration)
    with synthetic_context(test, cluster, slot, "platform") as (kubeconfig, context, runtime, subject):
        execute(test,
                "from pathlib import Path;import platform_services as p;"
                "p._apply_to_context(Path(sys.argv[2]),sys.argv[3],kubeconfig=Path(sys.argv[4]),"
                "context=sys.argv[5],run_directory=Path(sys.argv[6]))",
                bundle, expected, kubeconfig, context, runtime)
    receipt = json.loads((bundle / "platform.json").read_text())
    test.record.setdefault("tier_execution", []).append({
        "tier": "platform", "slot": slot, "context": "kind-" + cluster,
        "subject": subject, "identity_source": "synthetic-kubernetes-impersonation",
        "engine": "platform_services.prepare + platform_services._apply_to_context",
        "receipt_sha256": expected, "fixture_source_commit": receipt["source_commit"],
        "template": template_evidence(test, "platform_services.py"),
        "crd_bundle_count": len(receipt["crds"]),
        "crd_safe_upgrade_policy_bundle_count": len(receipt["crd_policies"]),
        "fixture_changes": ["ClusterIP transport", "single controller/proxy replicas",
                            "reduced proxy resources", "no HPA in kind",
                            "no Azure ServiceAccount annotations"],
        "result": "passed",
    })
    return receipt


def deploy(test, cluster, slot, bundle):
    """Use the actual deploy core as the namespace-scoped synthetic app user."""
    owned_context(test, cluster, slot)
    bundle = Path(bundle)
    if bundle.is_symlink() or not bundle.resolve().is_relative_to(Path(test.work).resolve()):
        raise ValueError("Application bundle must belong to this run's private workspace.")
    receipt = json.loads((bundle / "release.json").read_text())
    target = receipt.get("target", {})
    if (target.get("slot") != slot or target.get("environment") != "pprd"
            or target.get("region") != "uks" or target.get("namespace") != "platform-demo"):
        raise ValueError("Application receipt does not select this acceptance target.")
    expected = hashlib.sha256((bundle / "release.json").read_bytes()).hexdigest()
    with synthetic_context(test, cluster, slot, "application") as (kubeconfig, context, _, subject):
        execute(test,
                "from pathlib import Path;import delivery;"
                "delivery._deploy_to_context(Path(sys.argv[2]),sys.argv[3],"
                "kubeconfig=Path(sys.argv[4]),context=sys.argv[5])",
                bundle, expected, kubeconfig, context)
    test.record.setdefault("tier_execution", []).append({
        "tier": "application", "slot": slot, "context": "kind-" + cluster,
        "subject": subject, "identity_source": "synthetic-kubernetes-impersonation",
        "engine": "delivery._deploy_to_context", "receipt_sha256": expected,
        "source_commit": receipt["source_commit"], "image_digest": receipt["image_digest"],
        "template": template_evidence(test, "delivery.py"), "result": "passed",
    })
    return receipt
