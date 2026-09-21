# First-time setup

The additional [Argo CD deployment guide](ARGO-CD.md) reuses this same infrastructure, Gateway API profile and application configuration. Choose either direct application deployment or Argo ownership for each slot.

## Local tools and checks

Use Node.js 24, Python 3.10 or newer, and the kubectl version pinned by the chosen `aks-delivery-templates` release. Docker with Buildx is needed to build an image. Azure CLI and kubelogin are needed only for authenticated build/deployment. Review and pin the shared template release before use.

From the app root:

```bash
npm ci --ignore-scripts --no-audit --no-fund
npm run verify
python3 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements-dev.txt
.venv/bin/python tests/test_manifests.py
```

Put kubectl in PATH, or set `KUBECTL_BIN` to its absolute path for the manifest tests. Windows uses `.venv\Scripts\python.exe` and `.venv\Scripts\pip.exe`. These checks need no Azure login or Kubernetes context.

A local Docker check, when a Docker daemon is available:

```bash
REVISION="$(git rev-parse HEAD)"
docker build --build-arg BUILD_REVISION="$REVISION" -t aks-platform-demo:local .
docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges   -e APP_SLOT=aks01 -p 8080:8080 aks-platform-demo:local
```

From a second shell:

```bash
node scripts/smoke.mjs --url http://127.0.0.1:8080 --host web.example.test   --expected-slot aks01 --expected-revision "$(git rev-parse HEAD)"
```

The Dockerfile pins the official Node image by digest, runs as UID/GID 10001, and copies only built application files. Update that digest deliberately and rerun image checks. Do not pass secrets through build arguments.

## Infrastructure handoff

Provision the companion network and dual-AKS scenario before deploying this app. The synthetic contract is:

| Item | Value |
| --- | --- |
| Hub registry | `exampleplatformacr.azurecr.io/aks-platform-demo` |
| Registry resource group | `uks-hub-netw-rg-01` |
| AKS resource group | `uks-pprd-example-aks-rg` |
| Blue cluster/subnet | `uks-pprd-example-aks01` / `uks-pprd-aks01` |
| Green cluster/subnet | `uks-pprd-example-aks02` / `uks-pprd-aks02` |
| Blue Envoy candidate IP | `10.81.0.21` |
| Green Envoy candidate IP | `10.81.4.21` |
| Gateway source subnet | `10.81.8.0/24` |
| Namespace / Deployment / Service | `platform-demo` |

Replace every placeholder in [delivery.gateway.apps.json](../delivery.gateway.apps.json). Registry and AKS subscriptions may differ. The registry name must be globally unique and its login server must match. Both kubelet identities need image-pull access to the chosen registry. The AKS cluster identity must have the network permissions needed to create the internal LoadBalancer on the selected subnet. DNS, firewall rules, registry reachability, and image-pull access must work from both clusters.

Reserve each candidate IP in its correct subnet. The independently managed Envoy platform profile owns the Azure internal LoadBalancer and its source restrictions. The application owns a ClusterIP Service. Read [the Gateway API profile](GATEWAY-API.md), install its pinned platform controller/CRDs/listener, and configure the workload identity and Key Vault certificate before the first application deployment. The older direct-Service profile retains separate .20 frontends; do not give two Services the same address.

## Platform-owned bootstrap

[bootstrap.gateway.apps.json](../bootstrap.gateway.apps.json) supplies separately approved platform bootstrap targets. Replace the synthetic deployment principal **object IDs** (not client/application IDs), and match each Pod Security minor to the actual cluster. The example follows the platform's aks01 v1.35 / aks02 v1.36 upgrade slots. The maintained Gateway API profile requires Key Vault CSI; its bootstrap configuration enables that prerequisite check. The direct-Service alternative does not need CSI.

The [private bootstrap workflow](../examples/delivery/github-bootstrap.yml) uses a separate federated platform identity and bootstrap approval environments. That identity needs existing user-kubeconfig access, Kubernetes permission to create namespaces, and Azure role-assignment permission at the selected cluster scope. Its privileged role is deliberately separate from ordinary application deployment. The helper checks private managed-Entra AKS, disabled local accounts, workload identity, selected tenant/subscription, and Pod Security compatibility before creating the namespace and scoped deployment role assignments. It uses user credentials, never admin kubeconfig. Allow role-assignment propagation before the first application deploy.

A platform operator may instead perform the equivalent manual bootstrap below. The checked-in namespace manifest uses the current cluster policy version; the automated bootstrap pins the explicit minor from the bootstrap context.


For each cluster, a platform operator obtains an explicitly selected user kubeconfig using the real tenant/subscription and cluster name. Review the context and server before running any kubectl command. The app pipeline must never receive admin kubeconfig.

The operator applies [deploy/bootstrap/namespace.yaml](../deploy/bootstrap/namespace.yaml) once to each selected cluster:

```bash
kubectl --context "$EXPECTED_CONTEXT" apply -f deploy/bootstrap/namespace.yaml
kubectl --context "$EXPECTED_CONTEXT" get namespace platform-demo --show-labels
```

Its labels enforce, audit, and warn against restricted Pod Security admission; `latest` tracks the cluster's current policy version. Review policy changes when upgrading clusters, or pin all three version labels to a supported version as part of the platform policy. The manifests use nonroot execution, dropped capabilities, RuntimeDefault seccomp, no privilege escalation, and a read-only root filesystem. See [Kubernetes namespace policy guidance](https://kubernetes.io/docs/tasks/configure-pod-container/enforce-standards-namespace-labels/).

Separately grant the application deployment principal AKS Cluster User access and namespace-scoped AKS RBAC Writer access for `platform-demo` when using Azure RBAC for Kubernetes. The operator creates namespaces and role assignments; the ordinary app deployment contains only namespace-scoped resources. Confirm the default ServiceAccount exists before delivery; the shared helper checks it without needing Namespace read permissions. The pod disables automatic API-token mounting; the workload identity webhook injects its separately scoped projected federation token. Also authorize the required HTTPRoute and SecretProviderClass custom-resource operations explicitly; namespace Writer alone is not proof of those permissions.

## Delivery identities and protected runners

Use separate federated identities for registry push and app deployment. Limit push access to the intended registry/repository and cluster access to the intended cluster/namespace. Match federation subjects to protected pipeline environments, and configure approvals for `image-build`, `pprd-uks-aks01`, and `pprd-uks-aks02`.

Private clusters require a runner with private API connectivity and DNS. Restrict that runner to trusted private workflows on protected main; public pull-request checks use credential-free hosted runners. The [inactive pipeline examples](../examples/delivery/README.md) belong in a private deployment repository after configuration. Do not grant public fork jobs OIDC, service connections, or access to the private runner.


## Custom-resource authorization boundary

The shared [opt-in authorization example](https://github.com/MikeeeGit/aks-delivery-templates/tree/main/examples/authorization) provides an explicit HTTPRoute/SecretProviderClass grant recipe. Its Azure ABAC mechanism is preview and needs operator review. Namespace Writer also has broad rights over ordinary objects in the same namespace, including the platform proxy and TLS Secret; separating release ownership is not an adversarial tenant-security boundary. Use a separately reviewed tighter authorization/namespace design when that isolation is required.

## Qualify the Azure workload identity path

Use the additive [Azure workload identity profile](AZURE-WORKLOAD.md) when deploying the full Azure reference. Its combined callers build, deploy selected slots and verify the actual app identity/Key Vault CSI mount. The same application manifests can be delivered through the documented Argo method.
