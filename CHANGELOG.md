# Changelog

## Unreleased

- Replace the public pipeline selectors with targetCluster/targetClusters and
  target-cluster/target-clusters; pin the matching shared-template revision.
- Add Azure workload build-only/GitOps proposal/lifecycle callers for both CI hosts.
- Add explicit Azure target validation, coded scoped Argo sync permissions, CSI
  verification and exact non-cascading retirement.
- Exercise sync identity permissions and workload-preserving retirement in real
  two-cluster Argo acceptance; retain the direct delivery path.


## Unreleased

- Add a synthetic Node.js app with health, readiness, version, and API-path endpoints.
- Add a pinned multi-stage nonroot container build and reproducible revision metadata.
- Add Kustomize delivery for two private AKS clusters using the same image digest.
- Add operator-owned restricted namespace bootstrap and annotated internal LoadBalancer Services.
- Add application, manifest, and smoke tests plus first-time deployment and cutover guidance.

- Reconcile September AKS source patterns: selected build promotion, selectable target clusters, explicit post-deploy verification, and reviewed rollback.
- Add pinned two-kind-cluster acceptance with real image digests, update/rollback checks, scoped cleanup, and persistent result reports.

### Maintained ingress profile

- Add application HTTPRoutes for Envoy Gateway with separate platform-owned listeners, CSI TLS, workload identity, autoscaling and scoped NetworkPolicy.
- Make private caller examples select the maintained profile; retain direct-Service and archived ingress compatibility as explicit alternatives.
- Extend two-cluster acceptance to real pinned Envoy and verified HTTPS, plus inactive update, rollback and unchanged-active checks.
- Run Node application tests inside every Docker build before producing the runtime image.
