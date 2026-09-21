#!/usr/bin/env python3
"""Run the pinned, credential-free three-tier worked example and retain its evidence."""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/MikeeeGit/aks-delivery-templates.git"


def run(command, **kwargs):
    return subprocess.run(list(map(str, command)), check=True, **kwargs)



def validate_evidence(evidence, method, source, templates_commit):
    if (evidence.get("result") != "passed" or evidence.get("cleanup_errors") != []
            or evidence.get("public_source_commit") != source):
        raise ValueError("Run, cleanup or source evidence is incomplete: " + method)
    tiers = evidence.get("tiers", [])
    if [row.get("tier") for row in tiers] != [1, 2, 3] or any(
            row.get("result") != "passed" for row in tiers):
        raise ValueError("Missing completed three-tier evidence: " + method)
    checks = evidence.get("traffic_checks", [])
    if [row.get("step") for row in checks] != [
            "initial-active", "standby-updated-active-unchanged", "traffic-cutover", "traffic-rollback"]:
        raise ValueError("Missing stable-endpoint cutover evidence: " + method)
    if len({row.get("endpoint") for row in checks}) != 1 or not re.fullmatch(
            r"127\.0\.0\.1:[1-9][0-9]{0,4}", checks[0].get("endpoint", "")):
        raise ValueError("Traffic checks must use one loopback endpoint")
    revisions = []
    for check, slot in zip(checks, ("aks01", "aks01", "aks02", "aks01")):
        rows = check.get("responses", [])
        if (check.get("expected_slot") != slot or len(rows) != 2
                or any(row.get("slot") != slot or row.get("tls_verified") is not True for row in rows)
                or len({row.get("source_commit") for row in rows}) != 1):
            raise ValueError("Incomplete HTTPS slot/revision evidence")
        revisions.append(rows[0]["source_commit"])
    if revisions[0] != revisions[1] or revisions[0] != revisions[3] or revisions[0] == revisions[2]:
        raise ValueError("Traffic update/rollback did not prove distinct releases")
    execution = evidence.get("tier_execution", [])
    if {row.get("slot") for row in execution if row.get("tier") == "platform"} != {"aks01", "aks02"}:
        raise ValueError("Both slots must execute the shared platform engine")
    if method == "direct" and {
            row.get("slot") for row in execution if row.get("tier") == "application"} != {"aks01", "aks02"}:
        raise ValueError("Both slots must execute the shared application engine")
    if any(row.get("result") != "passed"
           or row.get("template", {}).get("repository_commit") != templates_commit
           or row.get("template", {}).get("working_tree_modified") is not False for row in execution):
        raise ValueError("Shared execution evidence does not match the clean pinned implementation")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--method", choices=("direct", "argocd", "both"), default="direct")
    parser.add_argument("--output", required=True, type=Path, help="New private directory for reports and diagnostics")
    args = parser.parse_args()
    config = json.loads((ROOT / "worked-example.json").read_text())
    if (config.get("schema_version") != 1 or config.get("templates_repository") != REPOSITORY
            or not re.fullmatch("[0-9a-f]{40}", config.get("templates_commit", ""))):
        raise ValueError("Invalid immutable shared template pin")
    for tool in ("git", "docker", "node", "openssl"):
        if not shutil.which(tool):
            raise ValueError("Missing required executable: " + tool)
    engine = run(["docker", "info", "--format", "{{.OSType}}"], capture_output=True, text=True)
    if engine.stdout.strip() != "linux":
        raise ValueError("A Linux Docker engine is required")
    actual_node = run(["node", "--version"], capture_output=True, text=True).stdout.strip().lstrip("v")
    expected_node = (ROOT / ".node-version").read_text().strip().lstrip("v")
    if actual_node.split(".")[:len(expected_node.split("."))] != expected_node.split("."):
        raise ValueError("Use the Node version in .node-version: " + expected_node)
    # The harness snapshots committed source. Never silently test an older commit
    # while a user believes their uncommitted code is being exercised.
    dirty = run(["git", "-C", ROOT, "status", "--porcelain"], capture_output=True, text=True).stdout
    if dirty.strip():
        raise ValueError("Commit or isolate source changes before this reproducible worked run")
    source = run(["git", "-C", ROOT, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    output = args.output.resolve()
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    result = {
        "schema_version": 1, "kind": "three-tier-kind-worked-example", "result": "running",
        "source_commit": source, "templates_commit": config["templates_commit"],
        "started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(), "methods": [],
        "qualification_limits": [
            "Kind provisions local clusters; it does not execute Azure Terraform",
            "No Azure federation, CSI, private DNS, WAF or live Azure qualification",
            "Synthetic Kubernetes impersonation tests permissions, not Entra login",
            "Traffic relay routes new TCP connections; existing streams retain their backend",
        ],
    }
    try:
        with tempfile.TemporaryDirectory(prefix="aks-worked-example-") as temporary:
            work = Path(temporary)
            templates = work / "templates"
            run(["git", "clone", "--quiet", "--filter=blob:none", "--no-checkout", REPOSITORY, templates])
            run(["git", "-C", templates, "fetch", "--quiet", "--depth=1", "origin", config["templates_commit"]])
            run(["git", "-C", templates, "checkout", "--quiet", "--detach", config["templates_commit"]])
            python = work / "python/bin/python"
            run([sys.executable, "-m", "venv", work / "python"])
            run([python, "-m", "pip", "install", "--require-hashes", "-r", templates / "requirements.txt"])
            tools = work / "tools"
            run([sys.executable, templates / "scripts/install_tools.py", "--directory", tools])
            run([sys.executable, ROOT / "scripts/install_ci_tools.py", "--directory", tools])
            env = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"])
            methods = ("direct", "argocd") if args.method == "both" else (args.method,)
            for method in methods:
                name = "test_dual_cluster.py" if method == "direct" else "test_argocd_dual_cluster.py"
                report = output / method / "report.json"
                run([python, ROOT / "scripts" / name, "--templates", templates,
                     "--report", report, "--artifacts", output / method / "diagnostics"], env=env)
                evidence = json.loads(report.read_text())
                validate_evidence(evidence, method, source, config["templates_commit"])
                result["methods"].append({"method": method, "result": "passed",
                                          "report": str(report.relative_to(output))})
        result["result"] = "passed"
        return 0
    except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        result.update(result="failed", error=str(error))
        raise
    finally:
        result["finished_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        (output / "worked-example.json").write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        raise SystemExit("Worked example stopped: " + str(error))
