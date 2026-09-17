#!/usr/bin/env python3
"""Install the checksum-pinned Linux amd64 kind binary for credential-free CI."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import tempfile
import urllib.request


def install(directory: Path) -> Path:
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
        raise ValueError("This CI tool lock targets Linux amd64.")
    lock = json.loads((Path(__file__).resolve().parents[1] / "ci-tools.json").read_text())
    tool = lock["kind"]
    expected_url = f"https://github.com/kubernetes-sigs/kind/releases/download/v{tool['version']}/kind-linux-amd64"
    if tool["url"] != expected_url:
        raise ValueError("Unexpected kind release URL.")
    directory.mkdir(parents=True, exist_ok=True)
    payload = urllib.request.urlopen(expected_url, timeout=120).read()
    if hashlib.sha256(payload).hexdigest() != tool["sha256"]:
        raise ValueError("kind checksum verification failed.")
    target = directory / "kind"
    with tempfile.NamedTemporaryFile(dir=directory, prefix=".kind-", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), 0o755)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path)
    args = parser.parse_args()
    print(install(args.directory))
