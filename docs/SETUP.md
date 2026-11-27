# Set up E2B with Enterprise Compose

Use an isolated [Enterprise 1.88.0 Compose install](https://github.com/OpenHands/enterprise/blob/c6191be/README.md#run-locally-with-docker-compose)
and verify a stock conversation first. The test baseline used Agent Server
1.60.1-python and Canvas 1.35.0. Compose 2.24.4 or later supports the overlay's
`!override` tag.

## Build and start

1. Obtain an E2B API key for the environment in which you will build and run
   the template. Load it as `E2B_API_KEY` from private operator configuration.
   A self-hosted E2B deployment also supplies its own `E2B_DOMAIN` and, where
   required, `E2B_API_URL`.
2. Generate a bootstrap key and build the image in that environment.
   Run the following from this repository with `INIT_KEY_FILE` set to a
   private path outside the checkout.

   ```bash
   python3 -m venv .venv
   . .venv/bin/activate
   python -m pip install 'e2b==2.62.0'
   umask 077
   openssl rand -hex 32 > "$INIT_KEY_FILE"
   python scripts/build_template.py --name openhands-enterprise-pilot --init-key-file "$INIT_KEY_FILE"
   ```

3. Copy [e2b.env.example](../compose/e2b.env.example) into the isolated install
   directory as `e2b.env`, set mode 0600, and fill in the E2B API key,
   matching bootstrap key, returned template ID, and guest-reachable HTTPS
   callback origin. Keep the original stock Compose files unchanged.
4. Make the configured callback origin reachable from E2B. Enterprise expects
   authenticated POSTs under `/api/v1/webhooks`, including conversation
   registration and event ingestion. An operator-only app can use a separate
   TLS callback listener. Expose only required callback routes there and
   preserve Enterprise's session-key checks. Use a stable ingress design if
   the provider may change guest egress addresses.
5. Layer the overlay after your existing Compose files. Include any existing
   HTTPS overlay before the E2B file. Keep `WEB_URL` and
   `AGENT_CANVAS_LOCK_TO_CLOUD` set to the browser-facing app origin.

   ```bash
   docker compose --env-file .env --env-file e2b.env \
     -f docker-compose.yml -f docker-compose.https.yml \
     -f "$E2B_PACKAGE/compose/docker-compose.e2b.yml" config --quiet
   docker compose --env-file .env --env-file e2b.env \
     -f docker-compose.yml -f docker-compose.https.yml \
     -f "$E2B_PACKAGE/compose/docker-compose.e2b.yml" up -d --wait
   ```

   Set `E2B_PACKAGE` to the absolute clone path before running these commands
   from the Compose directory.

## Configure small and large E2B profiles

Use an Enterprise image built with the E2B adapter changes for independent
spec IDs and provider egress policy. The stock 1.88.0 pilot uses the single
template configuration above.

Build two templates with your desired CPU and memory sizes. Use the existing
`E2B_TEMPLATE` and `E2B_INIT_API_KEY` entries for the small template. Add
`E2B_LARGE_TEMPLATE`, `E2B_LARGE_INIT_API_KEY`, and
`E2B_APPROVED_EGRESS_HOSTS` to the same private env file. For example, set
`E2B_APPROVED_EGRESS_HOSTS='["callbacks.example.com","model.example.com"]'`
using your actual callback, model, and integration hosts. The E2B provider
applies the allowlist over a default IPv4 deny. This is a hostname egress
policy.

Add `-f "$E2B_PACKAGE/compose/docker-compose.e2b-profiles.yml"` after the
base E2B overlay in both Compose commands. Select `microvm-small` or
`microvm-large` through the Sandbox Spec picker. Releases with Sandbox Spec
references on Agent Profiles can save the same ID there. Enterprise records
the stable ID. The E2B adapter resolves it to the configured template when
starting a VM.

The November 26 AWS check rendered both profiles for `app` and `worker`.
All 100 focused Enterprise E2B tests passed against temporary PostgreSQL,
including the profile configuration, provider egress arguments, and pause
refusal handling.

## Configure E2B warm inventory

Use a candidate Enterprise build with the E2B warm inventory worker enabled.
Keep the default size of zero for installations that do not need prewarming.
To opt in, set `E2B_WARM_POOL_SIZE` for hot inventory,
`E2B_WARM_POOL_PAUSED_SIZE` for suspended reserve inventory,
`E2B_WARM_POOL_ORG_ID` to the authorized organization's UUID, and
`E2B_WARM_POOL_SPEC_ID` to one Sandbox Spec using an immutable E2B template ID.
The two targets can total at most ten. The configured sandbox lease must be at
least 90 seconds. Keep the Compose
worker running to refill the pool and retire unused inventory.

The worker keeps dormant VMs for that organization and spec. The launch path
consumes a slot before sending the VM any conversation credentials. It prefers
hot inventory and falls back to the paused reserve or a normal cold launch. Ordinary
launches use the cold path whenever no eligible slot is available. PostgreSQL
records pending creates so an interrupted worker can retry the same provider
idempotency key. Disable the pool and let its worker retire available inventory
before rotating the E2B provider credential or removing the test stack.

The November 26 AWS probe claimed one running and one paused VM from live
E2B. Both claims returned the preallocated VM ID and received distinct
Enterprise session keys. The probe simulated the Agent Server handshake, so
it does not measure Canvas or live preview latency.

## Enable the Canvas Pause button

The base E2B adapter already supports Sandbox V1 pause and resume. To show
the button in Canvas, build the companion image so Enterprise advertises
the capability on E2B conversations.

```bash
docker build -t local/openhands-enterprise-e2b-pause:1.88.0 "$E2B_PACKAGE/compose/pause-overlay"
```

Add `-f "$E2B_PACKAGE/compose/docker-compose.e2b-pause.yml"` after the E2B
overlay in both Compose commands above. Follow the
[pinned Canvas build and browser test](../canvas/README.md). Use a
synthetic preview on the E2B endpoint, and tag the conversation
`canvasrecoverypilotpreview=true`. The companion adds API fields and
leaves the database schema unchanged.

## Validate and clean up

Confirm the Sandbox V1 spec catalog lists the exact `E2B_TEMPLATE` value.
Pass that ID as the `sandbox_spec_id` query parameter on
`POST /api/v1/sandboxes` to select the environment explicitly. Start a native
conversation and confirm a tool event reaches Enterprise history. Copy
[preview_probe.py](../validation/preview_probe.py) to the synthetic workspace,
run it with `--listen 0.0.0.0 --port 8011`, and click **Check live process**.

Pause the sandbox using the provider-aware Canvas build or Sandbox V1
`POST /api/v1/sandboxes/<id>/pause`. Verify settled PAUSED state and verify the
original history through `GET /api/v1/conversation/<id>/events/search`.
Reopen the original conversation in Canvas or call Sandbox V1 `/resume`,
then verify the workspace marker, boot marker and counter. Time history and
the first successful preview interaction separately. Only use synthetic
preview content on the default E2B browser endpoint.

To roll back, delete only the task-owned sandbox, confirm its termination
in E2B, and start Compose with the original file list. Remove the temporary
template and credentials under your normal provider retention policy.

## Qualify native E2B lifecycle

Run this provider-level probe from an AWS worker or CI before attempting the
same flow in Canvas. Use a dedicated synthetic Linux template with Python 3 and
curl, without customer data, injected credentials or mounted volumes. Use a
project with capacity for the original sandbox and two forks. Load the project's
`E2B_API_KEY` through the worker's private configuration.

```bash
python -m pip install 'e2b==2.64.0'
python validation/lifecycle_probe.py --template "$E2B_TEST_TEMPLATE_ID"
```

The probe starts a loopback preview, suspends the original VM, and verifies that
the same sandbox resumes with its workspace and in-memory counter intact. It
then forks two children and checks that writes to one child's files and memory
leave the original and the sibling intact. It prints timings for start, pause,
resume, and fork, and requests cleanup for every known sandbox. Inspect the
provider inventory after a timeout or cleanup failure. E2B paused sandboxes
require explicit deletion, so the running VM timeout alone cannot clean up a
failed pause-and-resume test.

This probe qualifies provider behavior. Canvas conversation forking, preview
authorization require separate Enterprise tests. See
[E2B forking](https://docs.e2b.dev/sandbox/fork) and
[E2B persistence](https://docs.e2b.dev/sandbox/persistence).

### AWS Compose run on November 26, 2026

One run using E2B Cloud's `base` template and Python SDK 2.64.0 passed. These
are elapsed times from the Compose runner through E2B's SDK. The preview was
checked over loopback inside each sandbox.

| Operation | Seconds |
| --- | ---: |
| Create VM and reach the synthetic preview | 0.900 |
| Suspend and observe `PAUSED` | 0.178 |
| Resume and verify the original preview and workspace | 0.382 |
| Return two forked VM handles | 0.951 |
| Verify both fork previews and workspaces | 1.113 |

The original and sibling retained their file and counter values after the
first fork changed both. All three sandboxes were verified terminated, and the
temporary Compose project was removed. Canvas and browser latency were outside
this run's scope.

## Configure private previews

Apply the matching Enterprise candidate and its ingress migration, then add
`compose/docker-compose.e2b-private.yml` after the E2B overlay. Set the HTTPS
app origin with `WEB_URL` and arrange DNS for `*.preview.<app-host>`. Set
`APP_IMAGE` to the matching candidate image for the app and gateway. Mount the
wildcard certificate and private key via `E2B_PREVIEW_TLS_DIR`, and set a random
`E2B_INGRESS_SIGNING_KEY`. The app and gateway use the shared Enterprise
filestore for the database encryption key.

The adapter starts new guests with E2B public traffic disabled. The gateway
authenticates the Agent Server connection and issues a short-lived, host-only
cookie for a preview. After checking it, the gateway sends a bounded HTTP
request to the guest's loopback preview port through E2B's management channel.
This keeps the provider ingress header and browser credentials out of the
preview application. The pilot serves HTTP previews with a 48 KiB request
limit and a 2 MiB response limit. Existing publicly reachable sandboxes should
expire or be retired before switching this fixture to private ingress.

## Enable E2B fork

Use a routed Enterprise candidate with the E2B and Kubernetes Sandbox
Spec service, the workspace reuse capability, and Agent Server 1.64.1.
The live demo used the Sandbox Spec catalog candidate based on Enterprise
change #1560 and Canvas 1.35.0 with the provider Pause and E2B fork patches.
The single-provider Enterprise 1.88.0 quickstart uses a different backend
interface.

From this repository, generate the overlay using a local copy of the
matching Enterprise candidate source. Use the effective source files for
the app and worker, including any existing candidate overlays.

```sh
export E2B_FORK_OVERLAY_DIR=/absolute/path/to/generated/e2b-fork
python3 compose/fork-overlay/prepare.py /absolute/path/to/enterprise-candidate "$E2B_FORK_OVERLAY_DIR"
```

The generator leaves the input untouched and checks that the expected
integration points are present before writing the output. Append
`compose/docker-compose.e2b-fork.yml` to the candidate's Compose file
list, after overrides for the app and worker. Set `E2B_FORK_ORG_ID` to the
allowed organization's UUID. Set `E2B_FORK_MODEL_URL` to the candidate's
approved HTTPS demo model URL, for example
`https://openhands.example.com/live-model/v1`. For DeepSeek V4.1 Flash,
the candidate's existing model binding hook must issue a separate
credential for each new VM. The scripted GPT-4o-mini fixture uses
`/synthetic-model/v1` on the same origin. Use whichever of those two
model paths your candidate already serves.

Render the combined files with `docker compose config --quiet` before
recreating the app and worker. Build Canvas with the
[optional fork patch](../canvas/README.md). Only owned, running E2B chats
with a supported model and a bounded workspace under `/workspace/project`
advertise fork support. The demo copies one workspace and requires room
for both the source and the fork. Follow the
[Canvas walkthrough](DEMO.md#fork-an-e2b-chat) to verify the result.
