#!/usr/bin/env python3
"""Verify a selected Application Gateway endpoint, backend health and HTTPS release."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import http.client
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import time

RESOURCE = re.compile(r"/subscriptions/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})/resourceGroups/([^/]+)/providers/Microsoft.Network/applicationGateways/([^/]+)", re.I)
HOST = re.compile(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?")
STEPS = ("initial-active", "standby-updated-active-unchanged", "traffic-cutover", "traffic-rollback")


def need(condition, message):
    if not condition:
        raise ValueError(message)


def azure(*arguments):
    result = subprocess.run(["az", *arguments, "--only-show-errors", "--output", "json"],
                            capture_output=True, text=True, timeout=120)
    need(result.returncode == 0, "Azure read failed; inspect the selected identity and network privately")
    need(len(result.stdout) < 16 * 1024 * 1024, "Azure response exceeds the size limit")
    return json.loads(result.stdout)


def validate_gateway(gateway, resource_id, endpoint_ip, public_ip_reader):
    need(gateway.get("id", "").lower() == resource_id.lower(), "Gateway resource identity mismatch")
    need(gateway.get("provisioningState") == "Succeeded", "Gateway is not successfully provisioned")
    addresses = []
    for frontend in gateway.get("frontendIPConfigurations", []):
        if frontend.get("privateIPAddress"):
            addresses.append(frontend["privateIPAddress"])
        public = (frontend.get("publicIPAddress") or {}).get("id")
        if public:
            observed = public_ip_reader(public)
            need(observed.get("id", "").lower() == public.lower(), "Public IP resource identity mismatch")
            if observed.get("ipAddress"):
                addresses.append(observed["ipAddress"])
    need(str(ipaddress.ip_address(endpoint_ip)) in addresses,
         "Requested endpoint IP is not a frontend of the selected gateway")


def validate_health(health, pools, *, gateway_id):
    rows = []
    for name in pools:
        matched = [entry for entry in health.get("backendAddressPools", [])
                   if (entry.get("backendAddressPool") or {}).get("id", "").rsplit("/", 1)[-1] == name]
        need(len(matched) == 1, "Expected exactly one selected backend pool: " + name)
        pool_id = (matched[0].get("backendAddressPool") or {}).get("id", "")
        need(pool_id.lower() == (gateway_id + "/backendAddressPools/" + name).lower(),
             "Backend health pool does not belong to the selected gateway")
        settings = matched[0].get("backendHttpSettingsCollection", [])
        need(settings, "Selected pool has no backend settings health")
        seen = set()
        for setting in settings:
            setting_id = (setting.get("backendHttpSettings") or {}).get("id", "")
            prefix = gateway_id + "/backendHttpSettingsCollection/"
            need(setting_id.lower().startswith(prefix.lower()) and setting_id[len(prefix):]
                 and "/" not in setting_id[len(prefix):] and setting_id.lower() not in seen,
                 "Backend health settings must be distinct children of the selected gateway")
            seen.add(setting_id.lower())
            servers = setting.get("servers", [])
            need(servers and all(server.get("health") == "Healthy" for server in servers),
                 "A selected backend is absent or not Healthy: " + name)
            rows.append({"pool": name,
                         "setting": (setting.get("backendHttpSettings") or {}).get("id", "").rsplit("/", 1)[-1],
                         "healthy_servers": len(servers),
                         "addresses": [server.get("address") for server in servers]})
    return rows


def request(endpoint_ip, hostname, path, context):
    # Connect to the observed gateway IP while verifying the real DNS name in TLS
    # and sending that same name in HTTP. No proxy, redirect or TLS bypass.
    need(context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED,
         "HTTPS qualification requires certificate and hostname verification")
    with socket.create_connection((endpoint_ip, 443), timeout=10) as connection:
        with context.wrap_socket(connection, server_hostname=hostname) as transport:
            transport.settimeout(10)
            transport.sendall(("GET " + path + " HTTP/1.1\r\nHost: " + hostname
                               + "\r\nAccept: application/json\r\nConnection: close\r\n\r\n").encode("ascii"))
            response = http.client.HTTPResponse(transport)
            response.begin()
            need(response.status == 200, "Expected HTTP 200 from " + hostname + path)
            payload = response.read(16385)
            need(len(payload) <= 16384, "Application response exceeds 16 KiB")
            return json.loads(payload)


def validate_release(payload, slot, revision):
    need(isinstance(payload, dict), "Application response must be a JSON object")
    need(payload.get("application") == "aks-platform-demo" and payload.get("slot") == slot
         and payload.get("revision") == revision,
         "Application, slot or source revision does not match the selected release")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-id", required=True)
    parser.add_argument("--endpoint-ip", required=True)
    parser.add_argument("--web-host", required=True)
    parser.add_argument("--api-host", required=True)
    parser.add_argument("--backend-pool", required=True, action="append")
    parser.add_argument("--expected-slot", required=True, choices=("aks01", "aks02"))
    parser.add_argument("--revision", required=True)
    parser.add_argument("--step", required=True, choices=STEPS)
    parser.add_argument("--ca-file", type=Path)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    match = RESOURCE.fullmatch(args.gateway_id)
    need(match, "Provide the complete selected Application Gateway resource ID")
    endpoint = str(ipaddress.ip_address(args.endpoint_ip))
    need(not ipaddress.ip_address(endpoint).is_loopback, "A loopback endpoint is not an Azure gateway")
    need(re.fullmatch(r"[0-9a-f]{40}", args.revision), "Provide the full deployed source commit")
    need(1 <= args.samples <= 30, "Use 1-30 independent HTTPS samples")
    need(all(HOST.fullmatch(host) and ".." not in host for host in (args.web_host, args.api_host)),
         "Provide plain lowercase DNS hostnames")
    need(len(args.backend_pool) == len(set(args.backend_pool)) and
         all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", pool) for pool in args.backend_pool),
         "Provide distinct backend pool names")
    need(not args.report.exists(), "Use a new report path; prior evidence is immutable")
    context = ssl.create_default_context(cafile=str(args.ca_file) if args.ca_file else None)
    subscription = match.group(1)
    report = {"schema_version": 1, "kind": "azure-gateway-traffic-qualification",
              "result": "running", "step": args.step, "gateway_id": args.gateway_id,
              "endpoint_ip": endpoint, "web_host": args.web_host, "api_host": args.api_host,
              "expected_slot": args.expected_slot, "source_commit": args.revision,
              "trust": {"source": "explicit-ca-file" if args.ca_file else "system",
                        "ca_sha256": hashlib.sha256(args.ca_file.read_bytes()).hexdigest() if args.ca_file else None},
              "started_at": datetime.now(timezone.utc).isoformat(), "samples": [],
              "limits": ["Independent new connections; not a zero-downtime or existing-session test",
                         "Does not establish CSI identity, WAF attack protection or data recovery"]}
    try:
        gateway = azure("network", "application-gateway", "show", "--ids", args.gateway_id,
                        "--subscription", subscription)
        validate_gateway(gateway, args.gateway_id, endpoint, lambda resource_id:
                         azure("network", "public-ip", "show", "--ids", resource_id,
                               "--subscription", subscription))
        need(isinstance(gateway.get("etag"), str) and gateway["etag"],
             "Gateway observation must include its configuration ETag")
        report["gateway_etag"] = gateway["etag"]
        report["backend_health"] = validate_health(
            azure("network", "application-gateway", "show-backend-health",
                  "--ids", args.gateway_id, "--subscription", subscription), args.backend_pool,
            gateway_id=args.gateway_id)
        for index in range(args.samples):
            rows = []
            for host, path in ((args.web_host, "/version"), (args.api_host, "/api/version")):
                payload = request(endpoint, host, path, context)
                validate_release(payload, args.expected_slot, args.revision)
                rows.append({"hostname": host, "path": path, "slot": payload["slot"],
                             "revision": payload["revision"], "tls_verified": True})
            report["samples"].append({"at": datetime.now(timezone.utc).isoformat(), "responses": rows})
            if index + 1 < args.samples:
                time.sleep(1)
        final_gateway = azure("network", "application-gateway", "show", "--ids", args.gateway_id,
                              "--subscription", subscription)
        need(final_gateway.get("id", "").lower() == args.gateway_id.lower()
             and final_gateway.get("provisioningState") == "Succeeded"
             and final_gateway.get("etag") == report["gateway_etag"],
             "Gateway configuration changed during qualification; rerun against a stable target")
        report["gateway_configuration_unchanged"] = True
        report["result"] = "passed"
    except (OSError, ValueError, subprocess.SubprocessError, http.client.HTTPException) as error:
        report["result"] = "failed"
        report["error_type"] = type(error).__name__
        raise
    finally:
        if report["result"] == "running":
            report["result"] = "failed"
            report["error_type"] = "IncompleteQualification"
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with open(args.report, "x", opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError, http.client.HTTPException) as error:
        raise SystemExit("Traffic qualification failed: " + str(error))
