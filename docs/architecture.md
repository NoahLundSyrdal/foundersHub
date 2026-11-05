# Architecture

The physical station supplies context. The backend interprets the need, applies office policy, records a proposal or ticket, and tracks completion. One server process owns SQLite; this is a local development architecture, not a distributed deployment.

```text
Atech board → Python USB bridge ─┐
Browser buttons / transcript ───┼→ API → Request workflow → SQLite
Low-stock sensor event ─────────┘              │
                                  Interpretation / catalogue
                                               │
                                 Proposal → Approval → Supplier adapter
                                               │
                                    Dashboard + device feedback
```

## Runtime boundaries

- **Web:** React UI, simulated stations, typed or browser-generated transcripts, proposal review and approval. It subscribes to server-sent events for immediate updates and keeps polling as a recovery path; it does not make supplier calls or store authoritative workflow state.
- **Server:** Station configuration, schema validation, request history, duplicate checks, interpretation, catalogue matching, policy, approvals, supplier execution, maintenance assignment, and SQLite.
- **Device:** Python normalizes USB events and sends them to the API. It can run without a board in simulator mode. It forwards returned display/light commands to firmware that implements the matching action handler.
- **Contracts:** Shared TypeScript schemas and types. The JSON Schema export is for non-TypeScript clients; regenerate it rather than independently editing it.

## Inputs and lifecycle

A `button_pressed` event opens an `awaiting_input` request. Its station supplies location, known equipment, approved item IDs, and responsible team. A transcript is submitted to that request. A `speech_transcript` event can also include the text directly.

A `stock_low` event includes an approved `item_id`; it enters purchase preparation without speech. A distance sensor must be calibrated for a specific container or stack. A reading is not general visual recognition of a cupboard.

Typical request outcomes:

```text
awaiting_input → processing → awaiting_approval → ordered
              → needs_clarification → new transcript
              → maintenance_open → resolved
              → failed
ordered → resolved (delivery confirmed)
```

Purchase proposals contain the product snapshot, quantity, total, currency, and simulation marker. Maintenance is a local assigned ticket and has no purchase proposal. The `/resolve` endpoint can also record delivery for an ordered purchase. `user_id` is nullable and remains unknown in the foundation.

## API surface

| Method and route | Input | Result |
| --- | --- | --- |
| `GET /api/health` | — | Provider and simulation status |
| `GET /api/events` | — | Server-sent operational progress; refresh durable state after reconnect |
| `GET /api/stations` | — | Station profiles |
| `GET /api/requests` | — | Current requests |
| `GET /api/devices/:station/command` | — | Latest feedback command for a station |
| `POST /api/device-events` | `DeviceEvent` | Request, duplicate flag, device command |
| `POST /api/requests/:id/transcript` | `{ "text": "We need coffee" }` | Request, duplicate flag, device command |
| `POST /api/requests/:id/approve` | `{ "proposal_fingerprint": "..." }` | Updated request |
| `POST /api/requests/:id/resolve` | `{}` | Resolved maintenance or delivered purchase request |

Errors use an `error` string and a non-success HTTP status. Exact types and validation live in `packages/contracts/src/index.ts`.

`GET /api/events` emits named `requests.updated` events with JSON data `{ "request_id": "..." }`. The client then refreshes authoritative request state. It is a notification stream, not replayable event storage. Each request can include a `trace` array of `{ stage, detail, at }` entries; those record operational progress and tool activity, not private model reasoning. The web client reconnects with EventSource and retains polling as a fallback.

Example canonical device event:

```json
{
  "event_id": "unique-stable-event-id",
  "station_id": "bathroom",
  "type": "stock_low",
  "timestamp": "2026-11-05T14:00:00.000Z",
  "payload": { "item_id": "toilet-paper", "level": 15 }
}
```

## Approval and duplicate handling

There are two distinct duplicate boundaries. Repeating an event ID retries the same input; it must not create a second request. A fresh event can still refer to an equivalent active purchase in `awaiting_approval` or `ordered` state, or a failed purchase with a retained proposal/uncertain execution. It returns the existing request instead of ordering again. Resolve an ordered request once delivery is confirmed; a later replenishment request can then create a new proposal.

The approval body is derived with `proposalFingerprint(proposal)`. It represents the complete displayed proposal snapshot. The backend compares it against persisted state before execution, then applies the spending cap. It must not accept a new client-supplied price or silently substitute a different product.

Supplier execution belongs exclusively to the server. Persist execution state and references. An already executed approval must not issue another purchase, and failed or uncertain execution must not trigger an unexamined automatic retry. Before adding a real supplier, support provider idempotency or explicit reconciliation.

## Providers and extension points

The default interpretation provider is a deterministic mock so all teammates can develop without credentials. The optional Anthropic provider uses the official SDK and a bounded tool loop. Read-only tools provide approved catalogue, office policy, and open-request context; a strict `submit_decision` tool produces the validated interpretation. No tool can spend money or place an order. Enabling the live provider does not enable real purchases.

The fixture catalogue provides approved products, prices, and explicitly unverified demo delivery estimates. Live product search, ranking by price/quality/delivery, supplier checkout, payment, verified delivery, contractor booking, and speaker identification are future work.

Implement those as adapters behind the current workflow rather than embedding external calls in the frontend or device bridge. An MCP server could later expose request creation, proposal review, and status through the same application services.

## Production work not included

The loopback-only foundation has no production user authentication, multi-office isolation, deployment, or real-spending authorization. A real deployment also needs station/device authentication, authenticated approvers, supplier credentials, audit controls, and data retention rules. Browser speech service behavior depends on the chosen browser; do not assume all transcription is processed locally.

## Technology rationale

Use a current model through its official SDK, keep model access behind a provider interface, and make the configured model visible rather than baking model assumptions into workflow code. The mock provider is a development tool, not an alternate claim of live model intelligence. Model access and capabilities must be verified for the team's API account.

Use explicit read-only tools for catalogue, policy, and open-request context. The agent submits a schema-validated decision; it does not receive an unrestricted purchase tool. Bounded iterations and request timeouts keep latency and spend predictable. A future real supplier adapter still belongs behind the application's approval and execution boundary.

Stream operational progress to the dashboard so developers and demo viewers can see tool calls and their outcomes. This is an execution trace, not hidden model reasoning. Persist important workflow outcomes in SQLite; a live stream is not a replacement for durable state. Reconnect or polling recovery should refresh authoritative API state.

The modern stack serves a concrete purpose: React and TypeScript for rapid UI integration, shared schemas for typed and runtime boundaries, Fastify for a testable API, and SQLite for local durability without another infrastructure dependency. Browser speech is a convenient optional input path; a dedicated low-latency speech provider and speaker identification are not included.
