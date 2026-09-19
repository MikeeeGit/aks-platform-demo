#!/usr/bin/env python3
"""Skip expensive GitHub CI only for a known Markdown-only commit range."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess


def needs_tests(source, event_name, event):
    if event_name == "pull_request":
        base = event.get("pull_request", {}).get("base", {}).get("sha", "")
    elif event_name == "push":
        base = event.get("before", "")
    else:
        return True
    if not re.fullmatch(r"[0-9a-f]{40}", base) or base == "0" * 40:
        return True
    try:
        subprocess.run(["git", "-C", str(source), "merge-base", "--is-ancestor", base, "HEAD"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        paths = subprocess.check_output(
            ["git", "-C", str(source), "diff", "--no-renames", "--name-only", "-z", base, "HEAD"]
        ).split(b"\0")[:-1]
    except (OSError, subprocess.CalledProcessError):
        return True
    # No diff, incomplete history, manual runs and unknown events keep full CI.
    return not paths or any(not path.lower().endswith(b".md") for path in paths)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        run = needs_tests(args.source, os.environ.get("GITHUB_EVENT_NAME", ""), event)
    except (KeyError, OSError, ValueError, TypeError, AttributeError):
        run = True
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"run-tests={str(run).lower()}\n")
    print("Full validation required." if run else "Markdown-only change: expensive validation skipped.")


if __name__ == "__main__":
    main()
