# Ask about conditions and assess a meeting room

Use the same station button and transcript flow. Examples: “What is the temperature?”, “What are the temperature and humidity?”, “How is the air?”, and “Book this meeting room tomorrow at 13:00 for an important meeting.” No second button or firmware change is required for backend routing.

Sensor questions bypass the purchasing workflow and prefer the latest physical canonical readings. Temperature uses °C, humidity uses % RH and CO₂ uses ppm. Readings older than 120 seconds, future samples and missing sensors are unknown; old values are never spoken as current. An explicitly seeded synthetic demo can supply labelled demo values when no live metric is fresh. The answer and its evidence persist so request refreshes show a resolved informational request rather than a purchase or an unanswered question.

Configure the meeting room's sensor explicitly in the private room calendar:

```json
{"rooms":[{"name":"Test room","simulation":true,"board_id":"kitchen"}],"bookings":[],"contacts":[]}
```

`board_id` identifies the physical sensor station, not another button. A named room without a mapping has unknown conditions. The parser receives the current station ID and resolves “this room” only to a uniquely mapped room. A direct question naming a different configured room reads that room's station rather than the requesting button's sensors.

The booking flow checks calendar availability, current conditions and measured history. Short `board.say` / `room.progress` updates describe the actual steps: “Let me check the room sensors” and “I’m comparing readings from similar meeting times.” The final reply includes the current temperature when available and the recommendation. No artificial delay or private reasoning is streamed. The hardware voice adapter must consume those progress events to speak them while the API call runs; emitting an event alone does not confirm playback.

Example with sufficient recorded physical evidence: “It is 29°C now. Consider another room: 3 of 3 qualified prior days were hot at this local meeting time.” These numbers are illustrative. Without recorded history or an explicitly seeded demo, the agent says the same-time history is insufficient.

## Synthetic room demo

With the local backend running, run `npm run demo:room-history`. This posts to `POST /api/demo/room-environment` and seeds 28 prior calendar days of deterministic temperature, humidity and CO₂ samples for the kitchen station. The fixture in `fixtures/room-environment-demo.json` represents a room that is too warm from 08:00–22:00 on four of every five dates. Its separate current demo preset is 29.2°C, 45% RH and 1,300 ppm CO₂. Try “What is the temperature and humidity?” and “Book this meeting room tomorrow at 13:00 for an important meeting.”

The reply explicitly says “Synthetic demo readings” or “Synthetic demo history.” The dataset uses dedicated `room_demo_datasets` / `room_demo_samples` tables, and never enters physical `sensor_readings`, facility checks, purchasing or messaging. Demo values have `state:"demo"`, `basis:"synthetic"`, and `simulation:true`; their timestamp is the seed time, not a fabricated fresh measurement. The physical air-quality endpoint remains unchanged. If any physical metric is fresh, the current snapshot uses physical data only, leaving other missing metrics unknown. Sufficient qualified physical history takes priority over the synthetic history; sources are never combined to establish a pattern.

Repeating the seed on the same local date returns the same dataset. Running it on a later date refreshes only that station's synthetic history. Use `npm run demo:room-history -- --clear` to remove it without deleting physical readings. `--board kitchen` and `--api http://127.0.0.1:3001` select a station/API. The CLI only talks to a loopback backend; the server performs mutations under its existing lock. Equivalent API bodies are `{ "board_id":"kitchen", "action":"seed" }` and `{ "board_id":"kitchen", "action":"clear" }`.

`config/room-environment.json` sets operational comfort bands and evidence requirements. Initial heat, humidity and CO₂ limits are 27°C, 65% RH and 1,200 ppm; these are configurable office settings. A near-term meeting within two hours gets an avoid recommendation when current heat, high humidity or elevated CO₂ exceeds those bands. Current bad conditions alone produce a caution for a later meeting, rather than a claim about its future temperature. A repeated historical heat pattern can recommend another room for the requested slot. Advice does not reserve a room, cancel an existing booking or send anyone a message. Calendar simulation remains explicitly labelled.

History covers 28 days, matching the requested local time window in Europe/Oslo and its weekday/weekend class. Today is excluded. A pattern requires at least three distinct prior days, at least three samples per day, 15 minutes of coverage, and at least half the requested interval represented by time span and populated 15-minute bins. Each bin and qualifying day has equal weight. A usual-heat warning requires at least 60% of qualifying days above the heat threshold. Insufficient or over-dense evidence stays unknown. This is an observation of recorded conditions, not a forecast. Keep each board's canonical temperature stream tied to one consistently placed ambient sensor.

Structured results appear in the normal response's `environment` or `room_workflow.environment`, with current freshness, historical evidence, recommendation and summary. TypeScript and JSON Schema contracts live in `packages/contracts/src/room-environment.ts`. Existing button, speech-transcript, task and room-conversation endpoints remain the entry points.

Software tests cover fresh/stale/missing readings, board mapping, same-button routing, transport retries, historical evidence, local-time/DST matching, and no-purchase/no-message behavior. Actual board microphone capture, voice playback of progress and sensor placement require the connected hardware adapter; these additions do not claim a new physical audio test.
