"""Credential-free real rendering and local TLS checks for the maintained profile."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import threading
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("acceptance_gateway", ROOT/"scripts/acceptance_gateway.py")
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


class GatewayProfile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.targets = json.loads((ROOT/"delivery.gateway.apps.json").read_text())["targets"]
        cls.objects = {}
        for target in cls.targets:
            content = subprocess.check_output(
                [os.environ.get("KUBECTL_BIN", "kubectl"), "kustomize", str(ROOT/target["overlay"])], text=True)
            cls.objects[target["slot"]] = list(yaml.safe_load_all(content))

    def test_route_attaches_to_both_exact_platform_listeners_without_legacy_rewrites(self):
        for items in self.objects.values():
            route = next(x for x in items if x["kind"] == "HTTPRoute")
            self.assertEqual(route["spec"]["hostnames"], ["web.example.test", "api.example.test"])
            self.assertEqual(route["spec"]["parentRefs"], [
                {"name":"platform-demo-private","sectionName":"https-web"},
                {"name":"platform-demo-private","sectionName":"https-api"}])
            self.assertEqual(route["spec"]["rules"][0]["matches"],
                             [{"path":{"type":"PathPrefix","value":"/"}}])
            self.assertNotIn("filters", route["spec"]["rules"][0])
            self.assertEqual(route["spec"]["rules"][0]["backendRefs"], [{"name":"platform-demo","port":80}])
            self.assertFalse(any(x["kind"] in ("Gateway","GatewayClass","EnvoyProxy","Namespace","Ingress") for x in items))

    def test_gateway_profile_does_not_claim_private_loadbalancer(self):
        for target in self.targets:
            service = next(x for x in self.objects[target["slot"]] if x["kind"]=="Service")
            self.assertEqual(service["spec"]["type"], "ClusterIP")
            self.assertNotIn("loadBalancerSourceRanges", service["spec"])
            self.assertFalse(any(k.startswith("service.beta.kubernetes.io/azure") for k in service["metadata"].get("annotations",{})))
            self.assertEqual(target["verification"]["ingress"], {
                "gateway":"platform-demo-private","http_routes":["platform-demo"],
                "hosts":["web.example.test","api.example.test"]})

    def test_workload_identity_mount_and_tls_sync_contract(self):
        for items in self.objects.values():
            deployment = next(x for x in items if x["kind"]=="Deployment")
            pod = deployment["spec"]["template"]
            self.assertEqual(pod["metadata"]["labels"]["azure.workload.identity/use"], "true")
            self.assertEqual(pod["spec"]["serviceAccountName"], "platform-demo")
            self.assertEqual(pod["spec"]["volumes"][0]["csi"]["volumeAttributes"]["secretProviderClass"], "platform-demo-tls")
            self.assertNotIn("replicas", deployment["spec"])
            spc = next(x for x in items if x["kind"]=="SecretProviderClass")
            sa = next(x for x in items if x["kind"]=="ServiceAccount")
            self.assertEqual(sa["metadata"]["annotations"]["azure.workload.identity/client-id"],spc["spec"]["parameters"]["clientID"])
            self.assertEqual(spc["spec"]["parameters"]["keyvaultName"], "example-platform-app")
            obj = yaml.safe_load(yaml.safe_load(spc["spec"]["parameters"]["objects"])["array"][0])
            self.assertEqual(obj["objectName"],"platform-demo-ingress")
            self.assertEqual(obj["objectType"],"secret")
            secret = spc["spec"]["secretObjects"][0]
            self.assertEqual(secret["secretName"],"platform-demo-tls")
            self.assertEqual({x["key"] for x in secret["data"]},{"tls.key","tls.crt"})
            self.assertEqual({x["objectName"] for x in secret["data"]},{"platform-demo-ingress"})

    def test_app_policy_does_not_deny_platform_proxy_or_allow_arbitrary_egress(self):
        for items in self.objects.values():
            policies = [x for x in items if x["kind"]=="NetworkPolicy"]
            self.assertEqual(len(policies),2)
            for policy in policies:
                self.assertEqual(policy["spec"]["podSelector"]["matchLabels"],{"app.kubernetes.io/name":"platform-demo"})
            allow = next(x for x in policies if x["metadata"]["name"]=="platform-demo-traffic")["spec"]
            self.assertEqual(allow["ingress"][0]["from"], [{"podSelector":{"matchLabels":{
                "gateway.envoyproxy.io/owning-gateway-name":"platform-demo-private",
                "gateway.envoyproxy.io/owning-gateway-namespace":"platform-demo"}}}])
            self.assertEqual(allow["ingress"][0]["ports"],[{"protocol":"TCP","port":8080}])
            self.assertEqual({p["port"] for e in allow["egress"] for p in e["ports"]},{53})
            self.assertTrue(all("ipBlock" not in peer for e in allow["egress"] for peer in e["to"]))


class StatusTests(unittest.TestCase):
    def test_stale_success_is_rejected(self):
        resource={"metadata":{"generation":2},"status":{"conditions":[
            {"type":"Programmed","status":"True","observedGeneration":1}]}}
        self.assertFalse(gateway.current_conditions(resource,["Programmed"]))
        resource["status"]["conditions"][0]["observedGeneration"]=2
        self.assertTrue(gateway.current_conditions(resource,["Programmed"]))

    def test_both_listener_statuses_are_required(self):
        route={"metadata":{"generation":3},"status":{"parents":[]}}
        for section in ("https-web","https-api"):
            route["status"]["parents"].append({
                "parentRef":{"name":"platform-demo-private","sectionName":section},
                "controllerName":"gateway.envoyproxy.io/gatewayclass-controller",
                "conditions":[{"type":c,"status":"True","observedGeneration":3} for c in ("Accepted","ResolvedRefs")]})
        self.assertTrue(gateway.ready_route(route))
        route["status"]["parents"][1]["conditions"][0]["observedGeneration"]=2
        self.assertFalse(gateway.ready_route(route))
        route["status"]["parents"].pop()
        self.assertFalse(gateway.ready_route(route))


class TLSChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix="demo-tls-unit-")
        cls.root=Path(cls.temp.name)
        subprocess.run(["openssl","req","-x509","-newkey","rsa:2048","-nodes","-days","1",
            "-subj","/CN=web.example.test","-addext","subjectAltName=DNS:web.example.test",
            "-keyout",str(cls.root/"tls.key"),"-out",str(cls.root/"tls.crt")],
            check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"host":self.headers["Host"],"path":self.path}).encode())
            def log_message(self,*args):pass
        cls.server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cls.root/"tls.crt",cls.root/"tls.key")
        cls.server.socket=context.wrap_socket(cls.server.socket,server_side=True)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.temp.cleanup()

    def test_validated_sni_and_host_can_use_loopback_forward(self):
        value=gateway.https_json(self.server.server_port,"web.example.test","/api/version",self.root/"tls.crt")
        self.assertEqual(value,{"host":"web.example.test","path":"/api/version"})

    def test_hostname_mismatch_is_rejected_without_tls_bypass(self):
        with self.assertRaises(ssl.SSLCertVerificationError):
            gateway.https_json(self.server.server_port,"wrong.example.test","/version",self.root/"tls.crt")


if __name__=="__main__":unittest.main()
