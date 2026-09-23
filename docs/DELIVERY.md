# Build, promote, verify, and cut over

The additional [Argo CD deployment guide](ARGO-CD.md) reuses this same infrastructure, Gateway API profile and application configuration. Choose either direct application deployment or Argo ownership for each cluster.

Complete [setup](SETUP.md) and [the maintained Gateway API profile](GATEWAY-API.md). Private callers select `delivery.gateway.apps.json`. The direct-Service alternative is `delivery.apps.json` with `bootstrap.apps.json` and requires corresponding caller changes.

## Build once and select the release

Review and commit source, Dockerfile, context and overlays together. The shared helper snapshots the selected full commit; uncommitted changes do not enter its build or render. The Node image bakes that commit into `/version`.

The shared build pushes one image, scans its exact remote digest, and produces a promotable release receipt only after the required security gate succeeds. Keep the private scan report and producer run metadata. Registry tags are push handles, not deployment selectors.

After installing the pinned shared helper dependencies:

~~~bash
REVISION="$(git rev-parse HEAD)"
python "$TEMPLATE_DIR/scripts/delivery.py" validate --source . --config delivery.gateway.apps.json
python "$TEMPLATE_DIR/scripts/delivery.py" build --source . --config delivery.gateway.apps.json --commit "$REVISION" --output .delivery/build
~~~

The build command pushes to Azure and requires approved registry credentials and Docker Buildx. The build/deploy caller passes the receipt directly; the promotion caller selects a verified successful build run without rebuilding. Select the inactive cluster first, or both clusters in explicit order. Sequential deployment stops on the first failure.

A credential-free render is:

~~~bash
python "$TEMPLATE_DIR/scripts/delivery.py" render --source . --config delivery.gateway.apps.json --commit "$REVISION" --digest "$IMAGE_DIGEST" --environment pprd --region uks --slot aks02 --output .delivery/aks02
~~~

Review `manifest.yaml` and `release.json`. Keep the receipt SHA from the trusted render job as the independent approval value. A checksum is not a signature; trusted artifact storage and approval remain necessary.

~~~bash
python "$TEMPLATE_DIR/scripts/delivery.py" deploy --output .delivery/aks02 --receipt-sha256 "$APPROVED_RECEIPT_SHA256"
~~~

This authenticates to the selected private cluster, checks access, applies the bundle and waits for rollout. It checks readiness and full revision/cluster through the application Service, then checks current-generation Gateway/HTTPRoute status and verified HTTPS through the selected Gateway proxy. It does not modify live gateway traffic.

## Verify the Azure frontend and full path

Discover the Envoy Service through its owning-Gateway labels; generated names are not a stable contract:

~~~bash
SELECTOR='gateway.envoyproxy.io/owning-gateway-name=platform-demo-private,gateway.envoyproxy.io/owning-gateway-namespace=platform-demo'
kubectl --context "$EXPECTED_CONTEXT" -n platform-demo get service -l "$SELECTOR" -o wide
~~~

There must be exactly one expected Service. Confirm its `status.loadBalancer.ingress` contains `10.81.4.21` for aks02, or `10.81.0.21` for aks01. A pending/wrong IP is a failure requiring review of subnet allocation, Service events, network permissions and platform values.

Port-forward verification exercises Envoy but bypasses the cloud frontend. From an explicitly allowed source, separately test the private IP using real certificate hostname validation:

~~~bash
curl --fail --silent --show-error --resolve web.example.test:443:10.81.4.21 https://web.example.test/readyz
curl --fail --silent --show-error --resolve web.example.test:443:10.81.4.21 https://web.example.test/version
curl --fail --silent --show-error --resolve api.example.test:443:10.81.4.21 https://api.example.test/api/version
~~~

Replace the reserved hosts with certificate SANs. For a private CA pass only the reviewed public CA via `--cacert`; never disable TLS validation. Assert that version JSON has the expected `slot` and full `revision`. Private source ranges, routing and DNS must permit this check; add only the actual diagnostic source if required.

Check Application Gateway backend health and use its preview listener:

~~~bash
node scripts/smoke.mjs --url https://preview.example.test --host preview.example.test --expected-slot aks02 --expected-revision "$REVISION"
~~~

The smoke helper checks health/readiness/version, rejects redirects and mismatches, and exits nonzero on failure. HTTPS uses the URL hostname for SNI and trust; Host override does not bypass certificate validation. A private CA can be supplied with Node's `NODE_EXTRA_CA_CERTS`. The preview frontend may forward `web.example.test` to the cluster, so the backend certificate covers web/API rather than necessarily the preview frontend hostname.

## Approve traffic independently

Use the companion gateway's ingress-TLS candidate profile first. It changes the preview path to aks02 Envoy `10.81.4.21` over HTTPS and can retain live direct-Service traffic at aks01 `10.81.0.20` over HTTP.

After acceptance, review the separate Terraform plan to switch `service.apps.internal.example` and HTTPS backend settings. Apply the saved plan, verify active web/API responses and observe WAF/backend health, latency and errors. Keep the previous healthy cluster during the rollback window.

During HTTP-to-HTTPS migration, rollback to the original path restores both address and protocol. Later HTTPS-to-HTTPS cluster switches retain the TLS contract but still require the correct alias and health checks. DNS caches and existing connections mean a DNS update is not instantaneous; draining settings do not create an atomic switch.

App rollback deliberately reapplies a previous approved source/digest receipt. It is separate from traffic rollback, controller/CRD rollback and database recovery. No app workflow automatically changes DNS or undoes production data changes.

## Removal

Retarget traffic before removing application resources. Review deletion of the app's route, Deployment, ClusterIP Service, policy, autoscaler and CSI binding. The platform owner controls Gateway, controller/load balancer and namespace removal. Shared apply does not automatically prune objects omitted from a later manifest. Switching a profile does not silently delete the old Service or Ingress.
