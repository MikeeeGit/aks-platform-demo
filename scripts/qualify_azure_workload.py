#!/usr/bin/env python3
"""Read-only real-AKS CSI qualification; never retrieves or emits secret contents."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
from urllib.parse import urlparse
from urllib.request import ProxyHandler, build_opener
import uuid

import yaml

REVISION = re.compile(r"^[a-f0-9]{40}$")
NAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
FILE = "/mnt/app-secrets/qualification"
MOUNT = "/mnt/app-secrets"


class QualificationError(ValueError):
    """A safe-to-display qualification failure without raw provider output."""


def need(condition, message):
    if not condition:
        raise QualificationError(message)


def read_json(path):
    need(Path(path).stat().st_size <= 1024 * 1024, "Configuration exceeds the size limit")
    return json.loads(Path(path).read_text())


def select(delivery, workload, environment, region, slot):
    need(delivery.get("schema_version") == workload.get("schema_version") == 1,
         "Expected version 1 delivery and workload contracts")
    key = (environment, region, slot)
    pick = lambda entries: [entry for entry in entries if
                            tuple(entry.get(k) for k in ("environment", "region", "slot")) == key]
    targets, bindings = pick(delivery.get("targets", [])), pick(workload.get("targets", []))
    need(len(targets) == len(bindings) == 1, "Expected one exact delivery target and workload binding")
    target, expected = targets[0], bindings[0]
    for field in ("namespace", "deployment"):
        need(NAME.fullmatch(target.get(field, "")), "Invalid delivery target name")
    for field in ("service_account", "secret_provider_class"):
        need(NAME.fullmatch(expected.get(field, "")), "Invalid workload binding name")
    need(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.()-]{0,89}", target.get("resource_group", "")),
         "Invalid Azure resource group name")
    need(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", target.get("cluster_name", "")),
         "Invalid AKS cluster name")
    need(re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{1,22}[A-Za-z0-9]", expected.get("vault_name", "")),
         "Invalid Key Vault name")
    need(re.fullmatch(r"[A-Za-z0-9-]{1,127}", expected.get("secret_name", "")),
         "Invalid Key Vault secret name")
    for value in (target.get("subscription_id"), delivery.get("tenant_id"),
                  expected.get("tenant_id"), expected.get("client_id")):
        need(isinstance(value, str), "Expected identity UUIDs")
        need(str(uuid.UUID(value)) == value.lower() and uuid.UUID(value).int != 0,
             "Expected nonzero identity UUIDs")
    need(expected["tenant_id"].lower() == delivery["tenant_id"].lower(), "Workload tenant mismatch")
    need(slot in ("aks01", "aks02"), "Expected an explicit supported AKS slot")
    return target, expected


def run_read(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=45)
    need(result.returncode == 0, "A required Azure/Kubernetes read failed; inspect access and connectivity privately")
    need(len(result.stdout) <= 16 * 1024 * 1024, "Read response exceeds the size limit")
    return result.stdout


def command(args):
    return json.loads(run_read(args))


def validate_cluster(cluster, kube_cluster, target, expected):
    cluster_id = ("/subscriptions/" + target["subscription_id"] + "/resourceGroups/" +
                  target["resource_group"] + "/providers/Microsoft.ContainerService/managedClusters/" +
                  target["cluster_name"])
    need(cluster.get("id", "").lower() == cluster_id.lower(), "Azure cluster identity mismatch")
    endpoint = urlparse(kube_cluster.get("server", ""))
    need(endpoint.scheme == "https" and endpoint.hostname and endpoint.port in (None, 443)
         and not endpoint.username and not endpoint.password and not endpoint.query
         and not endpoint.fragment and endpoint.path in ("", "/"), "Expected a direct HTTPS AKS endpoint")
    names = [cluster.get("privateFqdn"), cluster.get("fqdn")]
    need(endpoint.hostname.lower() in [name.lower() for name in names if isinstance(name, str)],
         "Kubeconfig endpoint does not match the selected Azure cluster")
    need(kube_cluster.get("certificate-authority-data") and
         not kube_cluster.get("insecure-skip-tls-verify") and not kube_cluster.get("proxy-url"),
         "Expected a CA-verified direct kubeconfig")
    need(cluster.get("aadProfile", {}).get("tenantId", "").lower() == expected["tenant_id"].lower(),
         "AKS Entra tenant mismatch")
    need(cluster.get("oidcIssuerProfile", {}).get("enabled") is True and
         cluster.get("securityProfile", {}).get("workloadIdentity", {}).get("enabled") is True and
         cluster.get("addonProfiles", {}).get("azureKeyvaultSecretsProvider", {}).get("enabled") is True,
         "AKS OIDC, workload identity and Azure CSI must all be enabled")
    return cluster_id


def validate_binding(account, provider, expected, namespace):
    need(account.get("metadata", {}).get("name") == expected["service_account"] and
         account["metadata"].get("namespace") == namespace, "ServiceAccount target mismatch")
    need(account["metadata"].get("annotations", {}).get("azure.workload.identity/client-id", "").lower()
         == expected["client_id"].lower(), "ServiceAccount workload client ID mismatch")
    need(provider.get("metadata", {}).get("name") == expected["secret_provider_class"] and
         provider["metadata"].get("namespace") == namespace, "SecretProviderClass target mismatch")
    spec = provider.get("spec", {})
    p = spec.get("parameters", {})
    need(spec.get("provider") == "azure" and p.get("usePodIdentity") == "false"
         and p.get("useVMManagedIdentity", "false") == "false"
         and not p.get("userAssignedIdentityID"), "Expected Azure workload identity CSI authentication")
    need(p.get("clientID", "").lower() == expected["client_id"].lower()
         and p.get("tenantId", "").lower() == expected["tenant_id"].lower()
         and p.get("keyvaultName") == expected["vault_name"], "SecretProviderClass identity or vault mismatch")
    need(not spec.get("secretObjects"), "Qualification secret must remain a file-only CSI mount")
    objects = yaml.safe_load(p.get("objects", ""))
    need(isinstance(objects, dict) and isinstance(objects.get("array"), list), "Invalid CSI object list")
    entries = [yaml.safe_load(item) for item in objects["array"]]
    need(len(entries) == 1 and isinstance(entries[0], dict), "Expected only the qualification object")
    item = entries[0]
    need(item.get("objectName") == expected["secret_name"] and item.get("objectAlias") == "qualification"
         and item.get("objectType") == "secret" and not item.get("objectVersion"),
         "Qualification object name, alias, type or rotation binding mismatch")


def container(pod_spec):
    apps = [item for item in pod_spec.get("containers", []) if item.get("name") == "app"]
    need(len(apps) == 1, "Expected exactly one named application container")
    return apps[0]


def validate_mount(pod_spec, expected):
    need(pod_spec.get("serviceAccountName") == expected["service_account"], "Pod ServiceAccount mismatch")
    app = container(pod_spec)
    env = {item["name"]: item.get("value") for item in app.get("env", [])}
    need(env.get("APP_REQUIRED_SECRET_FILE") == FILE, "Required app-secret readiness guard is missing")
    mounts = [m for m in app.get("volumeMounts", []) if m.get("mountPath") == MOUNT]
    need(len(mounts) == 1 and mounts[0].get("readOnly") is True
         and not mounts[0].get("subPath") and not mounts[0].get("subPathExpr"),
         "Expected one read-only rotating application-secret mount")
    volumes = [v for v in pod_spec.get("volumes", []) if v.get("name") == mounts[0]["name"]]
    need(len(volumes) == 1, "Application secret volume is missing")
    csi = volumes[0].get("csi") or {}
    need(csi.get("driver") == "secrets-store.csi.k8s.io" and csi.get("readOnly") is True and
         csi.get("volumeAttributes", {}).get("secretProviderClass") == expected["secret_provider_class"]
         and not csi.get("nodePublishSecretRef"), "Expected the declared workload-identity CSI volume")


def controlled_by(item, kind, uid):
    return any(ref.get("kind") == kind and ref.get("uid") == uid and ref.get("controller") is True
               for ref in item.get("metadata", {}).get("ownerReferences", []))


def validate_resources(deployment, replica_sets, pods, statuses, expected, namespace):
    uid = deployment.get("metadata", {}).get("uid")
    status = deployment.get("status", {})
    desired = deployment.get("spec", {}).get("replicas", 1)
    need(uid and isinstance(desired, int) and desired > 0, "Deployment has no desired running replicas")
    need(status.get("observedGeneration", 0) >= deployment["metadata"].get("generation", 1) and
         status.get("updatedReplicas", 0) == desired and status.get("replicas", 0) == desired and
         status.get("availableReplicas", 0) == desired and status.get("readyReplicas", 0) == desired,
         "Deployment is not fully observed, updated, available and ready")
    validate_mount(deployment["spec"]["template"]["spec"], expected)
    selected_image = container(deployment["spec"]["template"]["spec"]).get("image", "")
    need(re.fullmatch(r"[a-z0-9][a-z0-9./_:-]*@sha256:[a-f0-9]{64}", selected_image),
         "Deployed application must use an immutable image digest")
    owned_sets = {item["metadata"]["uid"] for item in replica_sets if controlled_by(item, "Deployment", uid)}
    selected = [pod for pod in pods if any(controlled_by(pod, "ReplicaSet", rs) for rs in owned_sets)
                and not pod.get("metadata", {}).get("deletionTimestamp")]
    need(len(selected) == desired, "Pod inventory does not match the fully rolled-out Deployment")
    result = []
    for pod in selected:
        meta, spec = pod.get("metadata", {}), pod.get("spec", {})
        need(meta.get("namespace") == namespace and meta.get("uid"), "Pod namespace/identity mismatch")
        need(meta.get("labels", {}).get("azure.workload.identity/use") == "true", "Workload identity pod label missing")
        validate_mount(spec, expected)
        app = container(spec)
        need(app.get("image") == selected_image, "Pod image differs from the selected Deployment")
        env = {item["name"]: item.get("value") for item in app.get("env", [])}
        need(env.get("AZURE_CLIENT_ID", "").lower() == expected["client_id"].lower()
             and env.get("AZURE_TENANT_ID", "").lower() == expected["tenant_id"].lower()
             and env.get("AZURE_FEDERATED_TOKEN_FILE"), "Projected workload identity configuration is missing or mismatched")
        need(any(c.get("type") == "Ready" and c.get("status") == "True"
                 for c in pod.get("status", {}).get("conditions", [])), "Selected application Pod is not Ready")
        matches = [item for item in statuses if
                   item.get("status", {}).get("podName") == meta["name"] and
                   item.get("status", {}).get("secretProviderClassName") == expected["secret_provider_class"]]
        need(len(matches) == 1, "Expected one application CSI mount status per Pod")
        mount_status, mount_meta = matches[0].get("status", {}), matches[0].get("metadata", {})
        need(mount_meta.get("namespace") == namespace and
             any(ref.get("kind") == "Pod" and ref.get("uid") == meta["uid"]
                 for ref in mount_meta.get("ownerReferences", [])),
             "CSI status belongs to another Pod or namespace")
        need(mount_status.get("mounted") is True and
             ("/pods/" + meta["uid"] + "/") in mount_status.get("targetPath", ""),
             "CSI status does not establish a mounted volume on this exact Pod")
        objects = mount_status.get("objects", [])
        need(len(objects) == 1 and objects[0].get("id") == "secret/" + expected["secret_name"]
             and re.fullmatch(r"[a-fA-F0-9]{32}", objects[0].get("version", "")),
             "CSI status does not contain the expected versioned Key Vault secret")
        result.append({"pod": meta["name"], "pod_uid": meta["uid"],
                       "secret_version": objects[0]["version"]})
    return selected_image, result


@contextmanager
def tunnel(base, namespace, pod):
    # Use kubectl's atomic ephemeral-port allocation instead of reserving a socket
    # and releasing it before kubectl can bind.
    process = subprocess.Popen(base + ["--namespace", namespace, "port-forward",
                               "pod/" + pod, ":8080", "--address", "127.0.0.1"],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    import queue
    import threading
    lines = queue.Queue()
    def consume():
        for line in process.stdout:
            lines.put(line)
    reader = threading.Thread(target=consume, daemon=True)
    reader.start()
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            need(process.poll() is None, "Pod port-forward failed")
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                continue
            match = re.search(r"Forwarding from 127\.0\.0\.1:(\d+) -> 8080", line)
            if match:
                yield int(match.group(1))
                return
        raise ValueError("Timed out opening the selected Pod tunnel")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        reader.join(timeout=1)


def request(port, path):
    opener = build_opener(ProxyHandler({}))
    with opener.open("http://127.0.0.1:" + str(port) + path, timeout=5) as response:
        need(response.status == 200, "Application readiness or version check failed")
        content = response.read(16385)
        need(len(content) <= 16384, "Application response exceeds the size limit")
        return json.loads(content)


def verify_response(readiness, version, revision, slot):
    need(readiness == {"status": "ready"}, "Application did not confirm its secret-dependent readiness")
    need(version.get("application") == "aks-platform-demo" and version.get("revision") == revision
         and version.get("slot") == slot, "Application revision or selected slot mismatch")


def qualify(args):
    need(REVISION.fullmatch(args.revision), "Provide the full deployed source revision")
    target, expected = select(read_json(args.config), read_json(args.workload_config),
                              args.environment, args.region, args.slot)
    need(Path(args.kubeconfig).is_file(), "Provide the selected isolated Entra kubeconfig")
    base = [args.kubectl, "--kubeconfig", str(Path(args.kubeconfig).resolve())]
    cluster = command(["az", "aks", "show", "--subscription", target["subscription_id"],
                       "--resource-group", target["resource_group"], "--name", target["cluster_name"],
                       "--output", "json"])
    kube_cluster = command(base + ["config", "view", "--minify", "--raw", "--output",
                                  "jsonpath={.clusters[0].cluster}"])
    cluster_id = validate_cluster(cluster, kube_cluster, target, expected)
    def get(resource):
        return command(base + ["--namespace", target["namespace"], "get", resource, "--output", "json"])
    account = get("serviceaccount/" + expected["service_account"])
    provider = get("secretproviderclass/" + expected["secret_provider_class"])
    validate_binding(account, provider, expected, target["namespace"])
    deployment = get("deployment/" + target["deployment"])
    image, pods = validate_resources(deployment, get("replicasets")["items"], get("pods")["items"],
                                    get("secretproviderclasspodstatuses")["items"], expected, target["namespace"])
    for pod in pods:
        with tunnel(base, target["namespace"], pod["pod"]) as port:
            verify_response(request(port, "/readyz"), request(port, "/version"), args.revision, args.slot)
    # Never claim a pass across a concurrent rollout or replacement.
    latest = get("deployment/" + target["deployment"])
    need(latest["metadata"].get("uid") == deployment["metadata"]["uid"] and
         latest["metadata"].get("generation") == deployment["metadata"].get("generation"),
         "Deployment changed during qualification; rerun against the final rollout")
    return {"schema_version": 1, "result": "passed",
            "qualification": "live-aks-workload-identity-key-vault-csi",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "cluster_id": cluster_id, "environment": args.environment, "region": args.region,
            "slot": args.slot, "namespace": target["namespace"], "source_revision": args.revision,
            "image": image, "workload_client_id": expected["client_id"],
            "vault_name": expected["vault_name"], "secret_name": expected["secret_name"], "pods": pods,
            "limits": ["No secret value was retrieved by this verifier.",
                       "A mounted current version does not prove future token renewal or secret rotation.",
                       "Other applications, Key Vault objects and Azure resource permissions are not qualified."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="delivery.azure-workload.apps.json")
    parser.add_argument("--workload-config", default="azure.workload.json")
    parser.add_argument("--environment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--slot", required=True, choices=["aks01", "aks02"])
    parser.add_argument("--revision", required=True)
    kube = parser.add_mutually_exclusive_group(required=True)
    kube.add_argument("--kubeconfig")
    kube.add_argument("--acquire-kubeconfig", action="store_true",
                      help="Obtain isolated user credentials using the current Azure CLI identity")
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    need(not Path(args.output).exists(), "Output already exists; retain prior evidence and choose a new file")
    if args.acquire_kubeconfig:
        target, _ = select(read_json(args.config), read_json(args.workload_config),
                           args.environment, args.region, args.slot)
        with tempfile.TemporaryDirectory(prefix="aks-workload-qualification-") as temporary:
            args.kubeconfig = str(Path(temporary) / "kubeconfig")
            run_read(["az", "aks", "get-credentials", "--subscription", target["subscription_id"],
                      "--resource-group", target["resource_group"], "--name", target["cluster_name"],
                      "--file", args.kubeconfig, "--format", "exec", "--overwrite-existing"])
            Path(args.kubeconfig).chmod(0o600)
            run_read(["kubelogin", "convert-kubeconfig", "--login", "azurecli",
                      "--kubeconfig", args.kubeconfig])
            report = qualify(args)
    else:
        report = qualify(args)
    # Exclusive creation keeps an older report intact; private directory is caller-owned.
    with open(args.output, "x", encoding="utf8") as output:
        json.dump(report, output, indent=2)
        output.write("\n")
    print("Selected AKS workload identity and Key Vault CSI qualification passed; private report written.")


if __name__ == "__main__":
    try:
        main()
    except QualificationError as error:
        raise SystemExit("Azure workload qualification failed: " + str(error) + ". No pass report was written.")
    except (ValueError, OSError, subprocess.SubprocessError, yaml.YAMLError, KeyError, TypeError, AttributeError):
        raise SystemExit("Azure workload qualification failed. Check the selected target, deployment, identity, CSI status and private worker access; no pass report was written.")
