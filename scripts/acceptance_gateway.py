"""Envoy/TLS acceptance support. All keys and cluster changes are test-local."""
import http.client
import json
from pathlib import Path
import socket
import ssl
import time
import yaml

GATEWAY = "platform-demo-private"
NAMESPACE = "platform-demo"
SELECTOR = ("gateway.envoyproxy.io/owning-gateway-name=" + GATEWAY +
            ",gateway.envoyproxy.io/owning-gateway-namespace=" + NAMESPACE)


def current_conditions(resource, required, *, conditions=None):
    """Reject stale successful conditions from before the current spec update."""
    generation = resource["metadata"]["generation"]
    values = resource.get("status", {}).get("conditions", []) if conditions is None else conditions
    return all(any(c.get("type") == kind and c.get("status") == "True" and
                   c.get("observedGeneration") == generation for c in values)
               for kind in required)


def ready_route(route):
    parents = route.get("status", {}).get("parents", [])
    for section in ("https-web", "https-api"):
        matching = [p for p in parents if
                    p.get("parentRef", {}).get("name") == GATEWAY and
                    p.get("parentRef", {}).get("namespace", NAMESPACE) == NAMESPACE and
                    p.get("parentRef", {}).get("sectionName") == section and
                    p.get("controllerName") == "gateway.envoyproxy.io/gatewayclass-controller"]
        if len(matching) != 1 or not current_conditions(
                route, ("Accepted", "ResolvedRefs"), conditions=matching[0].get("conditions", [])):
            return False
    return True


def https_json(port, host, path, ca_file, *, request_host=None):
    """Connect to loopback but verify the real route hostname and send its SNI."""
    context = ssl.create_default_context(cafile=str(ca_file))
    connection = http.client.HTTPSConnection(host, port, timeout=10, context=context)
    raw = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        connection.sock = context.wrap_socket(raw, server_hostname=host)
        connection.request("GET", path, headers={"Host": request_host or host, "Accept": "application/json"})
        response = connection.getresponse()
        body = response.read(16385)
        if response.status != 200 or len(body) > 16384:
            raise ValueError("Gateway HTTPS check returned non-200 or an oversized body.")
        return json.loads(body)
    finally:
        connection.close()
        raw.close()


def prepare_files(test, run):
    directory = test.work / "gateway"
    directory.mkdir()
    test.gateway_dir = directory
    test.ca_file = directory / "ca.crt"
    run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
         "-subj", "/CN=Ephemeral AKS demo acceptance CA",
         "-addext", "basicConstraints=critical,CA:TRUE",
         "-addext", "keyUsage=critical,keyCertSign,cRLSign",
         "-keyout", directory / "ca.key", "-out", test.ca_file], capture=True)
    run(["openssl", "req", "-newkey", "rsa:2048", "-nodes",
         "-subj", "/CN=web.example.test", "-keyout", directory / "tls.key",
         "-out", directory / "tls.csr"], capture=True)
    (directory / "extensions.cnf").write_text(
        "subjectAltName=DNS:web.example.test,DNS:api.example.test\n"
        "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n")
    run(["openssl", "x509", "-req", "-in", directory / "tls.csr",
         "-CA", test.ca_file, "-CAkey", directory / "ca.key", "-CAcreateserial", "-days", "2",
         "-extfile", directory / "extensions.cnf", "-out", directory / "tls.crt"], capture=True)
    # Only the isolated Git fixture changes. Azure CSI cannot operate in kind.
    base = test.fixture / "deploy/gateway-api/base"
    (base / "secret-provider-class.yaml").unlink()
    kustomization = yaml.safe_load((base / "kustomization.yaml").read_text())
    kustomization["resources"].remove("secret-provider-class.yaml")
    (base / "kustomization.yaml").write_text(yaml.safe_dump(kustomization, sort_keys=False))
    (base / "deployment-patch.yaml").write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: platform-demo\n"
        "spec:\n  replicas: 2\n  template:\n    spec:\n      serviceAccountName: platform-demo\n")
    account = yaml.safe_load((base / "service-account.yaml").read_text())
    account["metadata"].pop("annotations", None)
    (base / "service-account.yaml").write_text(yaml.safe_dump(account, sort_keys=False))
    # Keep the fixture's verification declaration honest even though checks run below.
    config = json.loads((test.fixture / "delivery.gateway.apps.json").read_text())
    trust = test.fixture / "deploy/trust/acceptance-ca.crt"
    trust.parent.mkdir(parents=True, exist_ok=True)
    trust.write_bytes(test.ca_file.read_bytes())
    for target in config["targets"]:
        target["verification"]["ingress"]["ca_file"] = "deploy/trust/acceptance-ca.crt"
    (test.fixture / "delivery.gateway.apps.json").write_text(json.dumps(config, indent=2)+"\n")
    # The same shared engine that serves Azure prepares the platform fixtures.
    # Imports remain local so standalone HTTPS helper tests need no extra modules.
    import acceptance_engines
    acceptance_engines.prepare_platform_fixture(test)


