# Founders Hub · Office assistant

A local hackathon foundation for an office assistant that receives requests from physical stations, turns them into purchase proposals or maintenance tickets, and tracks the outcome.

**Food uses Oda after account connection.** Review a live priced basket, approve adding it to your real cart, then complete delivery and payment on Oda. Other supplier execution and catalogue delivery estimates remain simulated.

## Start locally

Requirements: Node.js **22.13 or newer**, npm, **Python 3.12+** and **uv**. The application backend is Python (FastAPI/Pydantic AI/SQLite); the frontend is React/TypeScript.

```sh
npm install
npm run setup:python
cp .env.example .env
npm run dev
```

The API reload command allows two seconds for graceful shutdown so open event streams do not leave a source reload waiting indefinitely.

Open **http://localhost:5173**. The backend listens on **http://127.0.0.1:3001**. The web app proxies `/api` to the backend. This foundation is intended for local development, without production authentication; keep it bound to loopback.

No API key is needed with `OFFICE_MODEL=mock`. For live Pydantic AI, set `OFFICE_MODEL=anthropic:<supported-model>` and `ANTHROPIC_API_KEY` in the ignored `.env` file. Supplier execution remains simulated. Optional `TAVILY_API_KEY` enables supplier and FINN discovery.

The app reuses Leo's `feat/pydantic-agent-backend` with ranked offers, severity assessment, exact approval and critical dashboard alerts. See [the procurement guide](docs/procurement.md) for endpoints and configuration. `AGENT_DATABASE_PATH` defaults to `.data/agent.sqlite`.

See [deterministic facility rules and hardware acceptance steps](docs/facility-rules.md) for backend sensor thresholds, controlled replenishment, local inspection tasks and station sound commands.

The **Work** view adds durable outcomes, owned steps, completion evidence, recurring routines and a local follow-up inbox. Existing orders and maintenance tickets appear there without replacing their approval or execution rules. See [workflow setup and API](docs/office-workflows.md) for the receipt/inventory lifecycle, schedule behavior and isolated-worktree development ports.

## Try the flow

1. Choose the kitchen station and press its simulated button.
2. Enter `We need coffee` and submit the transcript.
3. Inspect the product, quantity, total, and simulated delivery estimate.
4. Approve that exact proposal. The request receives a **simulated** order confirmation.
5. Repeat the request to see duplicate handling, or report `The coffee machine is leaking` to create a maintenance ticket.

The dashboard includes a photo-informed [office twin](docs/digital-twin.md), configurable device placement, and a low-stock simulation. A sensor event enters the same request workflow without requiring speech. Browser voice input depends on browser support and microphone permission; typed transcripts are always available.

You can also simulate a device from another terminal while the backend runs:

```sh
npm run device:simulate
```

See [the demo guide](docs/demo.md) for the full sequence and [hardware notes](docs/hardware.md) before connecting a real board.

## Repository

```text
apps/web/                 React dashboard, browser voice input, approvals
apps/agent/               Python API, Pydantic agent, SQLite, approvals, board integration
apps/procurement/         Pydantic AI worker, severity and supplier/FINN discovery
apps/device/              Python USB bridge, simulator, device tests
packages/contracts/       Shared TypeScript schemas and generated JSON Schema
config/stations.json      Station locations, equipment, teams, catalogue scope
config/office.json        Office preferences and spending policy
config/brain/             Offline copy of the Notion company brain: procedures, approval routing
fixtures/products.json    Explicitly simulated catalogue
```

The backend owns persistent state and purchase execution. The web app and device bridge call its API; neither writes the database directly.

## Team ownership

| Owner | Area | First integration milestone |
| --- | --- | --- |
| 1 · Hardware | `apps/device/`, actual port mapping | Button event reaches API; feedback reaches board |
| 2 · Agent and speech | `apps/agent/office/agent.py`, `apps/agent/office/voice.py` | Transcript plus station produces validated intent |
| 3 · Procurement | `apps/procurement/`, `apps/agent/office/procurement_workflow.py` | Approved proposal produces a tracked execution result |
| 4 · Dashboard and integration | `apps/web/`, core API/database | Full request → approval → confirmation flow |

Agree contract changes in `packages/contracts/` before changing callers. Use short-lived branches and integrate early. Keep one integration owner; avoid editing another owner's files without coordinating.

## Agent communication with Hivemind

