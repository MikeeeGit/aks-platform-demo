# Changelog

## Unreleased

- Add a synthetic Node.js app with health, readiness, version, and API-path endpoints.
- Add a pinned multi-stage nonroot container build and reproducible revision metadata.
- Add Kustomize delivery for two private AKS slots using the same image digest.
- Add operator-owned restricted namespace bootstrap and annotated internal LoadBalancer Services.
- Add application, manifest, and smoke tests plus first-time deployment and cutover guidance.

- Reconcile September AKS source patterns: selected build promotion, selectable cluster slots, explicit post-deploy verification, and reviewed rollback.
- Add pinned two-kind-cluster acceptance with real image digests, update/rollback checks, scoped cleanup, and persistent result reports.

### Maintained ingress profile

- Add application HTTPRoutes for Envoy Gateway with separate platform-owned listeners, CSI TLS, workload identity, autoscaling and scoped NetworkPolicy.
- Make private caller examples select the maintained profile; retain direct-Service and archived ingress compatibility as explicit alternatives.
- Extend two-cluster acceptance to real pinned Envoy and verified HTTPS, plus inactive update, rollback and unchanged-active checks.
- Run Node application tests inside every Docker build before producing the runtime image.
