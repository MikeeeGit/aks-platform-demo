# Testing and evidence boundaries

## Fast local checks

```bash
npm ci --ignore-scripts --no-audit --no-fund
npm run verify
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

The manifest tests require kubectl in PATH, or `KUBECTL_BIN` set to its pinned executable. Node tests exercise the actual built HTTP process and shutdown, plus explicit Host/cluster/revision smoke checks. Python tests render both Kustomize overlays and verify the harness's digest and cleanup failure behavior. These checks make no Kubernetes API calls.

## Two real Kubernetes clusters

The [acceptance harness](../scripts/test_dual_cluster.py) requires a Linux Docker engine, Git, Node24, Python with the pinned development requirements, OpenSSL, and kind/kubectl/Helm in PATH. [ci-tools.json](../ci-tools.json) pins kind, its node image, registry, Envoy chart/images and Gateway API/Envoy CRD bundles. Install the binary with [the CI installer](../scripts/install_ci_tools.py); use the shared delivery template's pinned kubectl/Helm installer.

Run from a committed app checkout, with a reviewed shared delivery checkout:

```bash
.venv/bin/python scripts/test_dual_cluster.py   --templates ../aks-delivery-templates   --report .delivery/dual-cluster-report.json
```

The harness:

1. Clones the selected app commit into a disposable fixture and creates a local registry plus two uniquely named kind clusters.
2. Builds and pushes a real application image, verifies its registry content digest, and renders both cluster bundles using the actual shared delivery helper.
3. Installs real pinned Gateway API/Envoy CRDs and the Envoy controller on both clusters; creates local-only CA/certificate material, then performs server validation, apply and rollout checks for each app bundle.
4. Checks both the selected app Service and actual Envoy HTTPS path, current-generation Gateway/HTTPRoute status, web/API health, cluster and full revision. TLS uses the temporary trusted CA, real SNI/Host checks and a negative hostname test.
5. Creates a second source revision and real image in the disposable clone, updates only aks02, and checks its new release.
6. Reapplies the original approved aks02 bundle, verifies rollback, and checks aks01 without changing it.
7. Deletes its own clusters, registry container, and local image tags; writes a JSON report including failures, tool versions, digests, revisions, and completed checks.

The test-only containerd configuration maps the synthetic registry hostname to that run's local registry. The mirror exists only inside its ephemeral kind node containers; the host and real AKS configuration are untouched. The rendered image still uses the real pushed digest. The approach follows kind's [local registry configuration](https://kind.sigs.k8s.io/docs/user/local-registry/), with a local server configured to prevent fallback to the synthetic Azure hostname.

The isolated source fixture replaces Azure CSI mounting with an explicitly local TLS Secret and commits only a public test CA, never private keys. Its Gateway data plane uses ClusterIP transport instead of an Azure ILB. The report records public source and fixture revisions separately. The same real app HTTPRoute and controller process handle TLS requests through port-forwarding. A passing report establishes only local Kubernetes/controller/TLS deployment, update and rollback; it does not establish Azure ILB allocation, AKS identity/RBAC, production CNI NetworkPolicy enforcement, registry/private network access, DNS, Key Vault or Application Gateway/WAF behavior.

An incomplete run or cleanup failure produces a failed report and nonzero status. GitHub Actions and Azure Pipelines run this harness on isolated, Docker-capable hosted workers and retain the JSON report. Only a completed successful report with no cleanup errors counts as acceptance evidence. The retained diagnostics include bounded port-forward output and selected resource/controller status; temporary certificate private keys and kubecredentials are excluded. Positive HTTPS, unmatched HTTP Host and wrong-SNI checks use separate tunnels so an intentional TLS rejection cannot invalidate the next probe.

## Two real Argo CD instances

The additional [Argo harness](../scripts/test_argocd_dual_cluster.py) uses the same Docker-capable prerequisites and pinned shared tooling:

```bash
.venv/bin/python scripts/test_argocd_dual_cluster.py   --templates ../aks-delivery-templates   --report .delivery/argocd-report.json   --artifacts .delivery/argocd-diagnostics
```

It creates two independent clusters and installs real pinned Argo controllers, Envoy and metrics-server. A temporary read-only Git HTTP service is bound only to the run's Docker bridge; production GitOps configuration requires HTTPS. Argo alone applies application workloads from the committed plain manifest. The existing Kustomize renderer prepares those manifests upstream.

The report records initial reconciliation on both clusters, a new real image promoted only to aks02, Git-based rollback, rejection of an invalid Deployment and recovery, manual live drift detected as OutOfSync and repaired from Git, and unchanged active-cluster image/source. Each successful release requires exact operation revision, Synced/Healthy status, rollout, real app responses and verified Envoy HTTPS. HPA metrics/conditions and impersonated controller authorization checks are recorded separately. Positive RBAC checks include application custom-resource permissions; denial checks include Secrets, Roles, Gateways, other namespaces and cluster administration.

The kind fixture uses a local TLS Secret instead of Azure CSI. Its absent CSI custom resource is authorization-tested through an explicit SelfSubjectAccessReview, not claimed as an installed or exercised CSI provider. The metrics-server fixture uses an explicitly test-only insecure kubelet TLS option for kind's node certificate; production settings are unchanged. Kind's default networking does not qualify Azure NetworkPolicy enforcement.

A report passes only if all expected operations and cleanup succeed. CI retains `argocd-dual-cluster-acceptance` separately from `dual-cluster-acceptance`; inspecting one artifact cannot prove the other method. Diagnostics exclude Secrets, credential files and certificate private keys. The test does not use a production registry build receipt and does not claim a production image vulnerability scan.

The shared helper tests cover passed-build receipt selection/binding, exact Git commit and folder checks, wrong-cluster/config refusal, manual sync controls and review-only proposal API contracts. Private publisher tokens, real repository credentials, protected-environment approvals and SSO need an actual private consumer qualification. The HA profile is preparation-tested; the hosted runtime test uses the evaluation profile and does not establish HA failure tolerance.

## Live platform qualification

Follow [deployment verification](DELIVERY.md): confirm both Service private IPs, image pull, private API access, namespace permissions, selected-cluster HTTP responses, gateway backend health/TLS/WAF, and reviewed cutover/rollback. Keep local unit, kind acceptance, hosted pipeline, and live AKS evidence distinct.

## Azure managed identity and app-secret CSI

The [21 September 2026 Azure qualification record](https://github.com/MikeeeGit/terraform-delivery-templates/blob/main/docs/azure/qualification-2026-09-21.md) records successful private Azure DevOps builds/scans, application deployment and live CSI readiness on both AKS clusters, standby-only promotion and WAF traffic switch/rollback. The [run list](https://github.com/MikeeeGit/terraform-delivery-templates/blob/main/docs/azure/quick-runbook.md) links the deployment and removal actions. This cloud record is separate from the kind direct/Argo reports.

The [Azure workload profile](AZURE-WORKLOAD.md) adds an opt-in readiness dependency and a read-only live qualifier. Local tests render both Azure overlays, test missing/empty/replaced files without serving their content, and reject incorrect cluster/identity/CSI status, stale Pod ownership, wrong revisions and unavailable readiness. Those tests do not call Azure. Run the full private build/deploy caller or the documented qualifier against both actual AKS clusters to establish the cloud identity/Key Vault path. Its private report remains separate from kind evidence.


## Runtime image security

The Dockerfile pins the official Node multi-platform image by digest and tests the application in its build stage. The final image excludes npm and Yarn; they are not runtime dependencies of this application. This follows the [official Node image guidance](https://github.com/nodejs/docker-node/blob/main/docs/BestPractices.md#smaller-images-without-npmyarn).

The Azure rehearsal found HIGH vulnerabilities in the earlier base image and bundled package-manager dependencies. Updating the pinned base supplies patched Alpine OpenSSL libraries; removing unused package managers reduces the runtime dependency surface. The protected build must scan the resulting immutable ACR image successfully before it emits a release receipt. A digest pin alone is not a current vulnerability assessment.

## Azure Argo adapter coverage

Adapter tests reject stale source, foreign targets, unobserved identities, unsafe retirement and mismatched Git credentials. Real Argo kind acceptance uses the generated limited sync identity and checks that retirement preserves the Deployment UID. This does not claim Azure federation/CSI qualification; record the separate [Azure Argo run](AZURE-ARGOCD.md) before claiming a cloud pass.
