# Deterministic facility checks

The Python backend receives sensor readings and completed stock checks, evaluates fixed rules, and dispatches the configured actions. It runs with no browser open. Each configured measurement maps to a threshold, required duration, sample count, recovery threshold, and actions. Detection, local ticket creation, stock planning, and stock approval do not call a language model. Configuration is available through the API; this PR contains backend logic and contracts.

| Check | Initial condition | Recovery | Default actions |
| --- | --- | --- | --- |
| Elevated CO₂ | >1,200 ppm for 30 seconds, 3 samples | ≤1,000 ppm | Dashboard alert, local inspection task |
| Room too warm | >27°C for 5 minutes, 3 samples | ≤25°C | Alert, local inspection task |
| Room too cold | <18°C for 5 minutes, 3 samples | ≥20°C | Alert, local inspection task |
| High humidity | >65% RH for 10 minutes, 3 samples | ≤60% RH | Alert, local inspection task |
| Coffee running low | <20% for 30 seconds, 3 samples | ≥30% | Alert, replenishment proposal |
| Bathroom paper running low | <20%, 1 sample (existing normalized sensor contract) | ≥30% | Alert, replenishment proposal |
| Bin full | >80% for 60 seconds, 3 samples | ≤60% | Local cleaning task; initially disabled |
| Persistent obstruction | <300 mm for 60 seconds, 3 samples | ≥400 mm | Alert, local task, station sound command; initially disabled |
| Sustained sound | Relative amplitude >2,000 for 30 seconds, 3 samples | ≤1,500 | Alert; initially disabled |
| Equipment tilted | Tilt >30° for 5 seconds, 3 samples | ≤15° | Alert; initially disabled |
| Board telemetry absent | No fresh physical reading for 30 seconds | Fresh physical reading | Alert |

These are initial operating settings, not certified safety limits or a diagnosis. Defaults target the kitchen board except the bathroom paper rule. CO₂ hardware availability was reported by the user; no physical CO₂ calibration or firmware verification was performed by this implementation. A stock reading means a calibrated percentage for the configured container/item. Raw distance does not identify an item or establish a stock percentage. The single distance sensor needs a chosen physical placement; bin and obstruction configurations are alternative uses. The orientation feed and microphone amplitude need hardware verification before enabling those checks.

## Actions and control

Read rules with `GET /api/facility`, then submit a complete rule to `POST /api/facility/rules/:id` to change its enabled flag, thresholds, timing and actions. Settings persist in the server's SQLite database, overriding initial defaults in `config/facility-rules.json`. Rule identity, board, metric, direction and catalogue item remain fixed. Changing settings clears incomplete debounce samples; existing active incidents remain visible until a recovery reading, even if a rule is temporarily disabled.

- **Dashboard alert:** records an incident with severity, time, source and acknowledgement. Acknowledgement marks it seen; it does not recover the reading.
- **Maintenance task:** creates a local ticket for the board's responsible team. No contractor is contacted. Sensor recovery does not claim that the assigned work was completed.
- **Replenishment request:** prepares one catalogue unit using existing supplier ranking, availability, duplicate checks, spending limits and exact quote approval. Physical routine readings can follow existing automatic-approval policy. Simulated readings always require human review. Supplier execution remains simulated. Replenishment is available only for a configured stock rule and approved supply.
- **Sound:** creates a persistent station command for the existing hardware bridge to claim and acknowledge. Pending commands are cancelled on recovery. A claimed command is never automatically replayed after a timeout or process restart; an uncertain delivery needs reconciliation. Queuing does not claim that a physical sound occurred. Sound is disallowed on noise rules to avoid feeding the measurement back into itself. The optional browser chime remains independently muted until enabled.

## Input contract

`POST /api/facility/readings` accepts a canonical sample:

```json
{
  "event_id": "unique-stable-sample-id",
  "board_id": "kitchen",
  "metric": "co2_ppm",
  "value": 1300,
  "timestamp": "2026-11-05T15:00:00.000Z",
  "simulation": false
}
```

Use the current sample time. Readings older than 120 seconds, more than 5 seconds in the future, invalid values, and out-of-order timestamps are rejected. Retrying uses the same event ID and payload; changing its payload is rejected. A gap over the configured maximum restarts the sustained-condition window. Live and simulated samples, debounce state, heartbeats and incidents are separate. Simulated samples cannot satisfy or recover a physical incident, and are excluded from the agent's live sensor tools.

Canonical metrics: `co2_ppm`, `temperature_c`, `humidity_pct`, `stock_level`, `bin_fill_pct`, `obstruction_mm`, `noise_level`, `tilt_deg`, `heartbeat`. Heartbeat values are 0–1; any accepted physical measurement also refreshes that board's physical heartbeat. Missing telemetry begins a grace period at startup and is evaluated by the server every 2 seconds, even without an open browser.

