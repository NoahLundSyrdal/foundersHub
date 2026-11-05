# Procurement, severity and alerts

Both spoken/typed needs and deterministic sensor triggers use the server-owned workflow. Pydantic AI interprets the need and assesses severity; application code compares offers, enforces policy and owns execution. **Food uses live Oda products and real cart additions after account connection and exact basket approval.** Other supplier quotes and order/service execution remain simulated. Payment and order placement happen on Oda.

## Food through the official Oda MCP

`ODA_FOOD_ENABLED=true` (default) routes food and drink requests, including coffee, to [Oda's official MCP](https://oda.com/mcp?lang=en). Click **Connect Oda** in the dashboard and complete Oda sign-in/consent. No Oda API key is needed. The OAuth public client uses PKCE, scope `mcp`, and the fixed callback `http://127.0.0.1:3001/api/integrations/oda/callback`. Run the documented loopback backend on port 3001. Credentials are saved only in ignored `.data/oda/oauth.json` (file 0600, directory 0700); they are never sent to the model or dashboard.

Example: `Order 2 packs of ground coffee, 500g each, and 1 carton of whole milk, 1 litre`. The structured interpretation extracts food queries and pack counts; Oda `product_search` supplies real IDs, names, prices and availability. One bounded Pydantic AI selection chooses suitable products. Application code validates returned IDs, exact quantities, decimal prices and the office product subtotal cap. Vague `order food` asks what products and quantities are wanted; it does not invent meals or dietary requirements. Delivery fees and slots remain pending until Oda checkout. No ratings, stock or delivery promises are invented.

The dashboard shows the exact basket snapshot for approval. Approval checks its fingerprint, 15-minute expiry, current office cap and fresh Oda stock/prices before a cart update. The server persists an attempt first, reads the existing cart, then adds positive quantity deltas through `manipulate_cart`; unrelated items are preserved. A timeout or restart after dispatch leaves the attempt uncertain. Rechecking reads the cart and reconciles the expected quantities, without resending the addition. Equivalent prepared/uncertain baskets are blocked. A changed price or unavailable product requires fresh review.

The result is `checkout_ready`, **not an order**: click **Review delivery and pay on Oda**. Oda's MCP does not expose checkout/payment. No `orders` row or supplier-order reference is created for cart preparation. Live account authorization/product search/cart changes still require validation against the user's Oda account; automated tests use injected responses and do not touch a real account.

| Endpoint | Role |
| --- | --- |
| `GET /api/integrations/oda` | Safe connection status and connect URL |
| `GET /api/integrations/oda/connect` | Register local OAuth client and redirect to Oda consent |
| `GET /api/integrations/oda/callback` | Single-use PKCE callback; redirect back to dashboard |
| `POST /api/input` | Existing board/text input; grocery workflow appears as optional `grocery` |
| `POST /api/tasks/{id}/decide` | Exact `proposal_fingerprint` required before real cart mutation |
| `POST /api/requests/{id}/approve` | Dashboard compatibility route for the same reviewed snapshot |

Set `ODA_FOOD_ENABLED=false` only for the older fixture food flow. Deterministic sensor replenishment retains its existing fixed catalogue rules; real Oda cart writes require an explicit reviewed food request.

## Run Leo's Python app

The backend reuses `feat/pydantic-agent-backend` (`ee26a911`) under `apps/agent`, with procurement extensions in `office/procurement_workflow.py` and `office/plugins/procurement.py`. The TypeScript frontend remains in `apps/web`. Board/firmware connection code was not replaced.

Requires Python 3.12+ and uv:

```sh
npm install
npm run setup:python
npm run dev
```

`npm run dev:agent` starts FastAPI on loopback port 3001. `OFFICE_MODEL=mock` runs entirely offline unless a search key is supplied. For live interpretation set `OFFICE_MODEL=anthropic:<supported-model>` and `ANTHROPIC_API_KEY`. `TAVILY_API_KEY` optionally enables read-only supplier/FINN discovery; `FINN_DISCOVERY_ENABLED=false` disables FINN-specific search. Live account/model availability was not verified.

The Python runtime uses `AGENT_DATABASE_PATH` (default `.data/agent.sqlite`), separate from the previous TypeScript schema. Pydantic AI runs directly inside the app. Its structured assessment has bounded tool/model usage and a 35-second timeout. The old TypeScript server remains an inactive compatibility reference (`npm run dev:legacy`), not the default application.

The primary connection endpoints are:

| Endpoint | Input / role |
| --- | --- |
| `POST /api/input` | `{board_id,text,person?}`; receives a prompt after the existing board/voice connection. Returns a task augmented with `assessment`, `proposal`, `research`, `approval_required`. |
| `POST /api/sensor` | `{board_id,kind,value}`; Leo's configured deterministic board triggers. |
| `GET /api/state` | Current tasks, simulated orders, local tickets and board commands. |
| `POST /api/tasks/:id/decide` | `{approve,proposal_fingerprint?}`; approves or denies stored exact pending calls. |
| `GET /api/boards` | Existing board profiles and people configuration. |

## Application endpoints

| Endpoint | Purpose |
| --- | --- |
| `POST /api/device-events` | Existing `speech_transcript`, `button_pressed` or `stock_low` events; reuse `event_id` for transport retries. |
| `POST /api/requests/:id/transcript` | Submit `{text}` after a button or clarification. |
| `GET /api/requests` | Durable history, severity, approval requirement, proposal, comparison and discovery evidence. |
| `POST /api/requests/:id/approve` | The application's order endpoint: execute the exact proposal using `{proposal_fingerprint}`. |
| `GET /api/alerts` | Durable urgent/critical dashboard messages and acknowledgement history. |
| `POST /api/alerts/:id/acknowledge` | Mark a message seen; does not approve spending or resolve a hazard. |
| `POST /api/requests/:id/resolve` | Record maintenance resolution or delivered/completed order. |
| `GET /api/events` | Existing `requests.updated` notifications; refresh durable state on reconnect. |

Example input:

```sh
curl http://127.0.0.1:3001/api/device-events \
  -H 'Content-Type: application/json' \
  -d '{"event_id":"coffee-demo-001","station_id":"kitchen","type":"speech_transcript","timestamp":"2026-11-05T14:00:00.000Z","payload":{"text":"We need 2 coffee"}}'
```

Review `request.proposal.sourcing`, compute `proposalFingerprint(request.proposal)` with the shared helper and send that exact string to `/approve`. Clients cannot provide substitute prices or products. Repeating an event returns its request; equivalent active needs merge across new IDs. Service categories are station-scoped `ventilation-service`, `plumbing-service`, and `printer-service`; explicit service requests create priced proposals. Other maintenance remains a local assigned ticket. Add an approved catalogue entry, station scope and equivalent supplier offers before supporting another category; model-generated SKUs cannot execute.

## Severity and approval

The same bounded Pydantic agent call returns `assessment={severity:'routine'|'urgent'|'critical',confidence:0..1,requires_confirmation:boolean,reason:string}`. This is operations triage, not a certified hazard detector; confidence is self-reported, not calibrated. Missing assessments require human review. Known hazard language and configured sensor severity can escalate the agent's result. Urgent/critical requests create durable dashboard alerts.

Edit `config/office.json` and restart:

| Setting | Default | Rule |
| --- | --- | --- |
| `require_approval` | `true` | All proposals require exact approval. |
| `auto_approval_max_nok` | `300` | Higher landed totals require review, even in automatic mode. |
| `auto_approval_min_confidence` | `.85` | Lower confidence requires review. |
| `service_requires_approval` | `true` | Site-visit/service scope requires review. |
| `max_order_nok` | `1000` | Hard limit; human approval cannot bypass it. |

Automatic execution requires `require_approval=false`, an eligible quote within both limits, sufficient confidence and no model-required confirmation. Critical requests always require confirmation. The agent can escalate review, never grant itself spending authority. Current mocks conservatively require confirmation for all service visits. Acknowledging an alert does not change order approval.

## Ranking, sensors and recovery

`fixtures/offers.json` has three synthetic offers per category. Each is for one complete catalogue pack or defined service visit. NOK prices, quality scores and delivery times are fixtures, not verified supplier quotes or reviews. Landed total is `unit_price * quantity + shipping`, shipping once per order, rounded to two decimals. Real adapters must normalize taxes, fees and equivalent units.

First exclude out-of-scope, unavailable, expired, low-quality, slow or over-cap offers. Then score normalized price/quality/delivery weights (defaults .40/.35/.25): price is cheapest eligible landed total / own total; quality is score / 100; delivery is `(fastestHours+1)/(ownHours+1)`. Ties use landed total then stable offer ID. Proposals persist all alternatives, scores, exclusions and an explanation.

Quotes expire after `quote_ttl_seconds` (15 minutes by default). Execution checks the full selected offer snapshot, including supplier, scope, quality, shipping and delivery, against the current source. Changed/expired terms reject approval; create a new request with a fresh quote. Automatic and manual paths share these checks.

Leo's existing `config/boards.json` defines deterministic trigger transitions. This procurement extension consumes their prompts and does not change the board connection. The separate `config/sensor-rules.json` is a future sustained-reading policy example, not wired into the Python board endpoint. This task did not verify physical CO2 readings. Low CO2 alone does not imply a ventilation fault.

Supplier attempts persist before simulated execution. Retained uncertain attempts block equivalent purchases; an uncertain attempt is never automatically retried. Input IDs and exact approval snapshots persist in SQLite. The branch's existing trigger logic and speech input are reused; recordings and speaker claims do not authorize purchases. Critical alerts persist and acknowledgement does not approve spending.

## Existing tools and external execution endpoints

These are researched extension points, **not connected execution adapters**:

| Service | Interface | Requirements/limits |
| --- | --- | --- |
| Amazon Business | `POST /ordering/2022-10-30/orders`; reconciliation `GET /ordering/2022-10-30/orders/{externalId}` | Buyer-side ordering requires business/customer onboarding, OAuth and `AmazonBusinessOrderPlacement`. Stable external ID, product/offer references, shipping/payment attributes and exact price expectations are required. HTTP 200 can contain rejected lines; inspect provider acceptance. Norway is not listed as a marketplace; verify actual account/delivery/currency coverage. |
| Shopify Storefront | `POST https://{shop}.myshopify.com/api/{version}/graphql.json`, `cartCreate`, then `cart.checkoutUrl` | Merchant-specific cart and hosted customer checkout. A cart URL is not a paid/confirmed order. Admin create-order tools are merchant sales records, not general purchasing. |
| FINN | Official partner REST API for search/ad retrieval of partner-owned data | Requires business agreement/key. No documented public buyer checkout found. This worker uses Tavily `include_domains:["finn.no"]`, validates result domains, and marks `marketplace:'finn',executable:false`. Verify seller, condition, availability, shipping/collection and total before transacting. No seller contact, internal API or scraping implementation. |
| WhatsApp Business / Composio | Discovered `WHATSAPP_SEND_TEMPLATE_MESSAGE`, `WHATSAPP_GET_MESSAGE_TEMPLATES`, `WHATSAPP_GET_TEMPLATE_STATUS` | WhatsApp is not connected. Requires resolved recipient, business phone ID and approved utility template/language. No connection or send was attempted. Dashboard alerts work without it. |
| WhatsApp / Twilio | `POST https://api.twilio.com/2010-04-01/Accounts/{AccountSid}/Messages.json` | Requires credentials, sender/recipient setup and template handling. Not implemented/enabled. |
| DataForSEO / Composio | Discovered Amazon seller/product research tools | Market data, not purchasing. Paid research calls were not executed or integrated. |

Search evidence never supplies executable prices, availability or contractor bookings. A future real adapter must obtain a structured quote and support execution plus lookup by persisted execution ID; never automatically retry an uncertain write. Keep execution outside model tools. Expose only explicitly selected read-only MCP quote/catalogue tools rather than arbitrary commerce toolsets.

Primary sources: [Pydantic output](https://pydantic.dev/docs/ai/core-concepts/output/), [usage limits](https://pydantic.dev/docs/ai/core-concepts/agent/#usage-limits), [Tavily Search](https://docs.tavily.com/documentation/api-reference/endpoint/search), [Amazon ordering](https://docs.business.amazon.com/docs/ordering-api), [Amazon placement prerequisites](https://docs.business.amazon.com/docs/placing-an-order), [Shopify carts](https://shopify.dev/docs/storefronts/headless/building-with-the-storefront-api/cart/manage), [FINN API](https://www.finn.no/api/), [FINN help](https://hjelpesenter.finn.no/hc/no/articles/22629030477202-API), [Twilio WhatsApp](https://www.twilio.com/docs/whatsapp/quickstart).

## Verification

Run `npm run check`, `npm test`, `npm run test:procurement` and `npm run build`. Python API tests cover input/button → structured assessment → ranked proposal → exact approval → one simulated order, repair/service scope, critical alerts, duplicate inputs, concurrent approvals, rejected stale/delivery snapshots and one-unit sensor replenishment. Worker tests cover Pydantic TestModel and mocked Tavily/FINN discovery. Live Anthropic/Tavily, WhatsApp delivery, actual purchasing, seller availability and hardware calibration remain unverified.
