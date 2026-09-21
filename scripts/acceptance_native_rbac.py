"""Real native application RBAC checks confined to owned disposable kind clusters."""
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
