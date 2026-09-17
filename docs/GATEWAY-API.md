# Maintained Gateway API application profile

The additional [Argo CD deployment guide](ARGO-CD.md) reuses this same infrastructure, Gateway API profile and application configuration. Choose either direct application deployment or Argo ownership for each slot.

The recommended full profile is [delivery.gateway.apps.json](../delivery.gateway.apps.json), with [bootstrap.gateway.apps.json](../bootstrap.gateway.apps.json) and the [Gateway API Kustomize base](../deploy/gateway-api/base/kustomization.yaml). Private pipeline examples select these files. The independent platform consumer installs Envoy Gateway; application delivery creates an HTTPRoute, not the controller or listener.

| Profile | Purpose | Frontend ownership |
| --- | --- | --- |
| Gateway API | Maintained full ingress path with CSI TLS and scoped app network policy | Platform Envoy Service; candidate aks01 .21 / aks02 .21 |
| Direct Service | Lightweight one-app example using `delivery.apps.json` | App LoadBalancer Service; aks01 .20 / aks02 .20 |
| `ingress-compat` | Archived retired community ingress-nginx behavior for migration comparison only | Legacy controller; never a new default |

The profiles are alternatives for the same application. Do not apply both overlays to the same Deployment, and do not reuse a frontend address owned by another Service. Their resource lists differ; apply is not automatic pruning. During migration, keep the active old slot intact and first use the maintained profile on an inactive slot with a distinct candidate address.

## Ownership and prerequisites

The maintained platform contract is Envoy Gateway 1.9.1, Gateway API 1.6.1, controller namespace `envoy-gateway-system` and GatewayClass `envoy-gateway`. Platform-owned `Gateway/platform-demo-private` lives in `platform-demo` with listeners `https-web` and `https-api`. Its Envoy data plane uses GatewayNamespace mode. The app-owned `HTTPRoute/platform-demo` attaches to those listeners and routes `web.example.test` and `api.example.test` to `Service/platform-demo:80`.

The route preserves the path, including `/api`. The synthetic app supports both ordinary and API-prefixed endpoints, so no NGINX regex rewrite, sticky sessions or buffering annotations are carried over. Stateful apps must test their own routing, timeout, upload and session requirements before replacing old annotations.

First run the independent [platform service lifecycle](https://github.com/MikeeeGit/aks-delivery-templates/blob/main/docs/platform-services.md), then the [namespace bootstrap](https://github.com/MikeeeGit/aks-delivery-templates/blob/main/docs/bootstrap.md). Establish explicit namespace authorization for HTTPRoute and SecretProviderClass, in addition to ordinary resource permissions. The app principal must not gain ownership of GatewayClass, CRDs or the controller.

## CSI TLS and workload identity

The full profile uses `ServiceAccount/platform-demo` and `SecretProviderClass/platform-demo-tls`. Replace the synthetic client and tenant IDs in their manifests with the verified workload identity handoff. The example vault is `example-platform-app`; object `platform-demo-ingress` must contain the certificate and private key as an exportable PEM secret. Its SANs cover the real web/API hosts. Certificate upload and renewal are external prerequisites.

A dedicated UAMI has a federated credential for the exact ServiceAccount subject on each selected cluster issuer and least-privilege Key Vault access. Do not reuse control-plane or kubelet identity for this application. AKS workload identity and Key Vault CSI add-ons must be enabled and reachable.

The pod's CSI mount causes synchronization into `Secret/platform-demo-tls` of type `kubernetes.io/tls`; both tls.key and tls.crt are derived from the same certificate secret object. The listener references this same-namespace Secret. A new Gateway can stay pending until the pod mounts it; require readiness afterwards. CSI access failure, incomplete chain, expired certificate or wrong SAN must stop acceptance. See [Microsoft's CSI certificate synchronization example](https://learn.microsoft.com/en-us/azure/aks/csi-secrets-store-nginx-tls); the mechanism is reused with Gateway API rather than its legacy ingress controller.

The ordinary API token is not automatically mounted. Workload identity uses its scoped projected token through the webhook. Secret values and private keys are never part of the source, delivery receipt or ConfigMap.

## Network and runtime behavior

The app Service is ClusterIP. The platform owns the private Azure load balancer at `10.81.0.21` / `10.81.4.21` and reviewed subnet/source restrictions. App pods remain nonroot with read-only filesystem, dropped capabilities and RuntimeDefault seccomp. The HPA owns replica count, with minimum 2, maximum 3 and CPU target 50%; ensure metrics are available.

NetworkPolicy selects only application pods, denies unsolicited ingress/egress, permits TCP8080 from the exact same-namespace Envoy Gateway proxy labels, and permits DNS to kube-system/CoreDNS on TCP/UDP53. It does not default-deny the platform-owned Envoy pods. Platform policy must govern those independently. Real applications need explicit additional service dependencies; do not add blanket external HTTPS merely to make errors disappear.

CSI node/provider components fetch Key Vault outside the application's pod network-policy path. Their DNS, private endpoints and firewall requirements still need platform validation. The final Envoy-to-app hop is HTTP; this profile is not pod mTLS.

## Verification and cutover

`verification.ingress` binds the Gateway name, HTTPRoute name and expected hostnames. The shared helper verifies current-generation Gateway `Programmed` and HTTPRoute `Accepted`/`ResolvedRefs`, discovers the owning proxy Service and verifies HTTPS for the exact slot/revision with SNI and trust checking. For private CA certificates, add `ca_file` under `verification.ingress` pointing to a committed public CA bundle, such as `deploy/trust/backend-ca.crt`. Never put a private key there.

That check uses port-forwarding and does not establish an Azure ILB allocation or network path. Confirm the actual Service status IP, then test HTTPS from an allowed private source and through Application Gateway's preview route. See [delivery and cutover](DELIVERY.md), the shared [migration guide](https://github.com/MikeeeGit/aks-delivery-templates/blob/main/docs/ingress-migration.md), and the [Gateway API decision](https://github.com/MikeeeGit/aks-delivery-templates/blob/main/docs/decisions/0001-gateway-api.md).

The local two-cluster acceptance harness substitutes temporary test CA/certificate material and removes Azure-only CSI identity mounting in its disposable source fixture. It exercises actual Envoy routing and certificate verification when Docker is available. It cannot qualify Azure workload identity, Key Vault, Azure load balancers, network policy enforcement or WAF. Retain its report before claiming that acceptance ran.


## Custom-resource authorization boundary

The shared [opt-in authorization example](https://github.com/MikeeeGit/aks-delivery-templates/tree/main/examples/authorization) provides an explicit HTTPRoute/SecretProviderClass grant recipe. Its Azure ABAC mechanism is preview and needs operator review. Namespace Writer also has broad rights over ordinary objects in the same namespace, including the platform proxy and TLS Secret; separating release ownership is not an adversarial tenant-security boundary. Use a separately reviewed tighter authorization/namespace design when that isolation is required.


The platform bootstrap establishes ServiceAccount platform-demo, and the application profile reapplies the same reviewed identity binding. Replace the synthetic client ID consistently in the platform configuration, application ServiceAccount and SecretProviderClass using the verified handoff. A mismatch is a configuration failure; do not let an app rollout silently change which Azure identity the platform intended.
