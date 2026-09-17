"""Argo-only acceptance support: isolated Git serving and revision-bound checks."""
from __future__ import annotations

from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from urllib.parse import urlsplit

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


class SmartGitHandler(BaseHTTPRequestHandler):
    """Expose only anonymous read-only smart Git RPC for one synthetic repository."""
    MAX_BODY = 2 * 1024 * 1024
    MAX_RESPONSE = 8 * 1024 * 1024

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, format, *args):
        pass

    def reply(self, status, body=b"", headers=None):
        self.server.events.append({"method": self.command, "path": urlsplit(self.path).path,
                                   "status": status})
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.serve_git()

    def do_POST(self):
        self.serve_git()

    def serve_git(self):
        url = urlsplit(self.path)
        advertise = (self.command == "GET" and url.path == "/releases.git/info/refs"
                     and url.query == "service=git-upload-pack")
        fetch = (self.command == "POST" and url.path == "/releases.git/git-upload-pack"
                 and not url.query)
        if not (advertise or fetch):
            self.reply(403, b"Only read-only Git upload-pack is available.\n")
            return
        if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Encoding"):
            self.reply(400, b"Unsupported request encoding.\n")
            return
        lengths = self.headers.get_all("Content-Length", [])
        try:
            length = int(lengths[0]) if len(lengths) == 1 else 0
        except ValueError:
            length = -1
        if (len(lengths) > 1 or length < 0 or length > self.MAX_BODY
                or (fetch and len(lengths) != 1) or (advertise and length != 0)):
            self.reply(413, b"Invalid or oversized Git request.\n")
            return
        protocol = self.headers.get("Git-Protocol", "")
        if protocol not in ("", "version=0", "version=1", "version=2"):
            self.reply(400, b"Unsupported Git protocol.\n")
            return
        if fetch and self.headers.get("Content-Type") != "application/x-git-upload-pack-request":
            self.reply(415, b"Git upload-pack content type required.\n")
            return
        try:
            body = self.rfile.read(length)
            if len(body) != length:
                self.reply(400, b"Incomplete Git request.\n")
                return
            # Do not pass the CI environment, credentials, proxy settings, Git
            # overrides or global configuration to the backend process.
            env = {
                "PATH": str(Path(self.server.git_binary).parent) + ":/usr/bin:/bin",
                "HOME": str(self.server.git_home), "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
                "GIT_PROJECT_ROOT": str(self.server.git_root),
                "PATH_INFO": url.path, "QUERY_STRING": url.query,
                "REQUEST_METHOD": self.command, "REMOTE_ADDR": self.client_address[0],
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(length), "SERVER_PROTOCOL": self.request_version,
            }
            if protocol:
                env["GIT_PROTOCOL"] = protocol
            result = subprocess.run([self.server.git_binary, "http-backend"],
                                    input=body, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=env, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            self.reply(502, b"Git backend request failed.\n")
            return
        if result.returncode or len(result.stdout) > self.MAX_RESPONSE:
            self.reply(502, b"Git backend response failed validation.\n")
            return
        raw_headers, separator, payload = result.stdout.partition(b"\r\n\r\n")
        if not separator:
            raw_headers, separator, payload = result.stdout.partition(b"\n\n")
        if not separator:
            self.reply(502, b"Git backend did not return CGI headers.\n")
            return
        status, headers = 200, {}
        for line in raw_headers.decode("ascii", errors="strict").splitlines():
            key, sep, value = line.partition(":")
            if not sep:
                self.reply(502, b"Malformed CGI header.\n")
                return
            if key.lower() == "status":
                status = int(value.strip().split()[0])
            elif key.lower() in ("content-type", "cache-control", "expires", "pragma"):
                headers[key] = value.strip()
        self.reply(status, payload, headers)


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
        self.run(["git", "--git-dir", self.bare, "config", "http.receivepack", "false"])
        self.run(["git", "--git-dir", self.bare, "config", "http.getanyfile", "false"])
        (self.bare / "git-daemon-export-ok").touch()
        self.server = ThreadingHTTPServer((address, 0), SmartGitHandler)
        self.server.git_binary = shutil.which("git")
        self.server.git_root = self.served
        self.server.git_home = self.root / "empty-home"
        self.server.git_home.mkdir()
        self.server.events = deque(maxlen=200)
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
        allowed = {"manifest.yaml", "release.json", "ingress-ca.pem"}
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
        if current and not application.get("operation") and state["operation_phase"] == "Succeeded":
            raise AssertionError("Invalid desired Deployment unexpectedly synced successfully.")
        return bool(current and not application.get("operation") and state["operation_phase"] == "Failed" and rejected)
    errors = [c.get("message", c.get("type")) for c in status.get("conditions", [])
              if c.get("type") in ("InvalidSpecError", "ComparisonError", "SyncError")]
    if current and not application.get("operation") and state["operation_phase"] in ("Error", "Failed"):
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


def authorization_answer(returncode, stdout, stderr=""):
    """kubectl can-i uses exit 1 for denial; transport errors must never count."""
    if "doesn't have a resource type" in stderr:
        raise RuntimeError("Authorization probe cannot rely on an undiscovered Kubernetes resource.")
    answer = stdout.strip()
    if returncode == 0 and answer == "yes":
        return True
    if returncode == 1 and answer == "no":
        return False
    raise RuntimeError("kubectl auth can-i did not return an unambiguous authorization answer.")


def authorization_review(resource, namespace, verb):
    """Specify group explicitly even when the synthetic cluster lacks the CRD."""
    name, _, group = resource.partition(".")
    attributes = {"group": group, "resource": name, "verb": verb}
    if namespace is not None:
        attributes["namespace"] = namespace
    return {"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectAccessReview",
            "spec": {"resourceAttributes": attributes}}


def authorization_review_answer(returncode, stdout):
    if returncode != 0:
        raise RuntimeError("Kubernetes authorization review request failed.")
    try:
        status = json.loads(stdout)["status"]
    except (ValueError, KeyError, TypeError) as error:
        raise RuntimeError("Kubernetes authorization review returned malformed status.") from error
    if (type(status.get("allowed")) is not bool or status.get("evaluationError")
            or (status["allowed"] and status.get("denied"))):
        raise RuntimeError("Kubernetes authorization review was inconclusive.")
    return status["allowed"]
