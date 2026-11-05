# 5-hour plan: the agent core

Goal: talk to a board → the agent works out who you are and what you want → it does the task, or asks one person to approve → the board answers out loud or on its screen. Everything else is extra.

Pydantic AI reference: `docs/pydantic-ai/`. The pages we use: `agent.md`, `tools.md`, `toolsets.md`, `deferred-tools.md`, `dependencies.md`, `message-history.md`, `testing.md`, `models/anthropic.md`, `harness/browser-use.md`, `harness/memory.md`.

## Decision: one Python backend

Replace the TypeScript `apps/server` with a single Python service, `apps/agent/`, built on FastAPI, Pydantic AI and `sqlite3`.

- Pydantic AI is Python, and the device bridge is already Python.
- Keeping the TypeScript server means two services and two copies of the contracts. We don't have time for that.
- `apps/web` stays and gets pointed at the new API. `apps/device/src/bridge.py` stays and just changes URLs.
- `packages/contracts` is frozen. The Pydantic models in `apps/agent` are now the contract.

`apps/agent` requires Python ≥3.12 via `uv` (tested on 3.14). `resemblyzer` needs `setuptools<81` for `webrtcvad`, which is already in the `voice` extra.

**Status (built):** `apps/agent` with the agent, the 4 plugins, approval pause/resume, sensor triggers, SSE, the device-bridge routes, the mock model and 9 passing tests. `apps/web` is a minimal dashboard. `apps/server` and `packages/contracts` are deleted.

## Architecture

```text
Board (button/mic/screen/light/sensors) ─┐
Laptop mic per station (push-to-talk) ───┼→ POST /api/input ─→ office_agent.run(...)
Sensor thresholds ───────────────────────┘                         │
                         deps = {board, person+role, db}            │ toolsets chosen per board
                                                                    ▼
                            str reply ──→ TTS / screen / light   DeferredToolRequests
                                                                    │ (needs approval)
                                         dashboard / button ──→ POST /api/tasks/{id}/decide
                                                                    │
                                              agent.run(message_history, deferred_tool_results)
```

### One agent, many plugins

```python
# apps/agent/core.py  (sketch)
@dataclass
class Deps:
    board: Board          # id, location, modules: set[str]
    person: Person        # name, role, auto_approve_nok; "guest" if voice is unknown
    task_id: str
    db: sqlite3.Connection

office_agent = Agent(
    'anthropic:claude-sonnet-5-5',
    deps_type=Deps,
    output_type=[str, DeferredToolRequests],   # str = what the board says back
    instructions=BASE_PROMPT,                  # short, action-first, Instinct-style: do it, confirm in 1 sentence
)

@office_agent.instructions
def who_and_where(ctx: RunContext[Deps]) -> str:
    return f"Speaker: {ctx.deps.person.name} ({ctx.deps.person.role}). Board: {ctx.deps.board.location}. Time: {now()}."
```

### Plugins

A plugin is a single file in `apps/agent/plugins/`:

```python
# plugins/procurement.py
requires: set[str] = set()                # hardware modules the board must have; empty = any board
toolset = FunctionToolset()

@toolset.tool
def search_catalogue(ctx: RunContext[Deps], query: str) -> list[Product]: ...

@toolset.tool
def place_order(ctx: RunContext[Deps], product_id: str, quantity: int) -> str:
    total = price(product_id) * quantity
    if total > ctx.deps.person.auto_approve_nok and not ctx.tool_call_approved:
        raise ApprovalRequired(metadata={'total_nok': total, 'product': product_id})
    return execute_order(ctx.deps.task_id, ctx.tool_call_id, product_id, quantity)  # unique(tool_call_id) = no duplicate buys
```

`plugins/__init__.py` holds a plain list, `PLUGINS = [procurement, office, hardware, sensors]`. On each run:

```python
toolsets = [p.toolset for p in PLUGINS if p.requires <= board.modules]
result = await office_agent.run(text, deps=deps, toolsets=toolsets, message_history=history,
                                event_stream_handler=push_to_sse)
```

