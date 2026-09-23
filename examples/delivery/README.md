# Private delivery pipeline examples

These files are intentionally outside active workflow locations. Copy them into a private application consumer after [infrastructure and identity setup](../../docs/SETUP.md). Public CI remains credential-free.

| Operation | GitHub caller | Azure DevOps caller |
| --- | --- | --- |
| Platform-owned namespace and deployment-access bootstrap | [github-bootstrap.yml](github-bootstrap.yml) | [azure-bootstrap.yml](azure-bootstrap.yml) |
| Build once without deploying | [github-build.yml](github-build.yml) | [azure-build.yml](azure-build.yml) |
| Build once and deploy the current release to selected clusters | [github-build-deploy.yml](github-build-deploy.yml) | [azure-build-deploy.yml](azure-build-deploy.yml) |
| Promote a selected successful build run without rebuilding | [github-promote.yml](github-promote.yml) | [azure-promote.yml](azure-promote.yml) |
| Propose a selected build as an Argo CD GitOps PR | [github-gitops-propose.yml](github-gitops-propose.yml) | [azure-gitops-propose.yml](azure-gitops-propose.yml) |

The Argo proposal callers are additional choices. They never deploy directly, merge their own PR, or change traffic. Follow [Argo setup and operation](../../docs/ARGO-CD.md); use the build-only caller and pause direct application deployments before Argo owns the workload. Create the separate `gitops-proposals` approval environment and scoped repository publisher access. Argo installation and repository read credentials are platform responsibilities.

## Configure the private consumer

The examples pin the reviewed shared implementation. When upgrading GitHub, update both `uses@` and `template-ref` to the same reviewed full commit. In Azure DevOps, keep repository alias `aksTemplates` and replace the synthetic project name with the actual template project.

Copy the GitHub files to `.github/workflows/bootstrap.yml`, `image-build.yml`, `build-deploy.yml`, and `promote.yml` respectively. The selected-run promotion verifies the expected producer filename, so keep the filename and its selected `build-workflow` input consistent. Create Azure pipelines pointing at the corresponding private YAML files; record the actual build definition ID.

Configure separate federated identities for namespace/access bootstrap, platform services, registry build, and ordinary app deployment. These app callers use bootstrap, build and deployment identities; the separately copied platform consumer uses its own privileged identity. GitHub uses repository variables `AZURE_AKS_BOOTSTRAP_CLIENT_ID`, `AZURE_ACR_BUILD_CLIENT_ID`, and `AZURE_AKS_DEPLOY_CLIENT_ID`. Azure DevOps uses the three correspondingly named example service connections. Bootstrap also needs the deployment principal's **object ID** in [bootstrap.gateway.apps.json](../../bootstrap.gateway.apps.json).

Create protected approval environments `image-build`, `pprd-uks-aks01`, `pprd-uks-aks02`, `bootstrap-pprd-uks-aks01`, and `bootstrap-pprd-uks-aks02`. Configure required reviewers, main-branch restrictions, concurrency controls, and private-runner access outside YAML. Authorize only the intended pipeline to each resource.

## Operate the pipeline

1. Run namespace/access bootstrap against the selected cluster(s), with the separately privileged bootstrap identity. It creates the restricted namespace and scoped application access; ordinary deployment never creates Namespace or grants itself permissions.
2. Prepare the separate [Envoy platform consumer](https://github.com/MikeeeGit/aks-delivery-templates/tree/main/examples/platform-envoy), install its pinned CRDs/controller/listeners, and complete [workload identity and CSI TLS setup](../../docs/GATEWAY-API.md). This has separate platform approvals and identity from namespace bootstrap. The TLS Secret is synchronized when the application pod mounts it, so listener readiness is checked after application deployment.
3. Choose build-only, or build-and-deploy for a new release. Build-and-deploy passes the published digest and full source commit directly into selected-cluster deployment.
4. For later promotion, select a successful build run ID and its expected workflow/pipeline definition. The shared template downloads and checks that build's release receipt instead of accepting a guessed tag.
5. Select `aks01`, `aks02`, or both in the intended order. Sequential deployment is the default; the second cluster is reached only after the first succeeds. Parallel deployment is an explicit option, useful when both clusters can safely change together. During an upgrade, usually select the inactive cluster alone.
6. Complete [private frontend and gateway verification](../../docs/DELIVERY.md), then make a separately reviewed traffic change if needed.

For Azure DevOps selected-run promotion, use `image-release-BuildApplication` for the build-only caller and `image-release-Application_Build` for the combined caller. A renamed shared build stage changes its artifact name; keep the expected artifact input explicit.

Every chosen cluster consumes the same source commit and image digest. The callers select delivery.gateway.apps.json and bootstrap.gateway.apps.json. Target verification checks the application Service plus the selected Gateway's current status and actual verified HTTPS route. It still does not prove Azure ILB assignment or the WAF path. A failed check fails deployment; rollback uses a previously approved release deliberately. No application workflow changes Application Gateway DNS or active traffic.

## Full Azure workload identity example

The additive [Azure workload identity guide](../../docs/AZURE-WORKLOAD.md) provides complete `github-azure-workload-build-deploy.yml` and `azure-azure-workload-build-deploy.yml` callers. They use `delivery.azure-workload.apps.json`, retain the Gateway API/TLS deployment and qualify a real application Key Vault CSI mount on every selected cluster after deployment. Companion `*-azure-workload-promote.yml` callers reuse existing immutable-build promotion. The build approval name is `pprd-image-build`; match it to the Terraform-managed CI federation and actual protected environment.

## Full Azure Argo CD alternative

Use the Azure workload GitOps proposal and Argo lifecycle callers together. The [ordered guide](../../docs/AZURE-ARGOCD.md) covers both CI hosts, target-cluster selection, coded sync access, CSI checks, traffic switching and retirement. Direct callers remain available.
