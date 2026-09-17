# Deploy this application with Argo CD

This is an additional delivery method. The [direct pipeline method](DELIVERY.md) remains available. Choose one writer for each application's resources on each cluster.

Kustomize prepares Kubernetes YAML; Argo reconciles Git with Kubernetes. They are compatible and neither implies the other. This example keeps the existing Kustomize base/overlays as its single authoring source, renders them in the proposal pipeline, then commits the finished YAML to Git. Argo consumes that plain YAML directly. You do not run Kustomize in the cluster or maintain a second set of application definitions.

## Architecture and repository layout

Infrastructure and platform installation are unchanged. Terraform owns both private AKS slots, networking, identities and the edge WAF. Platform pipelines own namespaces, Envoy Gateway, TLS scaffolding and Argo installation. Each slot has its own Argo instance and an Application restricted to the local application namespace.

The private application repository holds both source and approved desired releases:

```text
deploy/gateway-api/base/                  # shared authored application configuration
deploy/gateway-api/overlays/pprd/uks/     # slot differences
delivery.gateway.apps.json              # private target/registry/verification settings
gitops.config.json                      # private repo URL and reviewed API endpoints
gitops/releases/pprd/uks/aks01/           # generated, reviewed desired release
gitops/releases/pprd/uks/aks02/           # independently promoted desired release
```

The release folder contains the validated `manifest.yaml`, its release receipt, a passed build receipt and optionally a public CA certificate. Argo's explicit `directory.include: manifest.yaml` prevents receipt JSON from being treated as Kubernetes resources.

Read the shared [design](https://github.com/MikeeeGit/aks-delivery-templates/blob/main/docs/argocd-design.md) and [delivery-method comparison](https://github.com/MikeeeGit/aks-delivery-templates/blob/main/docs/delivery-methods.md) before installation.

## First deployment

1. Follow [SETUP](SETUP.md) and the [Gateway API profile](GATEWAY-API.md). Complete the actual Azure output/identity/DNS/certificate configuration in a **private** consumer. Use the [Azure sandbox guide](https://github.com/MikeeeGit/terraform-delivery-templates/blob/main/docs/azure/sandbox-deployment.md) for infrastructure order and stop gates.
2. Copy [gitops.config.example.json](../gitops.config.example.json) to `gitops.config.json`. Replace the clone URL and each `cluster_api_servers` entry with the actual reviewed private AKS API URL. Keep HTTPS and certificate validation. A display name alone is not a cluster identity check.
3. Install the pinned Argo bundle in each selected slot using the shared [deployment guide](https://github.com/MikeeeGit/aks-delivery-templates/blob/main/docs/argocd-deployment.md). It covers explicit kubeconfig/context, credential-free preparation, reviewed apply, private repository credentials and initial admin/SSO responsibilities.
4. Copy the existing [build-only caller](../examples/delivery/README.md) into your private CI. It builds once and publishes a promotable receipt only after the immutable image passes the security gate.
5. Copy either [GitHub proposal](../examples/delivery/github-gitops-propose.yml) or [Azure DevOps proposal](../examples/delivery/azure-gitops-propose.yml). Preserve the reviewed full template pin. Configure `gitops-proposals` approvals and the scoped repository publisher permissions described below.
6. Select a successful build and **one** slot. Review the PR's image digest, source revision, target, HTTPRoute, policy and concrete YAML. Merge through protected-branch checks. The proposal workflow never merges or synchronizes.
7. Use that committed folder to generate/review the Argo Project/Application. Apply those control resources through the platform operator as documented. The first sync is explicit; no application auto-sync, auto-prune or cascading deletion finalizer is configured.
8. Follow the shared exact-commit sync and verification command. It checks that the reviewed folder matches the selected Git commit, binds the kubeconfig to the reviewed API endpoint, checks the Application's repository/path/project/destination, and waits for that operation to finish. Acceptance then checks the image, Service, web/API HTTPS routes, slot and application source revision.
9. Repeat for the other slot after the first is healthy. Both slots consume the same approved image; their runtime slot/configuration differs. Leave the existing active traffic target alone until a separate infrastructure change is approved.

For an existing installation, initially propose the currently deployed release and inspect the Argo diff. Pause direct deployment before Argo takes ownership. The [operations manual](https://github.com/MikeeeGit/aks-delivery-templates/blob/main/docs/argocd-operations.md) includes the complete handover.

## CI permissions and promotion

GitHub proposal publication uses `GITOPS_PR_TOKEN`: a scoped GitHub App installation token or fine-grained token able to create branches and PRs in this one private consumer. The reusable workflow's built-in token reads build metadata/artifacts; it is not used to publish a PR whose required checks would be suppressed. Never place the token in a URL or Git file.

Azure proposal publication uses the pipeline's `System.AccessToken`, with the selected build identity granted repository-scoped branch/contribution/PR permissions. Build selection is constrained to the expected repository and producer definition. Keep project-level job authorization limits enabled and authorize the template repository explicitly.

These are repository permissions. Proposal jobs require no cluster credentials or Azure deployment identity. Argo's Git credential is a separate **read-only** credential installed as a Secret outside Git. Human/automation permission to request Argo sync is separate again.

Three identifiers appear during a release:

| Identifier | Meaning |
| --- | --- |
| Application source commit | Code baked into the image and returned by `/version` |
| Image digest | Exact built and scanned container |
| GitOps commit | Desired configuration revision reconciled by Argo |

Do not compare `/version` with the GitOps merge SHA. Updating one slot can advance the watched branch for both Applications while the other slot's image and application source remain unchanged.

## Routine operation and recovery

The default is manual sync with continuous drift visibility. Auto-sync is an optional future operating-policy change; the supplied operator helper rejects an automated owner. HPA owns the replica count, with Argo's ignore-differences setting preserving it.

Promote the inactive slot first. A failed sync, unhealthy rollout or incorrect HTTPS response stops acceptance. Rollback restores a previous approved release folder in a new reviewed Git commit, then synchronizes and verifies it. Argo application rollback does not roll back data/schema changes or switch Application Gateway traffic.

Use the [operations manual](https://github.com/MikeeeGit/aks-delivery-templates/blob/main/docs/argocd-operations.md) for promotion, rollback, drift, backups, upgrades and ownership transfer; use [troubleshooting](https://github.com/MikeeeGit/aks-delivery-templates/blob/main/docs/argocd-troubleshooting.md) for Git, RBAC, sync, TLS, HPA and dual-slot incidents.

## Evidence

The separate [Argo acceptance harness](../scripts/test_argocd_dual_cluster.py) starts two real Kubernetes clusters and real Argo controllers. It exercises initial release, inactive-slot promotion, Git rollback, invalid desired-state rejection and recovery, drift detection/repair, active-slot preservation, HPA, RBAC and actual Envoy HTTPS. See [TESTING](TESTING.md) for exact commands, fixture changes and what a successful report does and does not establish.

Public CI is credential-free. Private proposal API calls are covered by contract tests; your own token permissions and protected-branch approval configuration still require a private-consumer trial. Real AKS networking, Entra/workload identity, CSI/Key Vault, Azure LoadBalancers, WAF and cutover remain Azure qualification work.