def install(test, cluster, run):
    import acceptance_engines
    slot = cluster.rsplit("-", 1)[-1]
    acceptance_engines.install_platform(test, cluster, slot)
    # Azure CSI is not imitated. Inject only this run's temporary local TLS fixture.
    directory = test.gateway_dir
    secret = test.kubectl(
        cluster, "-n", NAMESPACE, "create", "secret", "tls", "platform-demo-tls",
        "--cert=" + str(directory / "tls.crt"), "--key=" + str(directory / "tls.key"),
        "--dry-run=client", "-o", "yaml", capture=True)
    test.kubectl(cluster, "apply", "-f", "-", input=secret)


def check(test, cluster, slot, commit, forward):
    deadline = time.monotonic() + 300
    while True:
        gateway = json.loads(test.kubectl(
            cluster, "-n", NAMESPACE, "get", "gateway/" + GATEWAY, "-o", "json", capture=True))
        route = json.loads(test.kubectl(
            cluster, "-n", NAMESPACE, "get", "httproute/platform-demo", "-o", "json", capture=True))
        if current_conditions(gateway, ("Programmed",)) and ready_route(route):
            break
        if time.monotonic() >= deadline:
            raise TimeoutError("Gateway/HTTPRoute did not become current-generation ready.")
        time.sleep(2)
    services = json.loads(test.kubectl(
        cluster, "-n", NAMESPACE, "get", "services", "-l", SELECTOR, "-o", "json", capture=True))["items"]
    if len(services) != 1:
        raise ValueError("Expected exactly one selected Gateway proxy Service.")
    service = services[0]["metadata"]["name"]
    def tunnel():
        return forward(test.args.kubectl, test.kubeconfig, "kind-" + cluster,
                       service=service, remote_port=443, diagnostics_dir=test.artifacts)
    # Rejected SNI can close a kubectl transport. Never reuse its tunnel for
    # subsequent assertions, or mistake a dead local listener for route rejection.
    with tunnel() as url:
        port = int(url.rsplit(":", 1)[1])
        for host, prefix in (("web.example.test", ""), ("api.example.test", "/api")):
            for endpoint, expected in (("healthz", "ok"), ("readyz", "ready"), ("version", None)):
                path = prefix + "/" + endpoint
                print(f"Gateway HTTPS check: {slot} {host}{path}", flush=True)
                data = https_json(port, host, path, test.ca_file)
                if expected is not None and data.get("status") != expected:
                    raise ValueError("Gateway application health response mismatch.")
                if expected is None and (data.get("slot") != slot or data.get("revision") != commit or
                                         data.get("application") != "aks-platform-demo"):
                    raise ValueError("Gateway selected slot/revision mismatch.")
                print(f"PASS Gateway HTTPS: {slot} {host}{path}", flush=True)
    with tunnel() as url:
        port = int(url.rsplit(":", 1)[1])
        print(f"Gateway negative Host check: {slot}", flush=True)
        try:
            https_json(port, "web.example.test", "/version", test.ca_file,
                       request_host="unmatched.example.test")
        except ValueError as error:
            if "non-200" not in str(error):
                raise
        else:
            raise ValueError("Gateway accepted an unmatched HTTP Host.")
    with tunnel() as url:
        port = int(url.rsplit(":", 1)[1])
        print(f"Gateway negative SNI check: {slot}", flush=True)
        try:
            https_json(port, "untrusted.example.test", "/version", test.ca_file)
        except (ssl.SSLError, ConnectionResetError):
            # An unmatched SNI may be rejected before a certificate is sent.
            # ConnectionRefusedError is intentionally not an accepted rejection.
            pass
        else:
            raise ValueError("Gateway accepted an unexpected certificate hostname.")
    return {"gateway_generation": gateway["metadata"]["generation"],
            "route_generation": route["metadata"]["generation"],
            "https_hosts": ["web.example.test", "api.example.test"],
            "tls_hostname_rejection": True, "unmatched_host_rejection": True}
