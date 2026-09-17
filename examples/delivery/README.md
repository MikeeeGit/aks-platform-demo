# Private delivery pipeline examples

These files are intentionally outside active workflow locations. Copy them into a private application consumer after [infrastructure and identity setup](../../docs/SETUP.md). Public CI remains credential-free.

| Operation | GitHub caller | Azure DevOps caller |
| --- | --- | --- |
| Platform-owned namespace and deployment-access bootstrap | [github-bootstrap.yml](github-bootstrap.yml) | [azure-bootstrap.yml](azure-bootstrap.yml) |
| Build once without deploying | [github-build.yml](github-build.yml) | [azure-build.yml](azure-build.yml) |
| Build once and deploy the current release to selected slots | [github-build-deploy.yml](github-build-deploy.yml) | [azure-build-deploy.yml](azure-build-deploy.yml) |
| Promote a selected successful build run without rebuilding | [github-promote.yml](github-promote.yml) | [azure-promote.yml](azure-promote.yml) |

## Configure the private consumer

The examples pin the reviewed shared implementation. When upgrading GitHub, update both `uses@` and `template-ref` to the same reviewed full commit. In Azure DevOps, keep repository alias `aksTemplates` and replace the synthetic project name with the actual template project.

Copy the GitHub files to `.github/workflows/bootstrap.yml`, `image-build.yml`, `build-deploy.yml`, and `promote.yml` respectively. The selected-run promotion verifies the expected producer filename, so keep the filename and its selected `build-workflow` input consistent. Create Azure pipelines pointing at the corresponding private YAML files; record the actual build definition ID.

Configure three separate federated identities: platform bootstrap, registry build, and ordinary app deployment. GitHub uses repository variables `AZURE_AKS_BOOTSTRAP_CLIENT_ID`, `AZURE_ACR_BUILD_CLIENT_ID`, and `AZURE_AKS_DEPLOY_CLIENT_ID`. Azure DevOps uses the three correspondingly named example service connections. Bootstrap also needs the deployment principal's **object ID** in [bootstrap.gateway.apps.json](../../bootstrap.gateway.apps.json).

Create protected approval environments `image-build`, `pprd-uks-aks01`, `pprd-uks-aks02`, `bootstrap-pprd-uks-aks01`, and `bootstrap-pprd-uks-aks02`. Configure required reviewers, main-branch restrictions, concurrency controls, and private-runner access outside YAML. Authorize only the intended pipeline to each resource.

## Operate the pipeline

0. Prepare the separate [Envoy platform consumer](https://github.com/MikeeeGit/aks-delivery-templates/tree/main/examples/platform-envoy), install its pinned CRDs/controller/listeners, and complete [workload identity and CSI TLS setup](../../docs/GATEWAY-API.md). This has separate platform approvals and identity from namespace bootstrap.
1. Run platform bootstrap against the selected slot(s) once, with the separately privileged bootstrap identity. It creates the restricted namespace and scoped application access; ordinary deployment never creates Namespace or grants itself permissions.
2. Choose build-only, or build-and-deploy for a new release. Build-and-deploy passes the published digest and full source commit directly into selected-slot deployment.
3. For later promotion, select a successful build run ID and its expected workflow/pipeline definition. The shared template downloads and checks that build's release receipt instead of accepting a guessed tag.
4. Select `aks01`, `aks02`, or both in the intended order. Sequential deployment is the default; the second slot is reached only after the first succeeds. Parallel deployment is an explicit option, useful when both slots can safely change together. During an upgrade, usually select the inactive slot alone.
5. Complete [private frontend and gateway verification](../../docs/DELIVERY.md), then make a separately reviewed traffic change if needed.

For Azure DevOps selected-run promotion, use `image-release-BuildApplication` for the build-only caller and `image-release-Application_Build` for the combined caller. A renamed shared build stage changes its artifact name; keep the expected artifact input explicit.

Every chosen slot consumes the same source commit and image digest. The callers select delivery.gateway.apps.json and bootstrap.gateway.apps.json. Target verification checks the application Service plus the selected Gateway's current status and actual verified HTTPS route. It still does not prove Azure ILB assignment or the WAF path. A failed check fails deployment; rollback uses a previously approved release deliberately. No application workflow changes Application Gateway DNS or active traffic.
