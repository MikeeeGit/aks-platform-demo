# Security

Report a suspected vulnerability privately through the repository host's private vulnerability reporting feature when enabled. If that feature is unavailable, use a maintainer's published private contact channel. Do not publish credentials or exploit details in a public issue.

The sample exposes synthetic build metadata and has no authentication or business data. Keep the platform-owned Envoy LoadBalancer (or the lightweight direct-Service alternative) private and constrain source ranges. Apply the WAF, TLS, identity, network, and monitoring controls appropriate to a real application before adapting it.

Build and deployment examples require private trusted pipelines, protected branches/environments, short-lived federated credentials, and scoped permissions. Public pull-request jobs must not access cloud identities or private runners. Image digest pinning identifies content; it does not replace vulnerability scanning, provenance verification, or registry access controls.

Never store passwords, tokens, certificates, private keys, kubeconfig, or real configuration in this repository.
