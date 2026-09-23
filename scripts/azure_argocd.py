#!/usr/bin/env python3
"""Run one reviewed Argo operation on one real AKS cluster; retain private evidence."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import qualify_azure_workload as workload

SHA = re.compile(r"[0-9a-f]{40}")
ACTIONS = ("install", "bootstrap", "sync", "verify", "retire")


def need(condition, message):
    if not condition:
        raise ValueError(message)


def command(args, payload=None):
    result = subprocess.run(list(map(str, args)), input=None if payload is None else json.dumps(payload),
                            capture_output=True, text=True, timeout=120)
    # Never surface raw provider output: repository Secret data may be present.
    need(result.returncode == 0, "A required command failed; inspect access/connectivity privately")
    return result.stdout


def committed(source, revision, name):
    need(SHA.fullmatch(revision), "Use a complete reviewed commit")
    return json.loads(command(["git", "-C", source, "show", revision + ":" + name]))


def validate_source(source, revision):
    need(SHA.fullmatch(revision), "Use a complete reviewed GitOps commit")
    need(command(["git", "-C", source, "rev-parse", "HEAD"]).strip() == revision,
         "Checkout must equal the approved GitOps commit")
    need(not command(["git", "-C", source, "status", "--porcelain"]).strip(),
         "Use a clean committed consumer checkout")


def access_resources(native, target, namespace, application):
    matches = [x for x in native.get("targets", []) if all(
        x.get(k) == target[k] for k in ("environment", "region", "slot"))]
    need(len(matches) == 1 and matches[0].get("authorization_mode") == "kubernetes_rbac",
         "Provide this cluster's observed native application identity binding")
    binding = matches[0]
    ids = binding.get("deploy_principal_object_ids", [])
    names = binding.get("deploy_kubernetes_usernames", {})
    need(ids and set(ids) == set(names), "Observed usernames must match the declared application principals")
    for principal, username in names.items():
        need(re.fullmatch(r"[0-9a-fA-F-]{36}", principal) and isinstance(username, str)
             and 0 < len(username) <= 256 and not username.startswith("system:")
             and not any(c in username for c in "\r\n\t"),
             "Invalid observed application identity")
    name = application + "-sync"
    need(len(name) <= 63, "Argo sync role name is too long")
    role = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
            "metadata": {"name": name, "namespace": namespace},
            "rules": [{"apiGroups": ["argoproj.io"], "resources": ["applications"],
                       "resourceNames": [application], "verbs": ["get", "patch"]}]}
    binding = {"apiVersion": role["apiVersion"], "kind": "RoleBinding",
               "metadata": role["metadata"].copy(),
               "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name},
               "subjects": [{"kind": "User", "apiGroup": "rbac.authorization.k8s.io", "name": n}
                            for n in sorted(set(names.values()))]}
    return role, binding


def validate_application(current, wanted):
    need(not current.get("metadata", {}).get("deletionTimestamp"), "Application is being deleted")
    spec = current.get("spec", {})
    need(all(spec.get(k) == wanted["spec"][k] for k in ("source", "destination", "project"))
         and not spec.get("sources"), "Existing Application belongs to a different source or target")
    need(spec.get("syncPolicy", {}).get("automated") is None,
         "This controlled profile requires manual sync")


def retirement_options(current, wanted):
    validate_application(current, wanted)
    meta = current["metadata"]
    need(not meta.get("finalizers"), "Review cascading finalizers before retirement; none are removed automatically")
    need(not current.get("operation") and
         current.get("status", {}).get("operationState", {}).get("phase") not in ("Running", "Terminating"),
         "Wait for or terminate the active Argo operation before retirement")
    need(meta.get("uid") and meta.get("resourceVersion"), "Missing exact Application identity")
    return {"apiVersion": "v1", "kind": "DeleteOptions", "propagationPolicy": "Orphan",
            "preconditions": {"uid": meta["uid"], "resourceVersion": meta["resourceVersion"]}}


def repository_secret(base, config):
    name = "private-app-git"
    namespace = config["argo_namespace"]
    raw = command(base + ["-n", namespace, "get", "secret", name, "--ignore-not-found", "-o", "json"])
    if raw.strip():
        value = json.loads(raw)
        need(value.get("metadata", {}).get("labels", {}).get("argocd.argoproj.io/secret-type") == "repository"
             and base64.b64decode(value.get("data", {}).get("url", ""), validate=True).decode() == config["repository_url"],
             "Existing Git credential is not bound to this repository; it has not been changed")
        return
    username, token = os.environ.get("ARGO_GIT_USERNAME"), os.environ.get("ARGO_GIT_READ_TOKEN")
    need(username and token and not username.startswith("$(") and not token.startswith("$("),
         "Supply private repository read credentials through protected secret variables")
    value = {"apiVersion": "v1", "kind": "Secret",
             "metadata": {"name": name, "namespace": namespace,
                          "labels": {"argocd.argoproj.io/secret-type": "repository"}},
             "type": "Opaque", "stringData": {"type": "git", "url": config["repository_url"],
                                             "username": username, "password": token}}
    command(base + ["create", "-f", "-"], value)


def execute(args, gitops):
    source = args.source.resolve()
    validate_source(source, args.gitops_commit)
    need(re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.environment) and
         re.fullmatch(r"[a-z0-9][a-z0-9-]*", args.region), "Invalid environment/region")
    config_path = source / "gitops.config.json"
    config = gitops.load_config(config_path)
    need(config == committed(source, args.gitops_commit, "gitops.config.json"),
         "GitOps configuration differs from the approved committed version")
    need(config["revision"] == os.environ.get("DEPLOYMENT_BRANCH", "main"),
         "GitOps source must track the protected deployment branch")
    bundle = source / "gitops/releases" / args.environment / args.region / args.target_cluster
    receipt = None
    revision = args.gitops_commit
    if args.action != "install":
        need(re.fullmatch(r"[0-9a-f]{64}", args.release_sha256 or ""), "Approve the release receipt SHA256")
        receipt = gitops.verify_bundle(bundle, args.release_sha256)
        gitops.validate_proposal(bundle)
        gitops.verify_git_revision(source, args.gitops_commit, bundle, receipt["target"])
        revision = receipt["source_commit"]
    delivery = committed(source, revision, "delivery.azure-workload.apps.json")
    binding = committed(source, revision, "azure.workload.json")
    target, expected = workload.select(delivery, binding, args.environment, args.region, args.target_cluster)
    if receipt:
        need(receipt["target"] == target, "Release target differs from its source's Azure workload contract")
    account = json.loads(command(["az", "account", "show", "--subscription", target["subscription_id"], "-o", "json"]))
    need(account.get("tenantId", "").lower() == delivery["tenant_id"].lower()
         and account.get("id", "").lower() == target["subscription_id"].lower(), "Azure account/tenant mismatch")
    result = {"schema_version": 1, "action": args.action, "target_cluster": args.target_cluster,
              "gitops_commit": args.gitops_commit, "source_commit": revision, "result": "running"}
    with tempfile.TemporaryDirectory(prefix="azure-argocd-") as folder:
        private = Path(folder)
        kube = private / "kubeconfig"
        context = "reviewed-" + args.target_cluster
        command(["az", "aks", "get-credentials", "--subscription", target["subscription_id"],
                 "--resource-group", target["resource_group"], "--name", target["cluster_name"],
                 "--file", kube, "--context", context, "--format", "exec"])
        kube.chmod(0o600)
        command(["kubelogin", "convert-kubeconfig", "--login", "azurecli", "--kubeconfig", kube])
        base = ["kubectl", "--kubeconfig", str(kube), "--context", context, "--request-timeout=30s"]
        cluster = json.loads(command(["az", "aks", "show", "--subscription", target["subscription_id"],
                                     "--resource-group", target["resource_group"], "--name", target["cluster_name"], "-o", "json"]))
        kube_cluster = json.loads(command(base + ["config", "view", "--minify", "--raw", "-o",
                                                  "jsonpath={.clusters[0].cluster}"]))
        workload.validate_cluster(cluster, kube_cluster, target, expected)
        need(cluster.get("disableLocalAccounts") is True and
             cluster.get("apiServerAccessProfile", {}).get("enablePrivateCluster") is True,
             "This lab requires private AKS with local accounts disabled")
        gitops.verify_cluster_context(base, config, target)
        namespace = config["argo_namespace"]
        app = gitops.application_name(config, target)
        wanted = gitops.application(config, target)
        if args.action == "install":
            need(args.platform_bundle and args.platform_sha256, "Provide the reviewed Argo platform artifact and checksum")
            import argocd_bootstrap
            platform = argocd_bootstrap.verify(args.platform_bundle, args.platform_sha256)
            need(platform["namespace"] == namespace and platform["app_namespace"] == target["namespace"],
                 "Platform bundle namespaces differ from the selected application")
            subprocess.run([sys.executable, str(args.templates / "scripts/argocd_bootstrap.py"), "apply",
                            "--output", str(args.platform_bundle), "--receipt-sha256", args.platform_sha256,
                            "--kubeconfig", str(kube), "--context", context, "--yes"], check=True)
            repository_secret(base, config)
        elif args.action == "bootstrap":
            existing = command(base + ["-n", namespace, "get", "application", app, "--ignore-not-found", "-o", "json"])
            if existing.strip():
                validate_application(json.loads(existing), wanted)
            native = committed(source, args.gitops_commit, "bootstrap.native.apps.json")
            role, binding = access_resources(native, target, namespace, app)
            for value in (gitops.project(config, target), wanted, role, binding):
                command(base + ["apply", "-f", "-"], value)
        elif args.action in ("sync", "verify"):
            result["argo"] = gitops.verify_release(
                bundle, args.release_sha256, kube, context, namespace, app, args.gitops_commit,
                gitops_config=config_path, gitops_source=source, sync=args.action == "sync")
            delivery_path, binding_path = private / "delivery.json", private / "workload.json"
            delivery_path.write_text(json.dumps(delivery))
            binding_path.write_text(json.dumps(binding))
            result["azure_workload"] = workload.qualify(SimpleNamespace(
                config=delivery_path, workload_config=binding_path, environment=args.environment,
                region=args.region, slot=args.target_cluster, revision=revision,
                kubeconfig=kube, kubectl="kubectl"))
        else:
            raw = command(base + ["-n", namespace, "get", "application", app, "--ignore-not-found", "-o", "json"])
            if raw.strip():
                options = retirement_options(json.loads(raw), wanted)
                deletion = private / "delete-options.json"
                deletion.write_text(json.dumps(options))
                command(base + ["delete", "--raw", f"/apis/argoproj.io/v1alpha1/namespaces/{namespace}/applications/{app}",
                                "-f", deletion])
                command(base + ["-n", namespace, "wait", "--for=delete", "application/" + app, "--timeout=90s"])
            need(not command(base + ["-n", namespace, "get", "application", app, "--ignore-not-found", "-o", "name"]).strip(),
                 "Argo Application still exists; do not remove workloads")
            result["workloads_preserved"] = True
            result["next_action"] = "Run the reviewed lab service cleanup and Terraform removal procedure"
    result.update(result="passed", checked_at=datetime.now(timezone.utc).isoformat())
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--templates", required=True, type=Path)
    parser.add_argument("--gitops-commit", required=True)
    parser.add_argument("--environment", default="pprd")
    parser.add_argument("--region", default="uks")
    parser.add_argument("--target-cluster", required=True, choices=("aks01", "aks02"))
    parser.add_argument("--release-sha256")
    parser.add_argument("--platform-bundle", type=Path)
    parser.add_argument("--platform-sha256")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    need(not args.output.exists(), "Use a new private evidence filename")
    need(not args.output.resolve().is_relative_to(args.source.resolve()), "Keep runtime evidence outside the consumer checkout")
    args.templates = args.templates.resolve()
    pin = committed(args.source, args.gitops_commit, "worked-example.json")["templates_commit"]
    need(command(["git", "-C", args.templates, "rev-parse", "HEAD"]).strip() == pin and
         not command(["git", "-C", args.templates, "status", "--porcelain"]).strip(),
         "Shared checkout must match the consumer's clean immutable template pin")
    sys.path.insert(0, str(args.templates / "scripts"))
    import gitops
    result = execute(args, gitops)
    with args.output.open("x") as output:
        json.dump(result, output, indent=2)
        output.write("\n")
    print("Selected Azure Argo operation passed; private evidence written.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, subprocess.SubprocessError):
        raise SystemExit("Azure Argo operation failed. No pass report written; inspect the selected configuration, access and private connectivity.")