We use [Hivemind](https://github.com/Leonhest/hivemind) to share task ownership, contracts, decisions, questions, and handoffs between teammates' coding agents. Each teammate installs it once on their own machine:

```sh
npx hivemind-agents@0.1.14 install
```

The installer configures detected Claude Code, Codex, and Cursor installations globally. To configure only Codex, use `npx hivemind-agents@0.1.14 install --only=codex`. In Codex, run `/hooks` to review and approve the new hooks, then start a new agent session to load the integration. Restart Cursor if using it.

From this checkout, verify setup and read the plan:

```sh
hivemind doctor
hivemind status
hivemind plan
```

Claim a task before implementation. Share interface changes before teammates depend on them, update task status when finished or blocked, and use notebook notes for temporary handoffs. The existing ownership and approval rules still apply.

Hivemind requires Node.js 20+, Git, and an `origin` remote; publishing plan updates requires push access. It synchronizes through hidden `refs/hivemind/<member>` refs on the existing remote, independently of code branches. Plan entries are readable by anyone who can read the repository, including publicly for a public repo. Keep secrets and personal data out of them.

Automatic extraction uses your installed agent's login and subscription or API usage. Set `HIVEMIND_EXTRACTOR_BACKEND=none` in the environment launching your agent to use explicit MCP updates only. `hivemind disable` disables sharing for this clone; `hivemind enable` restores it. See the [upstream guide](https://github.com/Leonhest/hivemind#readme) for details.

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
| `npm run connect -- notion\|sync` | One-time Notion sign-in; re-sync the company brain |

Runtime database files live in ignored `.data/` by default. Stop the backend before removing its database to reset local demo data. Never commit `.env`, recordings, credentials, or runtime databases.

## Technology choices

The application uses a Python FastAPI/Pydantic AI backend with one SQLite owner and a React/TypeScript frontend. Three.js renders the office twin. The live Anthropic path has bounded model/tool usage; deterministic mock interpretation remains available without credentials.

The important technical boundaries are validated decisions, exact proposal approval, persisted execution attempts, and idempotent input. Optional supplier/FINN discovery is read-only. Live stock telemetry, speaker identity, supplier checkout and contractor booking remain separate integrations. See [the architecture rationale](docs/architecture.md#technology-rationale).

## What to build next

The application ranks approved fixture offers by landed price, quality and delivery; those inputs remain simulated. Real purchasing needs a supplier adapter, confirmed delivery terms, authenticated approval, credentials and reconciliation of uncertain execution results. Maintenance tickets are local records; service proposals do not book real contractors.

See [architecture and API contracts](docs/architecture.md), [demo steps](docs/demo.md), and [hardware setup](docs/hardware.md). MCP can expose the same backend capabilities later; it is not required to connect the first three apps.

## Company brain on Notion

Notion holds the office's procedures and an **approval routing** table: which person approves which kind of approval. Application code reads the table and attaches the approver to every proposal; the model never chooses who approves. The dashboard shows it above the approve buttons ("For Leo · Above the office cap of 1000 NOK per order").

| Approval type | When (decided by the server) |
| --- | --- |
| `purchase` | A simulated order that office policy sends to a human |
| `purchase_over_cap` | Any order above `max_order_nok`. It is no longer refused: if no offer fits under the cap, the best one goes to this approver, and it always needs a human |
| `contractor` | A service visit (plumbing, ventilation, printer repair) |
| `default` | Fallback when a type has no row |

Offline and in mock mode the brain is the checked-in copy in `config/brain/`. With a live model, connect Notion once and point it at the brain's root page:

```sh
npm run connect -- notion   # browser sign-in to Notion's hosted MCP; token in ignored .data/oauth/
```

Set `NOTION_BRAIN_PAGE_ID` to a page whose child pages hold the procedures and a table with `Approval type | Approver` (same shape as `config/brain/approval-routing.md`, with at least `purchase` and `purchase_over_cap`). `npm run connect -- sync` or `POST /api/brain/sync` re-reads it into `.data/brain/`; a sync without a valid routing table is refused and the previous snapshot stays. `GET /api/brain` shows what is in use. The agent also gets Notion's read-only tools; if Notion is down, a run continues without them. It keeps an office notebook across tasks (Pydantic AI Harness `Memory`, `.data/memory.sqlite`, live mode only).

## Food on Oda

Use **Connect Oda** in the dashboard, finish sign-in/consent, then ask for explicit products and pack quantities, for example: `Order 2 packs of ground coffee, 500g each, and 1 carton of whole milk, 1 litre`. Set `OFFICE_MODEL=anthropic:<supported-model>` and `ANTHROPIC_API_KEY` for live interpretation. Review the basket and click **Add these products to Oda cart**. Delivery fees and the delivery slot are confirmed when you **Review delivery and pay on Oda**. Cart preparation does not place an order.

`ODA_FOOD_ENABLED=true` is the default. Credentials stay in ignored `.data/oda/oauth.json`. Account signup/consent and a real cart test are deferred; automated tests use injected provider responses. See [the procurement guide](docs/procurement.md#food-through-the-official-oda-mcp).
