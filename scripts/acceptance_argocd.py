"""Argo-only acceptance support: isolated Git serving and revision-bound checks."""
from __future__ import annotations

import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import re
import shutil
import threading
import time

COMMIT = re.compile(r"[0-9a-f]{40}")
SLOTS = ("aks01", "aks02")


def bridge_gateway(config):
    addresses = []
    for item in config:
        try:
            address = ipaddress.ip_address(item.get("Gateway", ""))
        except ValueError:
            continue
        if (address.version == 4 and address.is_private and not address.is_loopback
                and not address.is_unspecified and not address.is_link_local):
            addresses.append(str(address))
    if len(addresses) != 1:
        raise ValueError("Require one private IPv4 Docker kind bridge gateway.")
    return addresses[0]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


class GitFixture:
    """A credentials-free bare repository, readable only on the chosen bridge IP."""
    def __init__(self, directory, address, run):
        self.root, self.run = Path(directory), run
        self.checkout = self.root / "checkout"
        self.served = self.root / "http"
        self.bare = self.served / "releases.git"
        self.server = self.thread = None
        self.served.mkdir(parents=True)
        self.run(["git", "init", "--quiet", "--initial-branch=main", self.checkout])
        self.run(["git", "init", "--quiet", "--bare", "--initial-branch=main", self.bare])
        self.run(["git", "-C", self.checkout, "remote", "add", "origin", self.bare])
        handler = functools.partial(QuietHandler, directory=str(self.served))
        self.server = ThreadingHTTPServer((address, 0), handler)
        self.server.daemon_threads = True
        self.url = f"http://{address}:{self.server.server_port}/releases.git"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self):
        return self.server.server_port

    def slot_path(self, slot):
        if slot not in SLOTS:
            raise ValueError("Explicit acceptance slot required.")
        return self.checkout / "gitops/releases/pprd/uks" / slot

    def replace(self, slot, materialized):
        target = self.slot_path(slot)
        allowed = {"kustomization.yaml", "manifest.yaml", "release.json", "ingress-ca.pem"}
        source = list(Path(materialized).iterdir())
        if not source or any(p.is_symlink() or not p.is_file() or p.name not in allowed for p in source):
            raise ValueError("Unexpected materialized release content.")
        if target.exists():
            if target.is_symlink() or any(p.is_symlink() or not p.is_file() or p.name not in allowed
                                          for p in target.iterdir()):
                raise ValueError("Refusing to replace unexpected Git fixture content.")
            for old in target.iterdir():
                old.unlink()
        target.mkdir(parents=True, exist_ok=True)
        for path in source:
            shutil.copyfile(path, target / path.name)

    def commit(self, message):
        self.run(["git", "-C", self.checkout, "add", "gitops"])
        self.run(["git", "-C", self.checkout, "-c", "user.name=Acceptance Fixture",
                  "-c", "user.email=fixture@example.test", "-c", "commit.gpgsign=false",
                  "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", message])
        revision = self.run(["git", "-C", self.checkout, "rev-parse", "HEAD"], capture=True)
        self.run(["git", "-C", self.checkout, "push", "--quiet", "origin", "main"])
        self.run(["git", "--git-dir", self.bare, "update-server-info"])
        return revision

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout=5)


def status_summary(application):
    status = application.get("status", {})
    operation = status.get("operationState", {})
    return {
        "application": application.get("metadata", {}).get("name"),
        "sync_revision": status.get("sync", {}).get("revision"),
        "sync_status": status.get("sync", {}).get("status"),
        "health": status.get("health", {}).get("status"),
        "operation_revision": operation.get("syncResult", {}).get("revision"),
        "operation_phase": operation.get("phase"),
    }