Compatible routes:

| Route | Purpose |
| --- | --- |
| `GET /api/facility` | Rule settings, source-labelled readings, incidents and evaluation time |
| `POST /api/facility/rules/:id` | Validate and persist the complete rule |
| `POST /api/facility/incidents/:id/acknowledge` | Mark an incident seen |
| `POST /api/sensor` | Existing `{board_id,kind,value}` input; supports optional stable `event_id`, `timestamp`, `simulation`; canonical metrics always use the fixed engine |
| `POST /api/device-events` (`type:stock_low`) | Completed calibrated stock check → exact catalogue replenishment, without model interpretation |
| `GET /api/devices/:board_id/actions` | Audit station commands and delivery states |
| `POST /api/devices/:board_id/actions/claim` | `{consumer_id}` claims pending commands before hardware delivery |
| `POST /api/devices/:board_id/actions/:id/acknowledge` | `{status:'delivered'|'failed',detail?}` records the adapter outcome |
| `POST /api/telemetry` | `{key,value,board_id?,event_id?,timestamp?,simulation?}`; defaults to kitchen and physical input |
| `GET /api/air-quality` | Latest physical kitchen temperature/humidity/CO₂, with stale values returned as unknown |
| `GET /api/events` | `facility.updated` notifications; clients refresh persisted state |

Temperature aliases are `temperature`, `th_temperature`, `temperature_c`; humidity aliases are `humidity`, `th_humidity`, `humidity_pct`. CO₂ accepts `co2`, `co2_ppm`, and `co2_co2`. Firmware fields `co2_temperature`/`aht20_temperature` map to temperature; `co2_humidity`/`aht20_humidity` map to humidity. Auxiliary module fields are never interpreted as CO₂. Unknown telemetry keys return `accepted:false`; raw `dist_min_distance` is deliberately not converted to inventory. The hardware owner must forward readings using the chosen mapping; the existing USB bridge currently handles request events rather than all telemetry.

Generated portable shapes are `packages/contracts/facility-reading.schema.json`, `facility-rule.schema.json`, and `station-action*.schema.json`; regenerate with `npm run contracts:export` after shared schema edits.

Completed `stock_low` device events already represent a calibrated threshold edge, so they skip raw-reading debounce and map straight to the fixed replenishment action. The backend validates the product's station and goods scope, fixes quantity at one catalogue unit, and enforces duplicate/quote/spending approval rules. Missing legacy source provenance is treated conservatively as simulated input requiring review; an explicit boolean `payload.simulation:false` can follow configured physical-input policy. Retries preserve the event ID and exact payload. A changed payload for that ID is rejected.

Device registration persists before dispatch. The server's background tick resumes interrupted stock preparation using that stored event. If a supplier attempt was already started or has an uncertain outcome, the server stops and requires reconciliation instead of executing it again.

Only the existing station bridge should claim sound commands. The server marks them `dispatched` before returning them. If the claim response or playback result is lost, the same command is not automatically returned again. Acknowledgements are idempotent and cannot overwrite a terminal delivered/failed result. This implementation did not open serial ports or upload firmware.

## Try it and verify

Publish current samples to `POST /api/facility/readings` with `simulation:true` for an isolated software test. Submit samples over the configured duration; simulated samples never combine with a physical stream. To test physical readings, forward the actual board samples with `simulation:false`.

Run `npm test`, `npm run check`, and `npm run build`. The focused tests cover every configured feature, local action dispatch, recovery, restart, event payload binding, live/simulated isolation, disabled-rule behavior, purchase deduplication and exact approval, including assertions that deterministic action paths never invoke a model.

## Anders: hardware acceptance

This feature branch includes the Python runtime and reviewed shared-connection fixes from PRs #4 and #6, now on main. Check out `NLS/deterministic-facility-backend`, install dependencies, and start an isolated test database:

```sh
npm ci
npm run setup:python
AGENT_DATABASE_PATH=.data/facility-hardware-test.sqlite npm run dev:server
```

The API stays on `127.0.0.1:3001`. Keep one process responsible for USB serial; reuse the existing connected station bridge.

