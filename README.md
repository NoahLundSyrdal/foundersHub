# Founders Hub · Office agent

An office assistant embodied in Atech boards around the office. Press a board's button, say what you need, and the agent does it: it orders supplies, logs broken things, or messages another room. It asks a human only when policy requires it.

**Orders are simulated.** No payment, supplier order or third-party message happens. Catalogue prices and delivery estimates are fixtures.

Plan, architecture and team split: [docs/agent-plan.md](docs/agent-plan.md). Demo script: [docs/demo.md](docs/demo.md).

## Start locally

Requirements: Node.js 22.13+, npm, [uv](https://docs.astral.sh/uv/) (it installs Python 3.12+ itself).

```sh
cp .env.example .env
npm run setup        # npm install + uv sync for apps/agent
npm run dev          # API on 127.0.0.1:3001, dashboard on http://localhost:5173
```

The default `OFFICE_MODEL=mock` is an offline keyword model, so it needs no API key. For the real agent, set `OFFICE_MODEL=anthropic:claude-sonnet-5-5` and `ANTHROPIC_API_KEY` in `.env`.

## Try it

1. On the dashboard, pick a speaker on the Kitchen board (this stands in for voice ID) and send `We need coffee`.
   - As **Leo** (office manager, auto-approves up to 500 NOK), it orders right away, simulated.
   - As **guest**, it asks for approval. Approve it under "Needs approval".
2. Send it again. The agent sees the open order and does not buy twice.
3. Send `The coffee machine is leaking`. This creates a local ticket for the responsible team.
4. Run `npm run device:simulate` in another terminal to send a request through the device bridge.
5. Low stock: `curl -X POST localhost:3001/api/sensor -H 'content-type: application/json' -d '{"board_id":"bathroom","kind":"stock_level","value":12}'`. A reading that crosses the threshold starts the agent without anyone speaking.

## Repository

```text
apps/agent/              Python backend: FastAPI + Pydantic AI + SQLite
  office/agent.py        the agent, task run/pause/resume
  office/api.py          HTTP API (the contract) + device-bridge routes
  office/plugins/        capabilities: one file = toolset + required hardware modules
  office/voice.py        speech-to-text + speaker ID (owner: voice)
  office/mock.py         offline model for tests and no-key dev
apps/web/                React dashboard: boards, approvals, live tool activity
apps/device/             Atech USB bridge and simulator
config/boards.json       boards: location, plugged-in modules, sensor triggers
config/people.json       people → role → auto-approve limit
config/office.json       office spend cap and preferences
fixtures/products.json   simulated catalogue
docs/pydantic-ai/        vendored Pydantic AI docs
docs/atech.md            Atech platform and SDK reference
```

## Commands

| Command | Purpose |
| --- | --- |
| `npm run setup` | Install web and Python dependencies |
| `npm run dev` | API and dashboard together |
| `npm run dev:agent` / `npm run dev:web` | One of them |
| `npm test` | Backend tests (mock model) + device bridge tests |
| `npm run check` / `npm run build` | Type-check / build the dashboard |
| `npm run device:simulate` | Send a sample kitchen request through the bridge |

Runtime data lives in the gitignored `.data/`. Stop the API before deleting it to reset. Never commit `.env`, recordings, voice embeddings or databases.

## API

| Route | Purpose |
| --- | --- |
| `POST /api/input` | `{board_id, text?, audio_b64?, person?}` runs the agent. Returns the task. |
| `POST /api/sensor` | `{board_id, kind, value}` stores a reading and fires board triggers when a threshold is crossed |
| `GET /api/state` | Tasks, orders, tickets, latest board commands |
| `POST /api/tasks/{id}/decide` | `{approve}` resolves a pending approval (409 if stale) |
| `GET /api/events` | SSE: `task.updated`, `tool.called`, `tool.result`, `board.say`, `board.listen` |
| `GET /api/boards` | Boards and people |
| `POST /api/device-events`, `GET /api/devices/{id}/command` | Device bridge (idempotent on `event_id`) |