def evaluate(application, revision, expectation):
    """Never accept stale success/failure or mere timeout as negative-test proof."""
    if not COMMIT.fullmatch(revision):
        raise ValueError("A full expected Git commit is required.")
    if expectation not in ("healthy", "rejected", "drift"):
        raise ValueError("Unknown acceptance expectation.")
    state = status_summary(application)
    status = application.get("status", {})
    operation = status.get("operationState", {})
    current = state["operation_revision"] == revision
    if expectation == "rejected":
        rejected = [item for item in operation.get("syncResult", {}).get("resources", [])
                    if item.get("kind") == "Deployment" and item.get("name") == "platform-demo"
                    and item.get("status") == "SyncFailed" and item.get("message")]
        if current and state["operation_phase"] == "Succeeded":
            raise AssertionError("Invalid desired Deployment unexpectedly synced successfully.")
        return bool(current and state["operation_phase"] == "Failed" and rejected)
    errors = [c.get("message", c.get("type")) for c in status.get("conditions", [])
              if c.get("type") in ("InvalidSpecError", "ComparisonError", "SyncError")]
    if current and state["operation_phase"] in ("Error", "Failed"):
        raise RuntimeError("Argo reconciliation failed: " + "; ".join(errors or [operation.get("message", "")]))
    # Conditions from the previous rejected revision can survive while the new
    # operation is Running. Wait for them to clear rather than failing recovery.
    if errors:
        return False
    if expectation == "drift":
        return state["sync_revision"] == revision and state["sync_status"] == "OutOfSync"
    return (current and state["operation_phase"] == "Succeeded"
            and state["sync_revision"] == revision and state["sync_status"] == "Synced"
            and state["health"] == "Healthy" and not application.get("operation"))


def wait_application(fetch, revision, expectation="healthy", *, timeout=600, sleep=time.sleep,
                     monotonic=time.monotonic):
    deadline = monotonic() + timeout
    last = {}
    while True:
        last = fetch()
        if evaluate(last, revision, expectation):
            return last
        if monotonic() >= deadline:
            raise TimeoutError("Argo did not prove " + expectation + ": " + json.dumps(status_summary(last)))
        sleep(2)


def sync_patch(revision):
    if not COMMIT.fullmatch(revision):
        raise ValueError("Sync requires a full Git commit.")
    return {"operation": {"initiatedBy": {"username": "isolated-acceptance"},
                          "sync": {"revision": revision, "prune": False,
                                   "syncStrategy": {"apply": {"force": False}}}}}


def invalid_deployment(manifest):
    """Deliberately invalid API input: independent of HPA replica ignore rules."""
    import yaml
    objects = list(yaml.safe_load_all(manifest))
    selected = [obj for obj in objects if obj and obj.get("kind") == "Deployment"
                and obj.get("metadata", {}).get("name") == "platform-demo"]
    if len(selected) != 1:
        raise ValueError("Require exactly one application Deployment.")
    selected[0]["spec"]["template"]["spec"]["containers"][0]["ports"][0]["containerPort"] = 70000
    return yaml.safe_dump_all(objects, sort_keys=False)


def drift_patch(deployment):
    containers = deployment["spec"]["template"]["spec"]["containers"]
    selected = [(i, j) for i, container in enumerate(containers) if container["name"] == "app"
                for j, value in enumerate(container.get("env", [])) if value["name"] == "APP_SLOT"]
    if len(selected) != 1:
        raise ValueError("Require exactly one application APP_SLOT binding.")
    container, env = selected[0]
    return [{"op": "replace", "path": f"/spec/template/spec/containers/{container}/env/{env}/value",
             "value": "local"}]


def sanitize_diagnostic(value):
    value = re.sub(r"-----BEGIN (?:[A-Z ]+)?PRIVATE KEY-----.*?"
                   r"-----END (?:[A-Z ]+)?PRIVATE KEY-----",
                   "[REDACTED PRIVATE KEY]", value, flags=re.DOTALL)
    value = re.sub(r"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})",
                   "[REDACTED TOKEN]", value)
    value = re.sub(r"(?i)(authorization:\s*bearer\s+)\S+", r"\1[REDACTED]", value)
    return value[:200_000]


def authorization_answer(returncode, stdout):
    """kubectl can-i uses exit 1 for denial; transport errors must never count."""
    answer = stdout.strip()
    if returncode == 0 and answer == "yes":
        return True
    if returncode == 1 and answer == "no":
        return False
    raise RuntimeError("kubectl auth can-i did not return an unambiguous authorization answer.")