**Composing physical boards:** `config/boards.json` lists each board with its id, location and the modules plugged in, for example `["button","screen","light_grid","mic","distance"]`. When you add a speaker to a board, the `speak` tool turns on for that board. That is the plugin story for the demo. There are no classes, no registry and no entry points.

Starter plugins:

| Plugin | Tools | Notes |
|---|---|---|
| `procurement` | `search_catalogue`, `place_order` | Uses the fixture catalogue first. Simulated execution, clearly labelled. |
| `office` | `list_open_tasks`, `create_ticket`, `notify(person, msg)` | `notify` writes to the dashboard. Slack is a stretch goal and needs explicit OK before it sends anything. |
| `hardware` | `show_on_screen`, `set_light`, `speak` | Each needs the matching module. Writes a command row that the bridge polls. |
| `sensors` | `read_sensor(kind)` | Reads the latest stored readings for temperature, humidity and distance. |
| `memory` *(stretch)* | harness `Memory(FileStore('.data/memory'))` | Lets the agent remember office facts like "Leo drinks oat milk". |
| `web_shop` *(stretch)* | harness `BrowserUse(allowed_domains=[one shop])` | Fills a real cart and **stops before payment**. Reports the cart total. |

### Approval (human-in-the-loop)

This uses Pydantic AI's built-in pause and resume flow. See `deferred-tools.md`.

1. The run ends with a `DeferredToolRequests` output. We store the message history as JSON (`ModelMessagesTypeAdapter`) and the pending tool calls on the task row. The status becomes `awaiting_approval`.
2. The dashboard shows the stored tool-call args: product, quantity and total. A board button can approve too.
3. `POST /api/tasks/{id}/decide {approve: bool}` builds `DeferredToolResults` from the **stored** call ids, then calls `agent.run(message_history=stored, deferred_tool_results=...)`. The client never sends args, so an approval always applies to exactly what was shown.
4. Duplicate purchases are blocked by `UNIQUE(tool_call_id)` on `orders`, plus a check for any open order of the same product.

### Atech boards (see `docs/atech.md`, `atech guide`)

- Use the official `atech` SDK. Firmware template: `apps/device/firmware/office_station/project.yaml` (ports TODO). Bridge: `apps/device/src/atech_bridge.py`.
- Built-in actions we use: `led_<station>_fill`. Built-in events: `btn_<station>` (1 = press), `aht20_temperature`, `aht20_humidity`, `distance_sensor_min_distance`, `microphone_level`. The screen has no built-in action, so the template adds a custom `office_say`.
- **Speaking on the board:** the speaker has a built-in `speaker_play_pcm` action (base64 16 kHz mono PCM, ≤3000 chars per call). The pipeline is TTS → resample → chunks → `board.call`. Stretch goal for hardware + voice.
- **Listening on the board:** the mic only streams a level by default. Real speech would need custom firmware sending base64 audio chunks as events (records ~3 s into heap). Stretch goal; use the laptop mic until then.
- **Boards in other rooms:** over WiFi the board talks to `wss://gateway.atech.dev/ws/live/<project id>` with no laptop needed. The SDK has no WiFi client, so that would be a small websocket loop in the bridge. Stretch goal.

### Voice and roles

- **Trigger:** the hardware owner wires a board button as push-to-talk. Audio comes from the **laptop mic** for each station. Streaming from the Atech I2S mic needs unverified firmware, so it's a stretch goal.
- **Speech-to-text:** OpenAI `gpt-4o-transcribe`, or `faster-whisper` locally if there's no key. It's one function: `transcribe(wav) -> str`.
- **Speaker ID:** enroll each of the 4 of us with about 20 seconds of speech. Embed with `resemblyzer`, or SpeechBrain ECAPA if that fails to install. Match by cosine similarity with a threshold. No match means `guest`.
- **Roles:** `config/people.json` gives each person a `{name, role, auto_approve_nok}`. Role is passed in through deps and instructions, and it changes behaviour, e.g. office manager orders under 500 NOK go straight through and guest orders always need approval.
- **Reply:** OpenAI TTS played on the laptop speaker. Playing it on the Atech speaker is a stretch goal.
- Skip Pydantic AI realtime (speech-to-speech) for now. It's impressive, but it's a different transport and it needs approval resolved inline. Revisit it after the core works.

