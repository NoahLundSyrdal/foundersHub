# WhatsApp approval and team access

The existing button/voice transcript and deterministic request paths feed the same Python workflow. A new purchase proposal can send one WhatsApp message with quantity, unit prices, total and a private confirmation link. The recipient is a fixed, verified account configured by the operator; the agent cannot choose it. Normal dashboard approval remains the default when the two settings below are empty.

Oda approval adds the exact reviewed products to a real cart. Delivery fees, delivery time and payment are completed on Oda. Other supplier execution remains explicitly simulated. A cart is not a paid order.

## Local setup

Connect Oda from the local dashboard first. Set these in the ignored root `.env`, then restart the API:

```dotenv
OFFICE_ORDER_WHATSAPP_TO=+<verified E.164 number>
OFFICE_ORDER_PUBLIC_URL=https://<your private demo host>
```

Pair the optional local worker as described in [its setup guide](../apps/whatsapp/README.md). Reuse an existing paired session; run only one worker. This uses an unofficial WhatsApp client and requires the laptop to stay running.

```sh
npm --prefix apps/whatsapp ci
OFFICE_WHATSAPP_API_URL=http://127.0.0.1:3001 npm --prefix apps/whatsapp start
```

The worker and API must share the same private `.data/whatsapp/bridge-token`. An isolated worktree can set `OFFICE_WHATSAPP_BRIDGE_TOKEN_PATH` to the existing token's absolute path and run the worker from the checkout holding the paired session. Never copy session material into Git. Health reports `approval_channel`; local `/api/whatsapp/status` reports bridge connectivity and queue counts without recipients or message bodies.

## Invite-protected team demo

Build the frontend. In separate terminals, run the API on port 3011 and the gateway on port 8086:

```sh
npm run build
uv run --project apps/agent uvicorn office.api:create_app --factory --app-dir apps/agent --host 127.0.0.1 --port 3011 --timeout-graceful-shutdown 2
uv run --project apps/agent uvicorn office.team_gateway:create_gateway --factory --app-dir apps/agent --host 127.0.0.1 --port 8086
```

The gateway targets `http://127.0.0.1:3011` by default; launch the WhatsApp worker with `OFFICE_WHATSAPP_API_URL=http://127.0.0.1:3011` too. Expose only port 8086 through an HTTPS tunnel. A temporary Cloudflare tunnel can be started with `cloudflared tunnel --url http://127.0.0.1:8086 --no-autoupdate`; for this tunnel set `OFFICE_GATEWAY_DISABLE_SSE=true` in the gateway shell so the dashboard falls back to polling. Update `OFFICE_ORDER_PUBLIC_URL` to its actual HTTPS origin and restart the API before creating a fresh request.

The gateway creates a private `.data/team-gateway/invite-token` file. Give teammates `https://<host>/join/<file contents>` through a private channel. It sets a Secure, HttpOnly cookie and redirects to a clean URL. This invite grants dashboard access and request submission using the operator's connected Oda account; keep it private. OAuth management, ordinary approval routes and WhatsApp bridge routes remain inaccessible through the gateway. Rotate the invite file and restart the gateway to revoke access. Temporary tunnels and approval links stop working when their processes stop; this is not permanent hosting.

## Approval behavior

- New proposals are queued once per exact snapshot; historical requests are not messaged on startup. An unclaimed message expires after two minutes and is not resent automatically.
- `GET`/`HEAD /confirm/<token>` only displays the exact offer. WhatsApp link previews cannot approve anything.
- `POST /confirm/<token>` approves; `POST /confirm/<token>/reject` declines. Links expire within 15 minutes or when the quote expires, whichever comes first. Changed/stale proposals are rejected.
- Approval is consumed before supplier execution. Repeated clicks and uncertain outcomes never repeat execution. Inspect an uncertain supplier result before taking any further action.
- Tokens are redacted from access logs. Runtime databases, message bodies, session credentials and invite files stay in ignored private storage.

The test suites cover snapshot binding, expiry, read-only previews, duplicate clicks, uncertain delivery/execution and gateway access boundaries. Provider delivery and physical hardware input require live verification; no firmware changes are part of this feature.
