# E2B for OpenHands Enterprise

This provider-facing package describes the existing Enterprise managed adapter and its Canvas qualification path. The pre-existing Canvas E2B POC remains a separate proof point.

| Seam | E2B mapping |
|---|---|
| Environment | Map an operator-selected sandbox spec to a pinned E2B template. Include the matching Agent Server and fixture ports. |
| Lifecycle and ownership | The Enterprise managed service records sandbox ownership and maps provision, read, pause, resume and delete to E2B. |
| Guest credentials | The control plane provides sandbox-scoped Agent Server configuration and event callbacks. The E2B API key stays on the control plane. |
| HTTP, WebSocket and preview | E2B exposes runtime ports through provider hostnames. Keep both Agent Server and preview access scoped to the correct sandbox. |
| Persistence | E2B pause and reconnect support stateful continuation. Qualify the actual preview process and original conversation in Canvas. Treat expiry recovery as a separate test. |
| Verification | Verify app-side event persistence and remove the disposable VM. Record SDK and template versions, feature availability, and any fallback. |

Use `provider.json` with the shared [API smoke](../openhands-provider-contract/README.md). Run the [Canvas acceptance case](../openhands-provider-contract/CANVAS.md) on the selected Enterprise version. The [E2B SDK reference](https://e2b.dev/docs/sdk-reference/python-sdk/v2.5.0/sandbox_sync) documents the provider primitives.
