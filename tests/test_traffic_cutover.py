"""Real local socket checks for the disposable traffic adapter."""
from pathlib import Path
import socket
import socketserver
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from acceptance_cutover import Relay


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        while True:
            body = self.request.recv(1024)
            if not body:
                return
            self.request.sendall(self.server.label + body)


class TrafficAdapterTests(unittest.TestCase):
    def setUp(self):
        self.backends = []
        for label in (b"one:", b"two:"):
            server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Echo)
            server.daemon_threads = True
            server.label = label
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.backends.append(server)
        self.relay = Relay({slot: server.server_address[1]
                            for slot, server in zip(("aks01", "aks02"), self.backends)})

    def tearDown(self):
        self.relay.close()
        for server in self.backends:
            server.shutdown()
            server.server_close()

    def connect(self):
        return socket.create_connection(("127.0.0.1", self.relay.port), timeout=3)

    def test_same_listener_switches_new_connections_and_preserves_existing_stream(self):
        address = self.relay.server_address
        self.assertEqual(address[0], "127.0.0.1")
        with self.connect() as first:
            first.sendall(b"start")
            self.assertEqual(first.recv(1024), b"one:start")
            self.relay.switch("aks02")
            first.sendall(b"existing")
            self.assertEqual(first.recv(1024), b"one:existing")
            with self.connect() as second:
                second.sendall(b"new")
                self.assertEqual(second.recv(1024), b"two:new")
            self.relay.switch("aks01")
            with self.connect() as rolled_back:
                rolled_back.sendall(b"rollback")
                self.assertEqual(rolled_back.recv(1024), b"one:rollback")
        self.assertEqual(self.relay.server_address, address)

    def test_unknown_target_cannot_change_route(self):
        with self.assertRaises(ValueError):
            self.relay.switch("production")
        self.assertEqual(self.relay.active, "aks01")

    def test_invalid_backend_contract_rejected_before_listener(self):
        for backends in ({}, {"aks01": 80, "aks02": "443"},
                         {"aks01": 80, "aks02": 0}, {"aks01": 80, "aks02": True}):
            with self.assertRaises(ValueError):
                Relay(backends)


if __name__ == "__main__":
    unittest.main()
