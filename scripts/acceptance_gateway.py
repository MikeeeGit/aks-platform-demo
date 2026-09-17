"""Envoy/TLS acceptance support. All keys and cluster changes are test-local."""
from contextlib import contextmanager
import hashlib
import http.client
import json
from pathlib import Path
import socket
import ssl
import time
from urllib.request import urlopen
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
    pins = test.pins["envoy_gateway"]
    directory = test.work / "gateway"
    directory.mkdir()
    test.gateway_dir = directory
    for index, item in enumerate(pins["crds"]):
        content = urlopen(item["url"], timeout=90).read()
        if hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise ValueError("Envoy/Gateway API CRD checksum mismatch.")
        (directory / f"crds-{index}.yaml").write_bytes(content)
    run([test.args.helm, "pull", pins["chart"], "--destination", directory], timeout=180)
    charts = list(directory.glob("*.tgz"))
    if len(charts) != 1 or hashlib.sha256(charts[0].read_bytes()).hexdigest() != pins["chart_sha256"]:
        raise ValueError("Envoy Helm package checksum mismatch.")
    test.envoy_chart = charts[0]
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
    values = {"crds": {"enabled": False}, "config": {"envoyGateway": {
        "provider": {"type": "Kubernetes", "kubernetes": {
            "deploy": {"type": "GatewayNamespace"},
            "watch": {"type": "Namespaces", "namespaces": [NAMESPACE, "envoy-gateway-system"]}}}}}}
    for key, value in pins.get("images", {}).items():
        values.setdefault("global", {}).setdefault("images", {})[key] = {"image": value}
    (directory / "values.yaml").write_text(yaml.safe_dump(values))


def install(test, cluster, run):
    directory = test.gateway_dir
    for path in sorted(directory.glob("crds-*.yaml")):
        objects = list(yaml.safe_load_all(path.read_text()))
        crds = [item for item in objects if item and item.get("kind") == "CustomResourceDefinition"]
        other = [item for item in objects if item and item.get("kind") != "CustomResourceDefinition"]
        test.kubectl(cluster, "apply", "--server-side", "-f", "-",
                     input=yaml.safe_dump_all(crds))
        for crd in crds:
            test.kubectl(cluster, "wait", "--for=condition=Established",
                         "crd/" + crd["metadata"]["name"], "--timeout=120s")
        if other:
            test.kubectl(cluster, "apply", "--server-side", "-f", "-",
                         input=yaml.safe_dump_all(other))
    run([test.args.helm, "upgrade", "--install", "envoy-gateway", test.envoy_chart,
         "--namespace", "envoy-gateway-system", "--create-namespace",
         "--kubeconfig", test.kubeconfig, "--kube-context", "kind-" + cluster,
         "--values", directory / "values.yaml", "--wait", "--timeout", "300s"], timeout=420)
    # Real TLS with a temporary trusted CA; no Azure CSI imitation.
    secret = test.kubectl(
        cluster, "-n", NAMESPACE, "create", "secret", "tls", "platform-demo-tls",
        "--cert=" + str(directory / "tls.crt"), "--key=" + str(directory / "tls.key"),
        "--dry-run=client", "-o", "yaml", capture=True)
    test.kubectl(cluster, "apply", "-f", "-", input=secret)
    profile = test.args.templates / "examples/platform-envoy"
    proxy = yaml.safe_load((profile / "manifests/proxy-aks01.yaml").read_text())
    kubernetes = proxy["spec"]["provider"]["kubernetes"]
    expected = test.pins["envoy_gateway"]["images"]
    if kubernetes["envoyDeployment"]["container"]["image"] != expected["envoyProxy"]:
        raise ValueError("Platform proxy image does not match the acceptance pin.")
    shutdown = kubernetes["envoyDeployment"]["patch"]["value"]["spec"]["template"]["spec"]["containers"]
    if not any(c.get("name") == "shutdown-manager" and c.get("image") == expected["envoyGateway"] for c in shutdown):
        raise ValueError("Platform shutdown-manager image does not match the acceptance pin.")
    kubernetes["envoyService"] = {"type": "ClusterIP"}
    kubernetes.pop("envoyHpa", None)
    kubernetes["envoyDeployment"]["replicas"] = 1
    # Preserve pinned images and security; reduce only test capacity/replica count.
    kubernetes["envoyDeployment"]["container"]["resources"] = {
        "requests": {"cpu": "100m", "memory": "128Mi"},
        "limits": {"cpu": "500m", "memory": "512Mi"}}
    test.kubectl(cluster, "apply", "--server-side", "-f", "-", input=yaml.safe_dump(proxy))
    test.kubectl(cluster, "apply", "--server-side", "-f", profile / "manifests/gateway.yaml")


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
    with forward(test.args.kubectl, test.kubeconfig, "kind-" + cluster,
                 service=services[0]["metadata"]["name"], remote_port=443) as url:
        port = int(url.rsplit(":", 1)[1])
        for host, prefix in (("web.example.test", ""), ("api.example.test", "/api")):
            for endpoint, expected in (("healthz", "ok"), ("readyz", "ready"), ("version", None)):
                data = https_json(port, host, prefix + "/" + endpoint, test.ca_file)
                if expected is not None and data.get("status") != expected:
                    raise ValueError("Gateway application health response mismatch.")
                if expected is None and (data.get("slot") != slot or data.get("revision") != commit or
                                         data.get("application") != "aks-platform-demo"):
                    raise ValueError("Gateway selected slot/revision mismatch.")
        # A certificate for the wrong name must never pass a 'smoke' test.
        try:
            https_json(port, "untrusted.example.test", "/version", test.ca_file)
        except (ssl.SSLError, ConnectionError):
            # An unmatched SNI may be rejected before a certificate is sent.
            pass
        else:
            raise ValueError("Gateway accepted an unexpected certificate hostname.")
        try:
            https_json(port, "web.example.test", "/version", test.ca_file,
                       request_host="unmatched.example.test")
        except ValueError as error:
            if "non-200" not in str(error):
                raise
        else:
            raise ValueError("Gateway accepted an unmatched HTTP Host.")
    return {"gateway_generation": gateway["metadata"]["generation"],
            "route_generation": route["metadata"]["generation"],
            "https_hosts": ["web.example.test", "api.example.test"],
            "tls_hostname_rejection": True, "unmatched_host_rejection": True}
