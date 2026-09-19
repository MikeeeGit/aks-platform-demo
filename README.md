# AKS platform demo

A small, synthetic Node.js application demonstrating build-once delivery to two private AKS clusters using an immutable image digest and shared Kustomize configuration. Choose direct pipeline deployment or the additional Argo CD method. It provides health, readiness, and version endpoints so a deployment and Application Gateway cutover can be checked explicitly.

This repository is an application example. Infrastructure comes from the companion `azure-network-foundation`, `azure-aks-foundation`, and `azure-application-gateway` repositories. Reusable delivery comes from `aks-delivery-templates`. Example names, addresses, subscriptions, and domains are synthetic; replace them together before deployment.

## Run locally

Install Node.js 24, then:

```bash
npm ci --ignore-scripts --no-audit --no-fund
npm run verify
npm start
```

Visit `http://localhost:8080/version`. The application has no npm runtime dependencies. A local build identifies its revision as `development` unless `BUILD_REVISION` contains the full lowercase Git commit.

| Endpoint | Purpose |
| --- | --- |
| `/healthz` | Process liveness |
| `/readyz` | Readiness; returns 503 while draining |
| `/version` | Application, package version, full source revision, and runtime slot |
| `/api/healthz`, `/api/readyz`, `/api/version` | Equivalent endpoints for the gateway's API path rule |

`APP_SLOT` accepts `local`, `aks01`, or `aks02`. `PORT` defaults to 8080. `SHUTDOWN_DELAY_MS` defaults to 3000; SIGTERM marks the app unready before closing connections. The source revision is baked into the image; the slot is runtime configuration.

## Delivery layout

- [Maintained delivery context](delivery.gateway.apps.json): registry, explicit cluster targets and controller HTTPS verification.
- [Gateway API app base](deploy/gateway-api/base/kustomization.yaml): workload, ClusterIP Service, HTTPRoute, CSI TLS, autoscaling and NetworkPolicy.
- [aks01 overlay](deploy/gateway-api/overlays/pprd/uks/aks01/kustomization.yaml) and [aks02 overlay](deploy/gateway-api/overlays/pprd/uks/aks02/kustomization.yaml): the same application with explicit slot metadata.
- [Operator namespace bootstrap](deploy/bootstrap/namespace.yaml): applied separately with Pod Security admission restricted.
- [Private pipeline examples](examples/delivery/README.md): trusted build and selected-release promotion through shared templates.
- [Argo CD example](docs/ARGO-CD.md): reviewed GitOps proposals and per-slot reconciliation of the same rendered YAML.
- [Smoke helper](scripts/smoke.mjs): expected slot and full revision checks over HTTP or verified HTTPS.

The recommended full path uses the independently managed Envoy Gateway platform profile and application-owned HTTPRoutes. Read the [Gateway API profile guide](docs/GATEWAY-API.md) before first deployment. Its ClusterIP Service forwards port 80 to the nonroot app on 8080. The platform pipeline owns the private listener and load balancer. The [direct-Service context](delivery.apps.json) remains a lightweight alternative with its own internal LoadBalancer; it is not the full ingress architecture.

## Guides

Start with [first-time setup](docs/SETUP.md), then choose [direct delivery and cutover](docs/DELIVERY.md) or [Argo CD delivery](docs/ARGO-CD.md). Argo supports plain YAML and Kustomize; here CI renders the shared source and Argo reads the resulting plain YAML from Git. See [testing and two-cluster acceptance](docs/TESTING.md), [source-pattern provenance](docs/PROVENANCE.md) for preserved behavior and deliberate changes, [contribution guidance](CONTRIBUTING.md), and [security guidance](SECURITY.md).

## Verification scope

Local checks build the Node application, exercise its HTTP process and graceful shutdown, test the smoke helper, and render both overlays with kubectl without contacting Kubernetes. The shared renderer checks both committed-source overlays and applies the same image digest.

The two-cluster acceptance harness builds and deploys real images when Docker is available; see its retained report before claiming acceptance. Container build and runtime checks require Docker; cloud checks require provisioned infrastructure and scoped access. Passing local tests does not establish an Azure deployment, successful image pull, an assigned LoadBalancer frontend, or gateway backend health. The deployment guide records those separate checks.

Licensed under [Apache 2.0](LICENSE).

## CI change scope

Markdown-only edits use lightweight required GitHub checks and are excluded from automatic Azure validation builds. Changes to Terraform, application code, scripts, workflow definitions or executable examples still run full validation, including examples stored under docs/. Mixed changes also run full validation. Manual GitHub runs and unknown Git comparison ranges default to full validation.
