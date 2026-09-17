# Desired application releases in Git

This directory is intentionally empty of deployable releases in the public template. A private consumer's reviewed GitOps proposal adds one real release at:

```text
gitops/releases/pprd/uks/aks01/
  manifest.yaml
  release.json
  build-release.json
  ingress-ca.pem       # optional public CA; never a private key
gitops/releases/pprd/uks/aks02/
  ...
```

The same Kustomize source used by direct pipelines generates the concrete YAML upstream. Argo reads only `manifest.yaml`, through its explicit directory include filter. It does not run Kustomize again and does not apply the receipt JSON files.

Do not copy synthetic image digests into an active release. Use a successful image-build receipt and the proposal pipeline. Review and merge one slot's PR, synchronize its exact commit, then complete HTTPS verification before proposing the next slot. The build's source commit and the GitOps merge commit have different purposes.

Start with [Argo CD deployment](../docs/ARGO-CD.md). Keep repository credentials, TLS private keys, tokens and kubeconfigs outside Git.