1. Forward each physical `co2_co2` sample to `POST /api/telemetry` with its measured `value`, `board_id:"kitchen"`, stable `event_id`, current ISO `timestamp`, and `simulation:false`.
2. Confirm `GET /api/facility` now contains a live CO₂ reading. The no-telemetry incident should recover when fresh physical samples arrive.
3. Enable sound for the CO₂ test through the complete rule update below. CO₂ defaults to alert + inspection ticket; it does not request sound until configured.
4. With three high samples spanning at least 30 seconds and no gap over 30 seconds, expect one active CO₂ incident, one local inspection task, and one pending sound command.
5. The station bridge posts `{ "consumer_id": "office-station-bridge" }` to `/api/devices/kitchen/actions/claim`, maps `kind:"sound", pattern:"attention"` to its verified speaker API, and acknowledges the command using its returned `id` and `{ "status":"delivered" }` only after the adapter confirms output. Report `{ "status":"failed", "detail":"..." }` on a known failure. Lost delivery remains dispatched/uncertain and is not automatically replayed.
6. Continued high readings must create no further task or command. A reading at/below 1,000 ppm recovers the incident; any still-pending sound is cancelled. A subsequent sustained high episode may create a new incident.
7. Verify the actual screen/LED/speaker output on the board. API queueing, firmware acknowledgement and audible output are separate observations; report which were confirmed.

Configure the CO₂ sound action without changing thresholds:

```sh
python3 - <<'PYTHON'
import json
from urllib.request import Request, urlopen
base = "http://127.0.0.1:3001"
with urlopen(base + "/api/facility") as response:
    rule = next(r for r in json.load(response)["rules"] if r["id"] == "co2-high")
rule["actions"] = ["alert", "ticket", "sound"]
request = Request(base + "/api/facility/rules/co2-high", data=json.dumps(rule).encode(),
                  headers={"Content-Type":"application/json"}, method="POST")
with urlopen(request) as response:
    print(json.load(response))
PYTHON
```

For a forced test, configure a temporary threshold in this isolated database or send explicitly simulated samples. Do not label generated values as physical readings. Restore the original threshold/recovery settings after testing. The software tests do not claim physical sensor calibration or speaker playback.

## Verified USB threshold test (2026-11-05)

A cable-connected board supplied 62 physical sensor samples to the backend. A temporary CO₂ trigger of 829 ppm with the normal 30-second sustained duration produced exactly one incident, a local test task and one sound command. The board acknowledged LED, screen and speaker handlers, and the user confirmed hearing the three-note chime. A subsequent physical reading satisfied the temporary recovery setting; the incident recovered and the sound delivery was recorded. The original rule was restored exactly: trigger 1,200 ppm, recovery 1,000 ppm, three samples over 30 seconds, alert + ticket actions. The local test task was resolved. This verified the physical pipeline using a deliberately lowered test threshold; it did not demonstrate an actual reading over 1,200 ppm. The sensor quantity aliases were corrected so CO₂-module temperature/humidity can never reset or trigger the CO₂ rule.

## Overnight cooling and moisture checks

Two configurable rules run daily between 22:00 and 07:00 Europe/Oslo, including weekends. `overnight-temperature-low` flags temperature below 16°C for 15 minutes, resetting at/above 18°C. `overnight-humidity-high` flags relative humidity above 60% for 30 minutes, resetting at/below 55%. Both need at least three valid samples with no gap above 120 seconds and create one alert, local inspection task and station sound command per episode. Initial settings need tuning to the office heating schedule and sensor placement.

A rule can have optional `schedule:{timezone,start,end}`. Start is inclusive, end exclusive; overnight ranges wrap midnight and use IANA timezone/DST rules. `schedule:null` retains continuous operation. Inactive readings do not count toward a new incident. A previously active incident stays visible, and a valid recovery reading can clear it outside the schedule. The clock alone never proves recovery.

Temperature is a proxy for unexpected cooling: it cannot confirm an open window, distinguish a heating fault, or detect an open window while heating maintains room temperature. The alert says “possible open window or heating issue.” Reliable open/closed detection requires a contact sensor on the window. Relative humidity flags persistent moisture conditions; it does not diagnose mold or measure window/wall surface moisture. Moisture control and cold-surface condensation matter; see [EPA moisture guidance](https://www.epa.gov/mold/mold-course-chapter-2).

Monitoring requires a powered board, fresh measurements, a running backend and a station bridge consuming outputs. The missing-telemetry alert detects loss of that feed. A disconnected/unpowered board cannot monitor the office overnight.

The overnight rule was also verified on the reconnected board: actual temperature around 28.5°C satisfied a temporary “below 29.46°C for 30 seconds” condition during a temporary current-time schedule. It created one incident/task/sound; firmware acknowledged and the user confirmed hearing the chime. A new physical reading satisfied recovery, the local test task was resolved, and the original 16°C/15-minute, 22:00–07:00 Europe/Oslo rule was restored exactly. The test confirmed the measurement-to-action pipeline, not an actual open window or a cold room. Daily schedule/midnight/DST and inactive-window behavior are covered by automated tests.