**Conflict with AGENTS.md:** it says "Voice recognition must not grant purchasing authority". Voice-based roles go against that. Suggested compromise: a voice role can only auto-approve **simulated** orders. Any real checkout always needs a dashboard approval. Update AGENTS.md once we agree.

### Proactive triggers (the "omniscient" part)

`POST /api/sensor {board_id, kind, value}` stores the reading. When it crosses a threshold (with hysteresis), it starts the **same** agent run with a synthetic prompt, for example: `"[sensor] Bathroom paper holder at 12%."` There is no second pipeline.

## API (the only contract)

| Route | Body → result |
|---|---|
| `POST /api/input` | `{board_id, text?, audio_b64?}` → `{task_id, status, reply, person}` |
| `POST /api/sensor` | `{board_id, kind, value}` → `{task_id?}` |
| `GET /api/tasks` | all tasks with pending approvals and orders |
| `POST /api/tasks/{id}/decide` | `{approve: bool, note?}` → task |
| `GET /api/events` | SSE: `task.updated`, `tool.called`, `tool.result` for the live activity feed |
| `GET /api/boards/{id}/command` | latest `{text, color, speak?}` for the device bridge to poll |

SQLite tables: `tasks`, `orders`, `sensor_readings`, `board_commands`. Every row is created with a plain `CREATE TABLE IF NOT EXISTS` at startup. No ORM and no migrations.

## Who does what

| Person | Owns | First milestone (≤ 1h30) |
|---|---|---|
| **A · Agent core** | `apps/agent/core.py`, `api.py`, DB, approval resume | `curl /api/input "we need coffee"` → approval → simulated order. Push the skeleton by **0:30** with `TestModel` so everyone else can build against it. |
| **B · Voice** | `apps/agent/voice.py`, enrollment script, `people.json` | `transcribe()` and `identify(wav) -> Person` working on recorded clips of all 4 of us |
| **C · Hardware** | `apps/device`, firmware, `boards.json` | Button press → `/api/input` trigger. Command poll → screen text and light colour on the real board. |
| **D · Plugins + dashboard** | `plugins/*.py`, `apps/web` | Dashboard shows the task feed, live tool calls and approve/deny buttons. Then the `web_shop` stretch goal. |

## Timeline

| Time | What |
|---|---|
| 0:00–0:30 | A scaffolds `apps/agent` (uv, FastAPI, agent, tables, routes returning stubs) and pushes. Others start their own pieces. |
| 0:30–2:00 | Everyone works in parallel against the stub API. A swaps `TestModel` for Claude. |
| **2:00** | **First end-to-end run:** a spoken request at the kitchen board → recognised person → order → approval → screen and voice reply. Fix whatever breaks. |
| 2:30–4:00 | Second board with different modules to show plugins composing, sensor-triggered tasks, roles changing behaviour, browser-use cart (stretch). |
| **4:00** | **Feature freeze.** Only fixes from here. Write the demo script. |
| 4:00–5:00 | Rehearse twice. Keep a typed-text fallback for every voice step. |

## Deliberately cut

These are out of scope: auth, multi-office, durable execution, evals, migrations, real payment, the TypeScript server and contracts package, Atech mic audio streaming, realtime speech-to-speech, and multi-agent delegation. Tests are a handful of `TestModel`/`FunctionModel` checks for the 3 things that would embarrass us in a demo: duplicate order, stale or changed approval, and over-limit order without approval.

## Honesty rules that still apply

- Orders are **simulated** unless someone explicitly enables a real shop. The UI says "SIMULATED" on every order.
- Browser-use stops before payment.
- Nothing is sent to Slack or email without an explicit OK.
- Keys go in `.env` (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`), and `.env.example` only gets placeholders.
