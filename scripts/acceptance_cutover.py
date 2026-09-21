"""Loopback-only TCP traffic switching for disposable two-cluster acceptance.

TLS terminates in the real Envoy Gateway on the selected cluster. This adapter
models connection routing, not Azure Application Gateway or WAF behavior.
"""
from contextlib import ExitStack, contextmanager
import json
import re
import selectors
import socket
import socketserver
import threading

import acceptance_gateway as gateway


class _Connection(socketserver.BaseRequestHandler):
    def handle(self):
        relay = self.server
        with relay.lock:
            port = relay.backends[relay.active]
        upstream = None
        try:
            upstream = socket.create_connection(("127.0.0.1", port), timeout=5)
            self.request.settimeout(5)
            upstream.settimeout(5)
            with relay.lock:
                relay.connections.update((self.request, upstream))
            with selectors.DefaultSelector() as selector:
                selector.register(self.request, selectors.EVENT_READ, upstream)
                selector.register(upstream, selectors.EVENT_READ, self.request)
                while not relay.stopping.is_set():
                    for key, _ in selector.select(timeout=0.25):
                        content = key.fileobj.recv(65536)
                        if not content:
                            return
                        key.data.sendall(content)
        except OSError:
            # A closed client/upstream ends this connection, never changes target.
            return
        finally:
            with relay.lock:
                relay.connections.discard(self.request)
                if upstream is not None:
                    relay.connections.discard(upstream)
            if upstream is not None:
                upstream.close()


class Relay(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def __init__(self, backends):
        if set(backends) != {"aks01", "aks02"} or any(
                type(port) is not int or not 1 <= port <= 65535 for port in backends.values()):
            raise ValueError("Traffic adapter requires exactly two loopback backend ports.")
        self.backends = dict(backends)
        self.active = "aks01"
        self.lock = threading.Lock()
        self.connections = set()
        self.stopping = threading.Event()
        super().__init__(("127.0.0.1", 0), _Connection)
        self.worker = threading.Thread(target=self.serve_forever, daemon=True)
        self.worker.start()

    @property
    def port(self):
        return self.server_address[1]

    def switch(self, slot):
        if slot not in self.backends:
            raise ValueError("Unknown traffic slot.")
        with self.lock:
            self.active = slot

    def close(self):
        self.stopping.set()
        self.shutdown()
        with self.lock:
            connections = list(self.connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.server_close()
        self.worker.join(timeout=5)


@contextmanager
def endpoint(test, forward):
    expected = [test.prefix + "-" + slot for slot in ("aks01", "aks02")]
    if not re.fullmatch(r"aks-demo-[0-9a-f]{8}", test.prefix) or test.clusters != expected:
        raise ValueError("Traffic switching requires the two owned disposable kind clusters.")
    with ExitStack() as stack:
        backends = {}
        for slot, cluster in zip(("aks01", "aks02"), test.clusters):
            services = json.loads(test.kubectl(
                cluster, "-n", gateway.NAMESPACE, "get", "services",
                "-l", gateway.SELECTOR, "-o", "json", capture=True))["items"]
            if len(services) != 1:
                raise ValueError("Expected one ready Gateway proxy Service per slot.")
            url = stack.enter_context(forward(
                test.args.kubectl, test.kubeconfig, "kind-" + cluster,
                service=services[0]["metadata"]["name"], remote_port=443,
                diagnostics_dir=test.artifacts))
            backends[slot] = int(url.rsplit(":", 1)[1])
        relay = Relay(backends)
        try:
            test.record["traffic_adapter"] = {
                "kind": "loopback-tcp-relay", "address": "127.0.0.1", "port": relay.port,
                "tls_termination": "selected-cluster-envoy",
                "connection_behavior": "existing connections retain their selected backend",
                "qualification_limit": "Does not emulate Azure WAF, DNS, health probes or connection draining policy",
            }
            yield relay
        finally:
            relay.close()


def check(test, relay, slot, revision, step):
    results = []
    for host, path in (("web.example.test", "/version"),
                       ("api.example.test", "/api/version")):
        data = gateway.https_json(relay.port, host, path, test.ca_file)
        if (data.get("slot") != slot or data.get("revision") != revision
                or data.get("application") != "aks-platform-demo"):
            raise AssertionError("Stable endpoint returned the wrong slot or source revision.")
        results.append({"host": host, "path": path, "slot": data["slot"],
                        "source_commit": data["revision"], "tls_verified": True})
    test.record.setdefault("traffic_checks", []).append({
        "step": step, "endpoint": "127.0.0.1:" + str(relay.port),
        "expected_slot": slot, "responses": results})
    print("PASS stable endpoint " + step + " -> " + slot, flush=True)
