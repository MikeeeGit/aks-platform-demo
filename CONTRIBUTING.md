# Contributing

Open an issue describing the intended behavior before a large change. Keep this repository synthetic and portable; use reserved example domains and placeholder identifiers.

Run `npm ci --ignore-scripts --no-audit --no-fund` and `npm run verify`. Install the hash-pinned development requirements in a virtual environment, then run `python tests/test_manifests.py` with the shared pinned kubectl in PATH. Include focused tests for changed routing, identity, health, or deployment behavior.

When changing the image base or container runtime, run the Docker build and read-only/nonroot smoke test from [setup](docs/SETUP.md). State clearly when a check could not be performed. Changes to network ranges, namespace permissions, image promotion, or gateway handoff need corresponding documentation and contract tests.

Keep secrets, kubeconfig, build outputs, rendered release bundles, customer data, and real infrastructure configuration out of commits.
