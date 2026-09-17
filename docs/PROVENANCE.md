# Source pattern and public changes

This sample is newly authored synthetic application code. It contains no extracted business application, customer data, original configuration values, or original Git history.

## Evidence from the updated archive

The September 2026 archive includes application AKS pipelines and Kubernetes manifests that were absent from the earlier March archive and inspected March/June local copies. The earlier .NET/IIS-only finding applies to those older copies; the updated archive is the basis for the current delivery pattern.

The main application pipeline builds and pushes an image once, optionally builds/deploys database changes, and promotes the current build through explicitly ordered environments. A cluster-list parameter selects the primary slot, secondary slot, or both. Per-environment templates loop over selected clusters, defaulting to sequential deployment, then call shared ConfigMap, optional certificate CSI, and Kustomize templates. Application verification follows deployment.

A separate production pipeline selects an existing build number, resolves its build run against a configured definition, and verifies that the corresponding registry image tag exists. Some selected-release deployment blocks in that source are commented out; the public orchestration makes the intended selected-build promotion path executable. It does not imply that every archived production path was runnable unchanged.

The application manifest reference includes rolling updates, startup/liveness/readiness probes, mounted configuration, a ClusterIP Service, TLS ingress, and optional resource-based autoscaling. Kustomize application deployment is provided by the separately inspected `aks-common-templates` source.

## Preserved behavior and deliberate changes

| Reference behavior | Public implementation |
| --- | --- |
| Build once, use current build or select a previous build | Publish an immutable image receipt; select its successful build run for promotion |
| Select one or both AKS clusters | Explicit aks01/aks02 slot selection, sequential by default, with parallel selection available |
| Ordered environment and cluster promotion | Protected target environments and explicit dependencies |
| Kustomize configuration and deployment | Render a committed source snapshot; verify the release receipt before namespace-scoped apply |
| Post-deploy application content verification | Readiness plus exact source revision and requested slot through that cluster's Service |
| Application rollback | Reapply a previously approved source/digest bundle deliberately |
| Optional database stages | The synthetic app has no database; add reviewed migrations and compatibility checks for a real workload |
| Optional ConfigMap/CSI integration | Maintained Gateway profile uses workload identity and CSI-synchronized TLS; direct-Service alternative needs neither |

The public image is identified by a digest rather than a mutable version tag. The sample uses one central registry shared by both cluster slots, rather than copying registry-specific company routing. Configuration remains committed Kustomize overlays; production secrets belong to separately controlled workload identity/CSI resources.

The archived content verification disables TLS verification and checks a shared ingress hostname before attempting automatic rollout undo. A shared hostname can identify the active slot rather than the cluster being deployed. Public delivery verifies the selected cluster directly, fails visibly, and leaves rollback as an explicit approved-release action. Gateway cutover remains separately reviewed infrastructure work.

Node.js keeps the sample small and locally executable without runtime packages. It does not claim language parity with the original .NET application. The sample has two replicas and a disruption budget; workload-specific autoscaling and database/business behavior are intentionally left to real application owners.

The maintained profile retains a separate platform-services tier and ingress-backed application path, modernized to Envoy Gateway and Gateway API. The platform owns the private listener and load balancer; the app owns HTTPRoute, ClusterIP Service, workload identity/CSI integration, autoscaling and NetworkPolicy. It re-establishes TLS from Application Gateway to the cluster listener, then uses policy-constrained HTTP to the app. It does not claim pod mTLS.

The direct-Service profile remains a simpler teaching option; it is not full source parity. The separate ingress-compat profile records sanitized retired community ingress-nginx behavior for migration comparison, not a supported new-deployment default. The stateless demo does not carry over application-specific cookie affinity, buffering or timeout settings without corresponding requirements and tests. No private credentials, certificate keys, variable groups or corporate identifiers are copied.

The source design document reinforces independent infrastructure, Helm platform services and Kustomize application lifecycles. Build-once selected-release promotion and an independently reviewed stable-DNS traffic switch are preserved. Gateway API modernization is a public reference implementation; it is not evidence that the original production estate has been migrated.
