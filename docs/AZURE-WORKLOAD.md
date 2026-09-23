# Azure workload identity application profile

For the complete Azure lab, use the [direct-or-Argo pipeline run list](https://github.com/MikeeeGit/aks-platform-demo/blob/main/docs/AZURE-ARGOCD.md). It includes Azure workload identity/CSI checks, scoped Argo sync permissions and retirement before cloud teardown.

This additive profile deploys the same application through the full Azure pipeline and requires a Key Vault secret mounted through Azure Workload Identity and the Secrets Store CSI driver before the app becomes ready. It retains the Gateway API route, CSI TLS, autoscaling, network policy and immutable two-cluster release model.

Use [delivery.azure-workload.apps.json](../delivery.azure-workload.apps.json) and [deploy/azure-workload](../deploy/azure-workload/base/kustomization.yaml) when qualifying the Azure identity chain. The ordinary [Gateway profile](GATEWAY-API.md), direct-Service example and disposable kind tests remain available. Select one application profile and one delivery owner per cluster.

## The three-tier contract

| Tier | Declared resources and handoff |
| --- | --- |
| Terraform infrastructure | Creates each AKS cluster with OIDC and CSI, application managed identities, exact namespace/ServiceAccount federated credentials for both issuer URLs, and scoped Key Vault roles. CI identities and their deployment permissions are separate from the application's identity. |
| AKS platform | Creates the namespaces and deployment authorization, installs Envoy Gateway and its listeners, and makes the CSI driver available. It consumes the same namespace and identity declarations as application delivery. |
| Application | Builds one immutable image, applies the selected ServiceAccount, TLS and app-secret CSI classes, HTTPRoute and workload, then verifies each selected cluster. Argo CD can reconcile the same rendered application bundle instead of a direct apply. |

The [AKS workload identity declarations](https://github.com/MikeeeGit/azure-aks-foundation/blob/main/variables.tf) and [applied-output handoff](https://github.com/MikeeeGit/terraform-delivery-templates/blob/main/docs/azure/workload-identity-handoff.md) bind the environment identity to the actual AKS issuers. Follow the companion [Azure deployment sequence](https://github.com/MikeeeGit/terraform-delivery-templates/blob/main/docs/azure/sandbox-deployment.md) for networks, registry, clusters, private workers and gateway routing.

## Configure an environment

The committed example covers pprd/uks on aks01 and aks02. Replace the synthetic values in a private consumer using the reviewed Terraform outputs. A different environment needs its own configuration, workload identity, permissions and caller approval names; do not point production at the preproduction vault by copying only the cluster names.

| File | Values owned by the environment |
| --- | --- |
| [delivery.azure-workload.apps.json](../delivery.azure-workload.apps.json) | Tenant, registry, subscription, exact cluster targets, overlays and approval environments |
| [service-account.patch.yaml](../deploy/azure-workload/base/service-account.patch.yaml) | Application managed identity client ID |
| [tls-identity.patch.yaml](../deploy/azure-workload/base/tls-identity.patch.yaml) | Same workload client ID, tenant and the TLS certificate vault |
| [application-secret-provider-class.yaml](../deploy/azure-workload/base/application-secret-provider-class.yaml) | Same client ID and tenant, application vault and secret object name |
| [azure.workload.json](../azure.workload.json) | Expected identity, vault, ServiceAccount and CSI object for each target; consumed independently by the qualifier |

Keep the TLS object name in the inherited Gateway base consistent with the provisioned certificate. The TLS and application vaults may differ; the workload identity must have the required secret-read grant on both. The application secret example uses Key Vault object `platform-demo-qualification`, mounted as `/mnt/app-secrets/qualification`. Provision a nonempty **non-sensitive qualification value** in the private environment's vault before the first deployment. Real application secret creation and rotation belong in the consumer's protected secret-provisioning process; never commit values, pass them through a ConfigMap, or print them in CI.

The app-secret CSI class deliberately does not synchronize a Kubernetes Secret. It mounts the current object version as a file, using `clientID` workload authentication, with VM managed identity authentication disabled. The independently retained TLS class still synchronizes the TLS Secret needed by the Gateway. Microsoft documents the [workload identity CSI binding](https://learn.microsoft.com/en-us/azure/aks/csi-secrets-store-identity-access).

`APP_REQUIRED_SECRET_FILE` enables the app's optional readiness dependency. Missing, unreadable, empty or non-file mounts return HTTP 503 with `dependency_unavailable`; liveness remains healthy. The app reads at most one byte and returns no file path, secret content or digest. Each readiness request reopens the file, so a rotated CSI mount can be observed without a process restart. This only tests availability of a readable nonempty secret, not business-specific content correctness.

## Build, deploy and qualify through either CI host

Before application delivery, copy the [Azure application-bootstrap caller](../examples/delivery/azure-bootstrap.yml) or [GitHub equivalent](../examples/delivery/github-bootstrap.yml) into the private consumer. Set `configFile` / `config` to `delivery.azure-workload.apps.json`, and select the reviewed `bootstrap.native.apps.json` prepared from the actual application identity observations. Select both clusters. The tested Azure DevOps profile uses a **dedicated bootstrap service connection bound to the Terraform-declared platform managed identity**, with bootstrap-only pipeline authorization and separate approval environments. Application deployment retains its separate namespace-scoped identity. A separate bootstrap MI requires its own coded Cluster User grant, identity observation and explicit native platform binding. The [worked Azure procedure](https://github.com/MikeeeGit/terraform-delivery-templates/blob/main/docs/azure/three-tier-worked-example.md#tier-2-bootstrap-access-and-install-platform-services) covers those prerequisites.

The full callers use the same shared build, selected-cluster deployment, receipt and promotion templates as the other profiles:

| Operation | GitHub | Azure DevOps |
| --- | --- | --- |
| Build, deploy and qualify Azure CSI on every selected cluster | [github-azure-workload-build-deploy.yml](../examples/delivery/github-azure-workload-build-deploy.yml) | [azure-azure-workload-build-deploy.yml](../examples/delivery/azure-azure-workload-build-deploy.yml) |
| Promote an existing successful image build and qualify every selected cluster | [github-azure-workload-promote.yml](../examples/delivery/github-azure-workload-promote.yml) | [azure-azure-workload-promote.yml](../examples/delivery/azure-azure-workload-promote.yml) |

Copy the chosen combined GitHub caller to `.github/workflows/build-deploy.yml` in a private consumer, or create an Azure pipeline pointing at its corresponding YAML. The promotion caller's expected build workflow/artifact must match that actual producer. Preserve the pinned shared-template references and update them together after review.

The combined callers use build environment `pprd-image-build` and cluster environments `pprd-uks-aks01` / `pprd-uks-aks02`. Their names must match the coded CI federation subjects and actual protected environments. GitHub needs the build/deploy client ID repository variables; Azure DevOps needs the corresponding federated ARM service connections. Configure private workers with the AKS/private-DNS route and the required tools. Public validation never logs into Azure.

The final qualification job in both build/deploy and promotion callers runs only after the shared deployment succeeds. It uses the **deployment identity** to obtain an isolated Entra user kubeconfig and inspect the app's **workload identity**; it never requests cluster-admin credentials or reads a Key Vault secret value. The workload identity grants Key Vault access to the Pod/CSI authentication path. The deployment identity needs Azure `managedClusters/read`, user-credential retrieval, namespace reads for ServiceAccounts, Deployments, ReplicaSets, Pods, SecretProviderClasses and SecretProviderClassPodStatuses, and Pod port-forward access. Those permissions must come from the coded platform authorization. It needs no Kubernetes Secret read or Pod exec permission for this qualifier.

Both caller types retain a separate private report per cluster. A failed identity check, absent CSI mount, wrong revision, unavailable dependency or changed Deployment makes qualification fail. Selected-build promotion preserves the existing receipt checks and loads the expected identity/configuration from that selected build source before qualification. The app image is not rebuilt. The standalone read-only check remains available for Argo delivery, later diagnosis and rotation checks.

## Argo CD uses the same Azure profile

Use the build-only pipeline and the existing [Argo CD proposal/sync method](ARGO-CD.md), with `config` / `configFile` set to `delivery.azure-workload.apps.json`. Kustomize renders this profile upstream; Argo reconciles the resulting plain YAML. Configure the per-cluster Argo controller's existing application permissions to cover both CSI classes. Keep the platform's bootstrap and the workload identity/Key Vault Terraform unchanged.

After the selected Git revision is Synced/Healthy and its exact application revision is verified, run the same Azure workload qualifier on that cluster. The readiness dependency behaves identically under direct deployment and Argo. A direct application pipeline must not apply competing desired state to an Argo-owned cluster.

## Run the read-only Azure check separately

From the committed private app consumer, log into Azure as its authorized deployment identity. Install Python with the shared pinned requirements and the shared pinned kubectl/kubelogin clients. Then run each intended cluster against the **actual deployed source revision**:

```bash
mkdir -p .delivery
python scripts/qualify_azure_workload.py \
  --environment pprd --region uks --slot aks01 \
  --revision "$DEPLOYED_SOURCE_COMMIT" --acquire-kubeconfig \
  --output .delivery/azure-workload-aks01.json

python scripts/qualify_azure_workload.py \
  --environment pprd --region uks --slot aks02 \
  --revision "$DEPLOYED_SOURCE_COMMIT" --acquire-kubeconfig \
  --output .delivery/azure-workload-aks02.json
```

Alternatively pass `--kubeconfig /private/selected-user-kubeconfig` instead of `--acquire-kubeconfig`. The check binds that context's HTTPS endpoint to the selected Azure resource and requires CA verification. Existing report files are never overwritten; choose new filenames for later runs.

The verifier checks:

1. The exact Azure AKS resource, tenant, kubeconfig endpoint, OIDC/workload-identity settings and CSI add-on.
2. ServiceAccount and CSI client IDs, vault, authentication mode and the exact file-only secret object.
3. A fully rolled-out immutable Deployment, its actual ReplicaSet-owned Pods, projected workload identity, rotating read-only CSI mount and ready state.
4. CSI status owned by each exact Pod UID, a successful mount and the expected versioned Key Vault object.
5. Each selected Pod's secret-dependent readiness and full application source revision/cluster through its own port-forward, followed by a Deployment-generation recheck.

[SecretProviderClassPodStatus](https://secrets-store-csi-driver.sigs.k8s.io/topics/secret-auto-rotation) records the object versions loaded by CSI. The report retains those non-secret version IDs and Pod identities, never values or tokens. It is deployment evidence in a private consumer, not a public artifact.

## Qualify the stable Azure gateway through cutover and rollback

The [read-only traffic qualifier](../scripts/qualify_azure_traffic.py) checks the actual Application Gateway frontend IP, every selected backend pool's health, and the web/API release over certificate-verified HTTPS. It connects directly to that observed IP while keeping the configured DNS hostname in TLS SNI and HTTP Host, so reserved `example.test` hostnames work without public DNS. It also rejects a gateway configuration change during sampling. It neither deploys an application nor changes traffic routing.

Run this from a private worker that can reach the chosen frontend, with an Azure identity allowed to read the gateway, its public IP resource when applicable, and backend health. Obtain the gateway ID/IP and pool names from the applied gateway configuration. For a lab CA, use the public root certificate trusted by the frontend certificate chain; configure the gateway's separate [backend trusted roots](https://github.com/MikeeeGit/azure-application-gateway/tree/main/examples/private-ca) as well. Do not disable certificate verification. System trust is used when `--ca-file` is omitted.

Keep the **same gateway, IP, web/API hostnames and CA** for all four checks. The following example uses the maintained gateway profile's `service` and `preview` pools. Both must be healthy, but the returned release assertions apply to the stable web/API listeners, not the preview listener. Qualify the candidate separately before changing the stable backend target.

```bash
# Set these from the applied private environment and successful build receipts.
# RELEASE_V1_COMMIT and RELEASE_V2_COMMIT must be full 40-character source SHAs.
GATEWAY_ID="<applied-application-gateway-resource-id>"
GATEWAY_IP="<applied-frontend-ip>"
LAB_CA="/private/trust/lab-ca.pem"
EVIDENCE_DIR="/private/evidence/traffic-run-01"
mkdir -p "$EVIDENCE_DIR"
chmod 700 "$EVIDENCE_DIR"

qualify_traffic() {
  python3 scripts/qualify_azure_traffic.py \
    --gateway-id "$GATEWAY_ID" --endpoint-ip "$GATEWAY_IP" \
    --web-host web.example.test --api-host api.example.test \
    --backend-pool service --backend-pool preview \
    --ca-file "$LAB_CA" --samples 3 \
    --step "$1" --expected-slot "$2" --revision "$3" \
    --report "$EVIDENCE_DIR/$1.json"
}

# 1. Release V1 is serving from aks01; both slots and pools are healthy.
qualify_traffic initial-active aks01 "$RELEASE_V1_COMMIT"

# 2. Promote V2 to aks02 and qualify that candidate first. Stable stays on V1.
qualify_traffic standby-updated-active-unchanged aks01 "$RELEASE_V1_COMMIT"

# 3. Apply the reviewed gateway backend-DNS target change to aks02.
# Run this check after observing DNS/backend convergence.
qualify_traffic traffic-cutover aks02 "$RELEASE_V2_COMMIT"

# 4. Apply a newly reviewed plan restoring the stable target to healthy aks01.
qualify_traffic traffic-rollback aks01 "$RELEASE_V1_COMMIT"
```

These are four separate acceptance points in the deployment procedure, not four commands to run consecutively without the intervening changes. Follow the [gateway cutover/rollback procedure](https://github.com/MikeeeGit/azure-application-gateway/blob/main/docs/cutover.md) for the actual Terraform changes. Retain the old cluster and its release throughout the rollback window. Updating the inactive app does not switch the stable endpoint; rolling traffic back does not redeploy the old app.

Each invocation writes a new private report, including failed checks, and refuses to overwrite existing evidence. Use a new evidence directory for a repeat run. The report retains the selected gateway, public CA hash, observed backend addresses and timestamped release samples; keep it with the corresponding infrastructure plan/apply and application deployment records. A failure is not a request to disable verification: diagnose certificate names/trust, frontend selection, backend health or DNS convergence before collecting a new report.

The script's tests include real local TLS requests proving CA, SNI and Host handling, plus rejection and report-handling tests. This public example does not yet include a completed live Azure qualification report. Successful private Azure runs are needed to establish the actual gateway path. The three samples are independent new connections; they do not establish zero downtime, existing-session behavior, WAF attack protection or data recovery. Gateway ETag checks do not prove that a separately managed backend DNS record stayed unchanged. The separate **CSI workload report** proves the selected cluster's mounted-secret identity chain; a gateway traffic report cannot replace it, and the CSI port-forward check cannot replace this traffic check.

## Troubleshooting and proof limits

| Symptom | Check |
| --- | --- |
| Pod stays ContainerCreating / CSI mount missing | Match the AKS issuer, exact `system:serviceaccount:<namespace>:<name>` subject, client ID and Key Vault grant. Confirm the object exists and provider nodes can resolve/reach the vault. |
| Readiness returns dependency_unavailable | Check object alias `qualification`, mount path, nonempty value and file permissions. Avoid printing the mounted file. |
| ServiceAccount or CSI identity mismatch | Regenerate all environment bindings from the same applied Terraform output; check both selected issuer credentials. |
| CSI status is absent or cannot be read | Confirm the CSI mount actually occurred and grant the qualifier read access to the CSI status resource through the platform's coded role. An authorization error does not count as evidence of a missing mount. |
| Kubeconfig endpoint mismatch / private API unavailable | Select the correct cluster and private worker route; do not disable TLS validation. |
| Secret rotation has not appeared | Ensure AKS CSI rotation is enabled, wait for its configured polling interval and rerun into a new report. Check that the reported version changes on every current Pod. Do not use subPath mounts. |

The unit tests exercise missing/empty mounts, local file replacement, non-disclosure and failure handling; Python tests use actual Kustomize rendering and reject mismatched/stale CSI evidence. These are local tests. Kind replaces Azure CSI with local fixtures and cannot establish Entra federation, Key Vault authorization or the Azure CSI path.

A passing **live** report proves the selected app's readable mounted Key Vault object and identity chain at that time, under the trusted cluster/provider configuration. It does not prove future token renewal, rotation, permissions to unrelated Azure resources, WAF routing or disaster recovery. For rotation acceptance, change only the private qualification object's version, wait for CSI rotation, and compare new reports from both clusters. Keep the separate [gateway/cutover checks](DELIVERY.md) and [testing scope](TESTING.md).

For the Argo build-only private caller, set both the delivery config to `delivery.azure-workload.apps.json` and the build approval environment to `pprd-image-build`. The GitHub environment must exactly match the CI identity federation; the generic build-only example retains its separate `image-build` default.
