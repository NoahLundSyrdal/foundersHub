# Working on the office assistant

## Ownership

- Keep one integration owner. Hardware owns `apps/device`; agent/speech owns interpretation; procurement owns catalogue and supplier execution; dashboard/integration owns `apps/web` and core API/database.
- Coordinate changes to `packages/contracts` before changing callers. It is the shared source of truth; regenerate JSON Schema with `npm run contracts:export` after relevant schema changes.
- Keep changes bounded to the requested feature. Avoid introducing extra services or frameworks without a concrete requirement.

## Architecture boundaries

- The server owns SQLite, request lifecycle, approvals, and execution. Devices and the browser use the API.
- Claude interprets requests and proposes actions. Application code validates catalogue scope, prices, spending rules, and approval before execution.
- Use the official Anthropic SDK for the live provider. Keep tool schemas strict, tool loops bounded, and tools read-only except for submitting a validated decision. Do not expose purchase execution directly to the model.
- Stream concise operational progress, not private model reasoning or secrets. Keep durable workflow state authoritative; reconnecting clients must refresh it.
- Preserve event idempotency and purchase duplicate checks. Retrying a transport request must not create another purchase.
- Approval must bind to the exact product/quantity/price snapshot shown. Reject a stale or changed proposal.
- Never automatically retry an uncertain real supplier execution. Reconcile with the supplier using a persisted execution identifier first.
- Keep user identity nullable. Voice recognition must not grant purchasing authority.

## Local development and demo honesty

- Default to mock interpretation and simulated supplier execution. Clearly label simulation, fixture delivery times, and simulated references throughout UI and docs.
- Do not claim supplier orders or contractor bookings occurred without an actual confirmed provider result.
- Do not push, publish, deploy, send third-party messages, or enable real spending without user authorization. Current work is local-only.
- Do not invent Atech port mappings or claim firmware is verified. Confirm the actual board, module inventory, and connected ports before building/uploading firmware.
- Keep credentials, recordings, runtime databases, and personal data out of Git. Add only placeholders to `.env.example`.
- Keep local development on loopback. Production authentication and deployment need separate work.

## Verification

Run checks appropriate to the changed boundary: `npm run check`, `npm run build` for web changes, `npm test` for workflow/device changes. Focus tests on meaningful behavior: duplicate events, duplicate purchases, stale approvals, spending limits, failed execution handling, and device normalization. Reuse passing checks until relevant inputs change.

Update setup instructions when scripts or environment variables change. State honestly which paths were tested, simulated, or left for hardware validation.
