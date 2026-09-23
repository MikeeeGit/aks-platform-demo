# Azure dual-AKS deployment: direct pipelines or Argo CD

Use the [Azure run list](https://github.com/MikeeeGit/terraform-delivery-templates/blob/main/docs/azure/quick-runbook.md)
for infrastructure and the common platform. This page supplies the application
GitOps route on that same Azure infrastructure.

**aks01 and aks02 are independent AKS clusters.** Either can serve active
traffic. Pipeline inputs are **targetCluster / targetClusters** (Azure DevOps)
and **target-cluster / target-clusters** (GitHub). The serialized configuration
and receipt field named **slot** still identifies the cluster; preserving it
keeps existing release records readable.

## Choose the application owner

| Tier | Direct pipeline delivery | Argo CD delivery |
| --- | --- | --- |
| 1: infrastructure | Terraform creates both AKS clusters, MI/FIC/RBAC, ACR, Key Vault, network, Firewall and WAF prerequisites. | The same Terraform and identities. Argo does not provision Azure resources. |
| 2: platform | Private platform pipelines install Gateway API/Envoy and establish native application access. | The same platform, followed by namespace-scoped Argo on each AKS cluster and coded application-sync permissions. |
| 3: application | A protected promotion pipeline applies the approved rendered application bundle. | A proposal pipeline commits that bundle through a reviewed Git PR. Argo applies it after an approved sync operation. |

Both methods retain Azure workload identity, CSI-mounted application secrets,
TLS/HTTPRoute, immutable images and the separate WAF traffic switch. Kustomize
renders source configuration in both methods. Argo consumes the committed plain
manifest; it does not run a second Kustomize build.

Select one owner per application/cluster. Keep both examples, but disable direct
application deployment when using Argo for those resources. This profile uses
**manual, approval-controlled Argo sync**, with no automatic pruning. Argo performs
all application reconciliation. Git merge alone does not deploy: the sync pipeline
requests an operation and verifies it.

## First-time private configuration

1. Complete infrastructure and common platform actions 1–9 in the Azure run list.
   Keep the separate platform/application managed identities and their
   Terraform-defined Cluster User grants. Keep local AKS accounts disabled.
2. Commit actual delivery.azure-workload.apps.json, azure.workload.json,
   bootstrap.native.apps.json and both Azure workload overlays in the private
   application consumer. The native bootstrap file must contain the application
   usernames observed on **each** real cluster, not guessed object IDs.
3. Copy [gitops.config.example.json](../gitops.config.example.json) to
   gitops.config.json. Set the exact private HTTPS Git clone URL, protected main
   branch and both real HTTPS AKS API addresses. Use **argocd** and
   **platform-demo** as the namespaces with these pipeline callers.
4. Pin the same reviewed shared-template commit in all callers and
   worked-example.json. Commit before building. The lifecycle helper rejects a
   dirty checkout, a changed source commit or an unpinned shared checkout.
5. Allow the existing private worker to reach both private APIs. Permit
   repo-server HTTPS Git access through namespace policy and Azure Firewall.
   Nodes must pull the pinned Argo images; bootstrap workers must fetch the
   hash-pinned installation. Follow the destinations in the
   [Argo platform guide](https://github.com/MikeeeGit/aks-delivery-templates/blob/main/examples/argocd-platform/README.md#network-and-private-access).
6. Use the non-HA **evaluation** profile for the small lab. HA needs separately
   reviewed capacity and availability testing.
7. Configure private Git **read-only** credentials as protected secret variables
   **ARGO_GIT_USERNAME** and **ARGO_GIT_READ_TOKEN** for install jobs/environments.
   Scope the credential to this private repository; use a short-lived approved
   evaluation credential and revoke it during removal. Credentials go through
   process environment and Kubernetes stdin, never command arguments, committed
   YAML or artifacts. An existing matching credential is preserved; the installer
   is not a rotation mechanism.
8. Register the private callers below. The Argo helper does not create, edit or
   remove any Azure DevOps service connection.

| Purpose | Azure DevOps caller | GitHub caller |
| --- | --- | --- |
| Build/test/push/scan once | [azure-azure-workload-build.yml](../examples/delivery/azure-azure-workload-build.yml) | [github-azure-workload-build.yml](../examples/delivery/github-azure-workload-build.yml) |
| Propose the Azure workload release | [azure-azure-workload-gitops-propose.yml](../examples/delivery/azure-azure-workload-gitops-propose.yml) | [github-azure-workload-gitops-propose.yml](../examples/delivery/github-azure-workload-gitops-propose.yml) |
| Install/bootstrap/sync/verify/retire | [azure-azure-workload-argocd.yml](../examples/delivery/azure-azure-workload-argocd.yml) | [github-azure-workload-argocd.yml](../examples/delivery/github-azure-workload-argocd.yml) |
| Direct alternative | [azure-azure-workload-promote.yml](../examples/delivery/azure-azure-workload-promote.yml) | [github-azure-workload-promote.yml](../examples/delivery/github-azure-workload-promote.yml) |

For Azure DevOps, replace the synthetic template project and connection names.
Use the **bootstrap-only connection bound to the platform MI** for
install/bootstrap/retire, and the application connection for sync/verify.
Authorize only the appropriate private pipeline. Do not repurpose other apps'
connections or grant project-wide pipeline access.

For GitHub, provide protected environment variables **AZURE_TENANT_ID**,
**AZURE_SUBSCRIPTION_ID**, **AZURE_AKS_BOOTSTRAP_CLIENT_ID** and
**AZURE_AKS_DEPLOY_CLIENT_ID**, with Terraform-managed federation for the actual
protected environments. Worker labels are self-hosted, linux, aks-private.
The workflow creates its own Azure CLI login directory and removes only that directory.

Create protected environments **argocd-ACTION-pprd-uks-CLUSTER**, for actions
install, bootstrap, sync, verify and retire and clusters aks01/aks02.
Restrict approvers and the allowed main branch. Install/bootstrap/retire are
platform operations. Review the install artifact before approving its deployment
job; the job verifies the earlier stage's receipt checksum.

GitHub required-reviewer availability depends on the plan for private repositories.
Verify the [environment protection requirements](https://github.com/MikeeeGit/terraform-delivery-templates/blob/main/docs/azure/github-actions.md)
before enabling writes. If the required gates are unavailable, use the protected
Azure DevOps route; an environment name alone does not enforce approval.

Add the actual new GitHub environments to the existing **platform.github_environments**
and **application.github_environments** maps in the private
[delivery-identities Terraform root](https://github.com/MikeeeGit/terraform-delivery-templates/tree/main/initial-setup/azure/delivery-identities).
Keep all existing entries. Platform gains install/bootstrap/retire for each target
cluster; application gains sync/verify for each target cluster. Use the actual
repository OIDC segment and apply the reviewed identity plan. For Azure DevOps,
add only the new lifecycle definition ID to the relevant **lab** connection's
pipeline authorization list in the separate
[connection Terraform root](https://github.com/MikeeeGit/terraform-delivery-templates/tree/main/initial-setup/azure/azure-devops-connections).
Do not update unrelated connection states.

Proposal jobs retain separate Git PR publishing permissions and have no AKS
credentials. For GitHub proposal writes, configure the scoped **GITOPS_PR_TOKEN**
described in the shared Argo deployment guide.

## Initial deployment: actions to run

Run from protected main. **gitopsCommit / gitops-commit** is its reviewed
**full current SHA**, distinct from the application's image source SHA.

| Order | Pipeline/action | Target and acceptance |
| --- | --- | --- |
| 1 | Argo lifecycle: **install** | aks01, then aks02. Review the platform artifact and approve. The helper checks subscription/tenant, private AKS API identity, OIDC/CSI prerequisites and TLS context before mutation. |
| 2 | **Image build** | Build/test/push/scan once. Retain successful build ID, trusted producer and artifact name. |
| 3 | **Azure workload GitOps proposal** | aks01, then aks02, selecting the same successful build. Review/merge each PR; preserve the other cluster's release folder. |
| 4 | **Committed release validation** | Required PR CI checks release hashes and target binding. Fetch the final merged main commit containing both releases. |
| 5 | Argo lifecycle: **bootstrap** | aks01, then aks02. Supply each approved release.json SHA256. Creates restricted AppProject, Application and native sync Role/RoleBinding using the observed application identity. It does not apply app workloads. |
| 6 | Argo lifecycle: **sync** | aks01, then aks02. Supply the reviewed GitOps commit and each release checksum. Argo applies the app; verification requires exact revision, Synced/Healthy state, approved digest, HTTPS and **real Azure workload identity/Key Vault CSI readiness**. |
| 7 | **Application Gateway** and traffic qualification | Apply reviewed WAF configuration; verify real stable/preview HTTPS endpoints, backend trust/health and cluster/revision. In-cluster probes do not establish this cloud path. |

The lifecycle caller operates on one explicit cluster per run. Complete and
inspect aks01 before starting aks02 initially. For updates, select standby first.
Calculate each receipt checksum from the reviewed committed release:

~~~bash
git show FULL_GITOPS_COMMIT:gitops/releases/pprd/uks/aks02/release.json | sha256sum
~~~

Keep approved commit, digest, source revision, checksum, pipeline run and private
report together. The helper reads workload contracts from the **application
source commit recorded in the release**; a later GitOps merge cannot silently
substitute another identity or vault binding. Reports stay outside the Git checkout.

## Standby update, switch and rollback

1. Build the next immutable image once.
2. Propose only the standby cluster's GitOps folder, review and merge.
3. Run **sync** for that cluster with the new GitOps commit and release checksum.
   Require Argo, HTTPS and live CSI checks to pass.
4. Verify preview WAF serves the new revision and stable traffic still serves the
   unchanged active cluster.
5. Run the reviewed Gateway Terraform traffic change. Qualify stable WAF web/API
   responses from the newly selected cluster and revision.
6. For **traffic rollback**, restore the previous Gateway backend/DNS target
   through Terraform and verify the original active release.
7. For **application rollback**, make a new reviewed Git commit restoring the
   prior release files, then **sync** that new commit. Do not use direct apply or
   rollout-undo behind Argo.

**verify** repeats Argo/HTTPS/CSI checks without requesting a new sync.
Keep the desired-state branch stable during verification; a new head requires
a newly reviewed run.

## Safe removal

1. Stop build/proposal/direct-deployment jobs and prevent new sync approvals.
   Wait for or explicitly terminate in-progress Argo operations. Keep the
   platform retirement approval available.
2. Run **retire** on aks01 and aks02 with each deployed reviewed release and
   checksum. It checks Application source/project/destination, rejects cascading
   finalizers and running operations, then deletes only that Application with
   UID/resource-version preconditions and orphan propagation. Workloads remain
   for controlled removal; other Applications and service connections are untouched.
3. Add new private proposal/lifecycle pipeline IDs to the lab freeze configuration.
   Run the full [scripted teardown](https://github.com/MikeeeGit/terraform-delivery-templates/blob/main/docs/azure/three-tier-removal.md#scripted-removal-run-list).
   It withdraws cloud traffic, removes app/Gateway Services, verifies frontend
   release, then removes dependent infrastructure and state storage. Its workload
   guard refuses to proceed while an Argo Application targets the app namespace.
4. Keep Argo controllers/namespaces/CRDs until the disposable AKS clusters are
   destroyed. No namespace-wide deletion is needed. Revoke the **lab-only Git
   credential** through its provider; never revoke a shared credential.
   Keep the existing exact lab service-connection ownership checks.
5. Confirm exact owned resource groups are absent and component/backend states
   are empty. Preserve private evidence; queued deletion is not completion.

For migration from a running direct deployment, stop its direct writer, preserve
the release and render the same digest/configuration. Review adoption on standby
before enabling Argo. Reversing ownership requires retiring the Application before
resuming direct delivery. Never enable both writers for the same resources.

## Evidence and limitations

The [21 September Azure record](https://github.com/MikeeeGit/terraform-delivery-templates/blob/main/docs/azure/qualification-2026-09-21.md)
qualifies **direct** Azure delivery and full removal. This added Azure Argo
adapter needs its own private live qualification before claiming a cloud pass.

On **23 September 2026**, [GitHub run 35827415210](https://github.com/MikeeeGit/aks-platform-demo/actions/runs/35827415210)
passed both real kind paths: direct in **4m07s** and Argo in **8m41s**, with
empty cleanup-error lists. The Argo report records **eight scoped sync-permission
checks** and **two Application retirements preserving the existing Deployment UID**.
The tested PR merge tree matches published application revision
**75eb0f3405558bb3761faeda4628969d30125f52** and uses shared templates
**9fd0d6bd2e860c3f2e6a6aae4f154c8d3d309179**. These are Kubernetes acceptance
results, not a live Azure Argo result.

Public tests exercise rejection paths, credential preservation, scoped sync
rights and retirement preconditions. The real two-cluster Argo test additionally
installs the generated sync Role/RoleBinding, uses it for sync, checks denied
access, and retires Applications while preserving workloads. Its synthetic
identity proves Kubernetes permissions, not Azure federation.

Record the Azure sequence above, including private Git access, federated pipeline
access, CSI, WAF cutover/rollback and teardown during the cloud trial. HA, SSO,
secret rotation, network-policy enforcement, load and stateful recovery remain
separate qualification work. Dynatrace is excluded.

References: [operations](https://github.com/MikeeeGit/aks-delivery-templates/blob/main/docs/argocd-operations.md),
[troubleshooting](https://github.com/MikeeeGit/aks-delivery-templates/blob/main/docs/argocd-troubleshooting.md),
[private Git authentication](https://argo-cd.readthedocs.io/en/stable/user-guide/private-repositories/),
[Application deletion](https://argo-cd.readthedocs.io/en/stable/user-guide/app_deletion/).
