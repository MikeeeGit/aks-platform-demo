# Testing and evidence boundaries

## Fast local checks

```bash
npm ci --ignore-scripts --no-audit --no-fund
npm run verify
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

The manifest tests require kubectl in PATH, or `KUBECTL_BIN` set to its pinned executable. Node tests exercise the actual built HTTP process and shutdown, plus explicit Host/slot/revision smoke checks. Python tests render both Kustomize overlays and verify the harness's digest and cleanup failure behavior. These checks make no Kubernetes API calls.

## Two real Kubernetes clusters

The [acceptance harness](../scripts/test_dual_cluster.py) requires a Linux Docker engine, Git, Node24, Python with the pinned development requirements, OpenSSL, and kind/kubectl/Helm in PATH. [ci-tools.json](../ci-tools.json) pins kind, its node image, registry, Envoy chart/images and Gateway API/Envoy CRD bundles. Install the binary with [the CI installer](../scripts/install_ci_tools.py); use the shared delivery template's pinned kubectl/Helm installer.

Run from a committed app checkout, with a reviewed shared delivery checkout:

```bash
.venv/bin/python scripts/test_dual_cluster.py   --templates ../aks-delivery-templates   --report .delivery/dual-cluster-report.json
```

The harness:

1. Clones the selected app commit into a disposable fixture and creates a local registry plus two uniquely named kind clusters.
2. Builds and pushes a real application image, verifies its registry content digest, and renders both slot bundles using the actual shared delivery helper.
3. Installs real pinned Gateway API/Envoy CRDs and the Envoy controller on both clusters; creates local-only CA/certificate material, then performs server validation, apply and rollout checks for each app bundle.
4. Checks both the selected app Service and actual Envoy HTTPS path, current-generation Gateway/HTTPRoute status, web/API health, slot and full revision. TLS uses the temporary trusted CA, real SNI/Host checks and a negative hostname test.
5. Creates a second source revision and real image in the disposable clone, updates only aks02, and checks its new release.
6. Reapplies the original approved aks02 bundle, verifies rollback, and checks aks01 without changing it.
7. Deletes its own clusters, registry container, and local image tags; writes a JSON report including failures, tool versions, digests, revisions, and completed checks.

The test-only containerd configuration maps the synthetic registry hostname to that run's local registry. The mirror exists only inside its ephemeral kind node containers; the host and real AKS configuration are untouched. The rendered image still uses the real pushed digest. The approach follows kind's [local registry configuration](https://kind.sigs.k8s.io/docs/user/local-registry/), with a local server configured to prevent fallback to the synthetic Azure hostname.

The isolated source fixture replaces Azure CSI mounting with an explicitly local TLS Secret and commits only a public test CA, never private keys. Its Gateway data plane uses ClusterIP transport instead of an Azure ILB. The report records public source and fixture revisions separately. The same real app HTTPRoute and controller process handle TLS requests through port-forwarding. A passing report establishes only local Kubernetes/controller/TLS deployment, update and rollback; it does not establish Azure ILB allocation, AKS identity/RBAC, production CNI NetworkPolicy enforcement, registry/private network access, DNS, Key Vault or Application Gateway/WAF behavior.

An incomplete run or cleanup failure produces a failed report and nonzero status. No Docker daemon is available in the current local authoring environment, so hosted acceptance execution is required before claiming this harness has passed.

## Live platform qualification

Follow [deployment verification](DELIVERY.md): confirm both Service private IPs, image pull, private API access, namespace permissions, selected-slot HTTP responses, gateway backend health/TLS/WAF, and reviewed cutover/rollback. Keep local unit, kind acceptance, hosted pipeline, and live AKS evidence distinct.
