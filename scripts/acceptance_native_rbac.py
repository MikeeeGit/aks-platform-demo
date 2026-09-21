"""Real native application and platform RBAC confined to owned disposable kind clusters."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys

import yaml

from acceptance_argocd import authorization_answer, authorization_review, authorization_review_answer

NAMESPACE = "platform-demo"
PRINCIPAL = "00000000-0000-0000-0000-000000000501"


def manifests(templates, subject):
    """Evaluate the actual shared authorization generator, without Azure login."""
    settings = {
        "deploy_principal_object_ids": [PRINCIPAL] if subject else [],
        "deploy_kubernetes_usernames": {PRINCIPAL: subject} if subject else {},
    }
    script = (
        "import json,sys;sys.path.insert(0,sys.argv[1]);"
        "from native_authorization import application_resources;"
        "json.dump({'apiVersion':'v1','kind':'List','items':"
        "application_resources(json.load(sys.stdin),'platform-demo')},sys.stdout)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(Path(templates).resolve() / "scripts")],
        input=json.dumps(settings), text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=30, check=True,
    )
    objects = json.loads(result.stdout)
    if ([item.get("kind") for item in objects.get("items", [])] != ["Role", "RoleBinding"]
            or any(item.get("metadata") != {"name": "aks-delivery-application", "namespace": NAMESPACE}
                   for item in objects["items"])):
        raise ValueError("Native acceptance must apply only the shared application Role and RoleBinding.")
    return json.dumps(objects)


def as_user(test, cluster, subject, arguments, *, input=None):
    return subprocess.run(
        [str(test.args.kubectl), "--kubeconfig", str(test.kubeconfig),
         "--context", "kind-" + cluster, "--as", subject, *arguments],
        input=input, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=30, check=False,
    )


def forbidden(result):
    if result.returncode == 0 or "Error from server (Forbidden)" not in result.stderr:
        raise AssertionError("Expected an explicit Kubernetes Forbidden response, not transport/discovery failure.")


def check(test, cluster, slot, bundle):
    if (not re.fullmatch(r"aks-demo-[0-9a-f]{8}", test.prefix)
            or slot not in ("aks01", "aks02") or cluster != test.prefix + "-" + slot
            or cluster not in test.clusters):
        raise ValueError("Native RBAC acceptance is restricted to this run's named kind clusters.")
    manifest = (Path(bundle) / "manifest.yaml").resolve()
    if not manifest.is_relative_to(test.work.resolve()):
        raise ValueError("Native RBAC acceptance requires this run's rendered bundle.")
    objects = [item for item in yaml.safe_load_all(manifest.read_text()) if item]
    if (not objects or any(item.get("kind") == "Secret" or
                           item.get("metadata", {}).get("namespace") != NAMESPACE for item in objects)):
        raise ValueError("Expected a namespaced application fixture with no Secret material.")
    subject = test.prefix + "-application"
    declaration = manifests(test.args.templates, subject)
    empty = manifests(test.args.templates, None)
    test.kubectl(cluster, "apply", "--validate=strict", "-f", "-", input=declaration)
    checks = test.record.setdefault("native_rbac_checks", [])

    def record(step, method, **details):
        checks.append({"step": step, "cluster": cluster, "context": "kind-" + cluster,
                       "slot": slot, "subject": subject, "method": method, **details})

    def probe(verb, resource, namespace, expected, step="authorization"):
        explicit = resource == "secretproviderclasses.secrets-store.csi.x-k8s.io"
        if explicit:
            command = ["create", "--raw", "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews", "-f", "-"]
            body = json.dumps(authorization_review(resource, namespace, verb))
        else:
            name, _, subresource = resource.partition("/")
            command = ["auth", "can-i", verb, name]
            if subresource:
                command += ["--subresource", subresource]
            if namespace:
                command += ["--namespace", namespace]
            body = None
        result = as_user(test, cluster, subject, command, input=body)
        allowed = (authorization_review_answer(result.returncode, result.stdout) if explicit else
                   authorization_answer(result.returncode, result.stdout, result.stderr))
        record(step, "explicit-self-subject-access-review" if explicit else "kubectl-auth-can-i",
               verb=verb, resource=resource, namespace=namespace,
               expected_allowed=expected, allowed=allowed,
               resource_discovery_used=not explicit)
        if allowed != expected:
            raise AssertionError(f"Native application RBAC mismatch: {verb} {resource} in {namespace}.")

    allowed = [
        ("create", "deployments.apps"), ("patch", "deployments.apps"),
        ("create", "httproutes.gateway.networking.k8s.io"),
        ("patch", "httproutes.gateway.networking.k8s.io"),
        ("get", "gateways.gateway.networking.k8s.io"),
        ("create", "pods/portforward"),
        ("create", "secretproviderclasses.secrets-store.csi.x-k8s.io"),
        ("patch", "secretproviderclasses.secrets-store.csi.x-k8s.io"),
    ]
    denied = [
        ("patch", "gateways.gateway.networking.k8s.io", NAMESPACE),
        ("patch", "envoyproxies.gateway.envoyproxy.io", NAMESPACE),
        ("create", "roles.rbac.authorization.k8s.io", NAMESPACE),
        ("create", "rolebindings.rbac.authorization.k8s.io", NAMESPACE),
        ("get", "secrets", NAMESPACE), ("create", "pods/exec", NAMESPACE),
        ("create", "deployments.apps", "default"), ("delete", "deployments.apps", NAMESPACE),
    ]
    for verb, resource in allowed:
        probe(verb, resource, NAMESPACE, True)
    for verb, resource, namespace in denied:
        probe(verb, resource, namespace, False)
    dry_run = ["apply", "--dry-run=server", "--validate=strict", "-f", str(manifest)]
    result = as_user(test, cluster, subject, dry_run)
    if result.returncode:
        raise AssertionError("Native application identity could not server-dry-run the actual rendered fixture.")
    record("bundle-admitted", "kubectl-server-dry-run", expected_allowed=True, allowed=True)
    result = as_user(test, cluster, subject, [
        "patch", "gateway", "platform-demo-private", "--namespace", NAMESPACE,
        "--type=merge", "--patch", '{"metadata":{"annotations":{"native-rbac-check":"must-be-denied"}}}',
        "--dry-run=server",
    ])
    forbidden(result)
    record("gateway-write-rejected", "kubectl-server-dry-run", expected_allowed=False, allowed=False)
    test.kubectl(cluster, "apply", "--validate=strict", "-f", "-", input=empty)
    probe("patch", "deployments.apps", NAMESPACE, False, step="removed-principal-revoked")
    result = as_user(test, cluster, subject, dry_run)
    forbidden(result)
    record("revoked-bundle-rejected", "kubectl-server-dry-run", expected_allowed=False, allowed=False)
    test.kubectl(cluster, "apply", "--validate=strict", "-f", "-", input=declaration)
    result = as_user(test, cluster, subject, dry_run)
    if result.returncode:
        raise AssertionError("Restored native application binding did not restore rendered-fixture admission.")
    record("restored-bundle-admitted", "kubectl-server-dry-run", expected_allowed=True, allowed=True)
    print("PASS native application RBAC and binding revocation " + cluster, flush=True)
    platform_check(test, cluster, slot)

def owned_clusters(test, cluster, slot):
    """Restrict privileged fixtures to both clusters created by this harness."""
    expected = {test.prefix + "-aks01", test.prefix + "-aks02"}
    if (not re.fullmatch(r"aks-demo-[0-9a-f]{8}", test.prefix)
            or slot not in ("aks01", "aks02") or cluster != test.prefix + "-" + slot
            or len(test.clusters) != 2 or set(test.clusters) != expected):
        raise ValueError("Platform RBAC acceptance requires this run's two named kind clusters.")
    work, kubeconfig = test.work.resolve(), Path(test.kubeconfig)
    if kubeconfig.is_symlink() or not kubeconfig.resolve().is_relative_to(work):
        raise ValueError("Platform RBAC acceptance requires this run's isolated kubeconfig.")
    return next(item for item in test.clusters if item != cluster)


def platform_manifests(templates, subject, slot):
    """Render the real opt-in generator using clearly synthetic applied outputs."""
    if slot not in ("aks01", "aks02") or (subject is not None and
            not re.fullmatch(r"aks-demo-[0-9a-f]{8}-platform-" + slot, subject)):
        raise ValueError("Only a disposable synthetic platform user and slot are accepted.")
    tenant = "00000000-0000-0000-0000-000000000001"
    subscription = "00000000-0000-0000-0000-000000000002"
    principal = "00000000-0000-0000-0000-000000000601"
    client = "00000000-0000-0000-0000-000000000602"
    resource_group, cluster_name = "kind-fixture-rg", "kind-fixture-" + slot
    cluster_id = (f"/subscriptions/{subscription}/resourceGroups/{resource_group}"
                  f"/providers/Microsoft.ContainerService/managedClusters/{cluster_name}")
    target = {
        "environment": "pprd", "region": "uks", "slot": slot,
        "subscription_id": subscription, "resource_group": resource_group,
        "cluster_name": cluster_name,
    }
    values = {
        "deployment_context": {
            "tenant_id": tenant, "subscription_id": subscription,
            "environment": "pprd", "region": "uks",
        },
        "delivery_authorization": {
            "mode": "kubernetes_rbac",
            "admin_group_object_ids": ["00000000-0000-0000-0000-000000000603"],
            "principals": {
                "platform": {
                    "client_id": client, "principal_id": principal, "purpose": "platform",
                    "namespaces": [], "clusters": [slot],
                },
            },
            "targets": {slot: {
                "cluster_id": cluster_id, "cluster_name": cluster_name,
                "resource_group_name": resource_group,
            }},
            "cluster_user_assignments": {"platform/" + slot: {
                "id": cluster_id + "/providers/Microsoft.Authorization/roleAssignments/"
                      "00000000-0000-0000-0000-000000000604",
                "principal_id": principal, "cluster_id": cluster_id,
            }},
        },
    }
    settings = {
        "outputs": {name: {"sensitive": False, "value": value} for name, value in values.items()},
        "records": [{
            "schema_version": 1, "kind": "aks-kubernetes-identity",
            "tenant_id": tenant, "subscription_id": subscription, "cluster_id": cluster_id,
            "client_id": client, "username": subject,
        }] if subject else [],
        "target": target, "tenant": tenant, "keys": ["platform"] if subject else [],
    }
    script = (
        "import json,sys;sys.path.insert(0,sys.argv[1]);"
        "from native_authorization import platform_resources;"
        "s=json.load(sys.stdin);"
        "json.dump({'apiVersion':'v1','kind':'List','items':platform_resources("
        "s['outputs'],s['records'],s['target'],s['tenant'],"
        "platform_principal_keys=s['keys'],allow_platform_admin=True)},sys.stdout)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(Path(templates).resolve() / "scripts")],
        input=json.dumps(settings), text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=30, check=True,
    )
    objects = json.loads(result.stdout)
    wanted = {
        "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
        "metadata": {"name": "aks-delivery-platform",
                     "labels": {"app.kubernetes.io/managed-by": "aks-delivery-templates"}},
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io",
                    "kind": "ClusterRole", "name": "cluster-admin"},
        "subjects": [{"kind": "User", "apiGroup": "rbac.authorization.k8s.io",
                      "name": subject}] if subject else [],
    }
    if objects != {"apiVersion": "v1", "kind": "List", "items": [wanted]}:
        raise ValueError("Platform acceptance may bind only the explicit synthetic platform User.")
    return json.dumps(objects)


def platform_fixture(test, slot):
    group = test.prefix + "-" + slot + ".example.test"
    return {
        "apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
        "metadata": {"name": "platformchecks." + group,
                     "labels": {"app.kubernetes.io/part-of": test.prefix}},
        "spec": {
            "group": group, "scope": "Namespaced",
            "names": {"plural": "platformchecks", "singular": "platformcheck", "kind": "PlatformCheck"},
            "versions": [{
                "name": "v1", "served": True, "storage": True,
                "schema": {"openAPIV3Schema": {"type": "object"}},
            }],
        },
    }


def platform_check(test, cluster, slot):
    """Prove optional platform authority, isolation and revocation against real APIs.

    The synthetic Terraform/discovery inputs exercise the actual shared manifest
    generator. Kind proves Kubernetes authorization, never Azure authentication,
    federation, managed identities, or the live Terraform outputs.
    """
    other = owned_clusters(test, cluster, slot)
    subject = test.prefix + "-platform-" + slot
    application = test.prefix + "-application"
    declaration = platform_manifests(test.args.templates, subject, slot)
    empty = platform_manifests(test.args.templates, None, slot)
    fixture = platform_fixture(test, slot)
    fixture_name = fixture["metadata"]["name"]
    checks = test.record.setdefault("native_platform_rbac_checks", [])

    def record(step, expected, *, selected=cluster, username=subject):
        checks.append({
            "step": step, "cluster": selected, "context": "kind-" + selected,
            "slot": selected.rsplit("-", 1)[1], "subject": username,
            "method": "kubectl-api-operation",
            "expected_allowed": expected, "allowed": expected,
            "binding_generator": "native_authorization.platform_resources",
            "identity_source": "synthetic-kind-fixture",
        })

    def operation(selected, username, command, expected, step, body=None):
        result = as_user(test, selected, username, command, input=body)
        if expected:
            if result.returncode:
                raise AssertionError("Native platform operation failed: " + step)
        else:
            forbidden(result)
        record(step, expected, selected=selected, username=username)

    create = ["create", "--validate=strict", "-f", "-"]
    patch = [
        "patch", "customresourcedefinition", fixture_name, "--type=merge", "--patch",
        '{"metadata":{"annotations":{"native-platform-check":"restored"}}}',
    ]
    try:
        test.kubectl(cluster, "apply", "--validate=strict", "-f", "-", input=declaration)
        operation(cluster, subject, create, True, "platform-created-crd", json.dumps(fixture))
        operation(other, subject, [*create, "--dry-run=server"], False,
                  "unselected-cluster-rejected", json.dumps(fixture))
        operation(cluster, application, [*create, "--dry-run=server"], False,
                  "application-crd-rejected", json.dumps(fixture))
        role_binding = {
            "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
            "metadata": {"name": test.prefix + "-rejected", "namespace": NAMESPACE},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role",
                        "name": "aks-delivery-application"},
            "subjects": [{"kind": "User", "apiGroup": "rbac.authorization.k8s.io",
                          "name": application}],
        }
        operation(cluster, application, [*create, "--dry-run=server"], False,
                  "application-rolebinding-rejected", json.dumps(role_binding))
        test.kubectl(cluster, "apply", "--validate=strict", "-f", "-", input=empty)
        operation(cluster, subject, patch, False, "platform-revoked-crd-rejected")
        test.kubectl(cluster, "apply", "--validate=strict", "-f", "-", input=declaration)
        operation(cluster, subject, patch, True, "platform-restored-crd-patched")
    finally:
        # Both cleanup operations must run even if the first one fails.
        active_error = sys.exc_info()[0] is not None
        cleanup_errors = []
        try:
            test.kubectl(cluster, "apply", "--validate=strict", "-f", "-", input=empty)
            operation(cluster, subject, patch, False, "platform-final-revocation-confirmed")
        except (OSError, subprocess.SubprocessError, AssertionError, ValueError) as error:
            cleanup_errors.append("Platform fixture binding cleanup failed: " + str(error))
        try:
            test.kubectl(cluster, "delete", "customresourcedefinition", fixture_name,
                         "--ignore-not-found=true", "--wait=true", "--timeout=30s")
        except (OSError, subprocess.SubprocessError) as error:
            cleanup_errors.append("Platform fixture CRD cleanup failed: " + str(error))
        test.record.setdefault("cleanup_errors", []).extend(cleanup_errors)
        if cleanup_errors and not active_error:
            raise RuntimeError("; ".join(cleanup_errors))
    print("PASS native platform authority, cluster isolation and revocation " + cluster, flush=True)
