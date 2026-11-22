# E2B interfaces and design choices

| Seam | E2B choice and reason | Alternative to qualify |
|---|---|---|
| Catalog and sizing | The Enterprise spec maps the selected template ID to an immutable build. The builder defaults to 2 vCPUs and 2 GiB. | Use an allowlisted catalog with multiple templates if workloads need different sizes. Verify placement and image compatibility. |
| Guest startup | The template snapshots a dormant Agent Server with the installer-generated bootstrap key. Enterprise uses `/api/init` to issue a session-specific key, workspace settings and event callbacks. | A boot-time supervisor can replace the dormant snapshot if it preserves isolated per-sandbox credentials and readiness. |
| Ownership and lifecycle | Enterprise uses its shared managed-sandbox ownership records and E2B pause and connect operations. The pilot enables provider pause on lease expiry. | Validate a replacement sandbox separately if your provider can only retain files. Define elapsed time and what lives past lease expiry. |
| HTTP and WebSocket | E2B-issued HTTPS port addresses carry Agent Server traffic and workspace previews. Agent Server authenticates its API and socket. | Front preview traffic with a dedicated authenticated origin if users serve private content. Verify the provider's original port exposure as well as the proxy. |
| History | Guest callbacks send events to Enterprise. Its persisted history remains readable while the sandbox is paused. | Use a different private callback transport if it preserves session authentication and retries. Verify event delivery across resume. |
| Cleanup | Delete through Enterprise and verify E2B termination for the bound sandbox ID. The template has an independent lifecycle. | Retain a bounded template rotation policy and reconcile ambiguous create and delete responses. |
