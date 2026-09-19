# Application design and compatibility

This is a synthetic sample application for demonstrating immutable releases, explicit cluster selection and independent infrastructure, platform and application lifecycles. It contains no business application or customer data.

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

The public image is identified by a digest rather than a mutable version tag. The sample uses one central registry shared by both cluster slots, with registry access configured separately from application promotion. Configuration remains committed Kustomize overlays; production secrets belong to separately controlled workload identity/CSI resources.

Delivery verifies the selected cluster directly with certificate validation and explicit source/slot checks. A shared ingress hostname can identify the active slot rather than the candidate, so it is insufficient for selected-slot verification. Rollback is an explicit approved-release action; gateway cutover remains separately reviewed infrastructure work.

Node.js keeps the sample small and locally executable without runtime packages. The sample has two replicas and a disruption budget; workload-specific autoscaling and database/business behavior are intentionally left to real application owners.

The maintained profile retains a separate platform-services tier and ingress-backed application path, modernized to Envoy Gateway and Gateway API. The platform owns the private listener and load balancer; the app owns HTTPRoute, ClusterIP Service, workload identity/CSI integration, autoscaling and NetworkPolicy. It re-establishes TLS from Application Gateway to the cluster listener, then uses policy-constrained HTTP to the app. It does not claim pod mTLS.

The direct-Service profile remains a simpler teaching option; it is not full source parity. The separate ingress-compat profile records sanitized retired community ingress-nginx behavior for migration comparison, not a supported new-deployment default. The stateless demo does not carry over application-specific cookie affinity, buffering or timeout settings without corresponding requirements and tests. No private credentials, certificate keys, variable groups or corporate identifiers are copied.

Infrastructure, Helm platform services and Kustomize application configuration have independent lifecycles. Build-once selected-release promotion and a separately reviewed stable-DNS traffic switch remain separate operations.
