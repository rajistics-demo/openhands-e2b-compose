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
