# Founders Hub · Office assistant

A local hackathon foundation for an office assistant that receives requests from physical stations, turns them into purchase proposals or maintenance tickets, and tracks the outcome.

**Default mode is simulated.** No payment, supplier order, plumber booking, or message to a third party is performed. Catalogue prices and delivery estimates are fixtures. Speaker identification is not implemented.

## Start locally

Requirements: Node.js **22.13 or newer**, npm, and Python **3.10 or newer**. The backend uses Node's built-in SQLite support; Node may print an experimental-feature warning.

```sh
npm install
cp .env.example .env
npm run dev
```

Open **http://localhost:5173**. The backend listens on **http://127.0.0.1:3001**. The web app proxies `/api` to the backend. This foundation is intended for local development, without production authentication; keep it bound to loopback.

No API key is needed in the default mock mode. `AGENT_PROVIDER=anthropic` is the optional Claude interpretation adapter: configure `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL` in the ignored `.env` file. The example model is `claude-sonnet-5-5`; change it if your API account uses a different supported model. Supplier execution remains simulated regardless of the interpretation provider.

## Try the flow

1. Choose the kitchen station and press its simulated button.
2. Enter `We need coffee` and submit the transcript.
3. Inspect the product, quantity, total, and simulated delivery estimate.
4. Approve that exact proposal. The request receives a **simulated** order confirmation.
5. Repeat the request to see duplicate handling, or report `The coffee machine is leaking` to create a maintenance ticket.

The dashboard includes a low-stock simulation. A sensor event enters the same request workflow without requiring speech. Browser voice input depends on browser support and microphone permission; typed transcripts are always available.

You can also simulate a device from another terminal while the backend runs:

```sh
npm run device:simulate
```

See [the demo guide](docs/demo.md) for the full sequence and [hardware notes](docs/hardware.md) before connecting a real board.

## Repository

```text
apps/web/                 React dashboard, browser voice input, approvals
apps/server/              API, request workflow, Claude adapter, SQLite
apps/device/              Python USB bridge, simulator, device tests
packages/contracts/       Shared TypeScript schemas and generated JSON Schema
config/stations.json      Station locations, equipment, teams, catalogue scope
config/office.json        Office preferences and spending policy
fixtures/products.json    Explicitly simulated catalogue
```

The backend owns persistent state and purchase execution. The web app and device bridge call its API; neither writes the database directly.

## Team ownership

| Owner | Area | First integration milestone |
| --- | --- | --- |
| 1 · Hardware | `apps/device/`, actual port mapping | Button event reaches API; feedback reaches board |
| 2 · Agent and speech | `apps/server/src/agent/`, speech adapter | Transcript plus station produces validated intent |
| 3 · Procurement | `apps/server/src/procurement/`, supplier adapters | Approved proposal produces a tracked execution result |
| 4 · Dashboard and integration | `apps/web/`, core API/database | Full request → approval → confirmation flow |

Agree contract changes in `packages/contracts/` before changing callers. Use short-lived branches and integrate early. Keep one integration owner; avoid editing another owner's files without coordinating.

## Commands

| Command | Purpose |
| --- | --- |
| `npm run dev` | API and dashboard together |
| `npm run dev:server` | API only |
| `npm run dev:web` | Dashboard only |
| `npm run check` | Type-check workspaces |
| `npm run build` | Build dashboard assets |
| `npm test` | Backend tests and Python device tests |
| `npm run contracts:export` | Regenerate device JSON Schema from shared contracts |
| `npm run device:simulate` | Submit a sample kitchen request to a running API |

Runtime database files live in ignored `.data/` by default. Stop the backend before removing its database to reset local demo data. Never commit `.env`, recordings, credentials, or runtime databases.

## Technology choices

The foundation uses current React/TypeScript tooling, Fastify, schema-validated contracts, and a single SQLite owner so the four developers can integrate quickly. The live Claude path uses the official Anthropic SDK with bounded tool use and streamed progress; the mock path remains available for offline development and deterministic tests. These choices make the system inspectable and extensible, rather than asserting that a framework or model alone makes it state of the art.

The important technical boundaries are explicit tools, validated decisions, approval tied to an exact proposal, persisted execution attempts, and idempotent input. Real-time speech, speaker identity, supplier search, and checkout remain separate integrations. See [the architecture rationale](docs/architecture.md#technology-rationale).

## What to build next

This foundation uses a fixed approved catalogue, not live search or a quality/price/delivery ranking engine. Real purchasing needs a supplier adapter, confirmed delivery terms, authenticated approval, credentials, and reconciliation of uncertain execution results. Maintenance tickets are local records, not actual contractor bookings.

See [architecture and API contracts](docs/architecture.md), [demo steps](docs/demo.md), and [hardware setup](docs/hardware.md). MCP can expose the same backend capabilities later; it is not required to connect the first three apps.
