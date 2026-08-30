## v5.6.2

- Fixed Web Panel HTML/CSS rendering so stylesheet content is not shown as page text.
- Fixed admin customization save flow.
- Kept admin controls isolated behind the private admin path/session.

## v5.6.1

- Unified project version across CLI, Web Panel, updater metadata and documentation.
- Keeps the existing PasarGuard Node traffic/statistics integration and admin-path functionality intact.
- Web Panel release identifier is now `5.6.1`.

## v5.5.4-node-traffic-fix3

- Reworked PasarGuard Node traffic collection against the current Node API contract.
- Uses the canonical `NodeService.GetStats` gRPC method with `UsersStat` and `reset=false`.
- Uses `Authorization: Bearer <node_api_key>` for current Node authentication and keeps `x-api-key` as compatibility fallback.
- Uses the Node service `port` (not `api_port`) for traffic statistics.
- REST `/stats/` protobuf transport remains as a fallback.
- Accepts newer/older node field shapes for address, port, API key, and CA certificate.
- Keeps the UI unchanged and accumulates Node counters across Xray/Panel resets.
- Records the last Node connection error internally instead of silently swallowing every failure.
