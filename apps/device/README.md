# Atech device bridge

Owner: hardware developer. This app converts physical input into the shared
`DeviceEvent` contract. The backend handles interpretation, approvals, and orders.
No purchase is made by the bridge.

## Start without hardware

With the root API running (`npm run dev`), from the repository root:

```sh
python3 apps/device/src/bridge.py --simulate --station kitchen --text 'We need coffee'
python3 apps/device/src/bridge.py --simulate --station bathroom --item toilet-paper --level 15
python3 apps/device/src/bridge.py --simulate --station printer
```

The last command creates an awaiting-input request. Enter a transcript for it in
the dashboard. Simulation emits one event and prints the API response and device
feedback command. It does not capture speech, recognise speakers, or program a board.

## Real board: atech SDK (use this)

The board runs firmware built with the official `atech` SDK:
[`firmware/office_station/project.yaml`](firmware/office_station/project.yaml). It is our 14-port
board as the **kitchen** station; its ports were probed and verified on 2026-11-05 (port map in
[`docs/hardware.md`](../../docs/hardware.md)). If you move a module, update the YAML and re-upload:

```sh
uv venv apps/device/.venv && uv pip install --python apps/device/.venv './apps/device[atech]'
cd apps/device/firmware/office_station
../../.venv/bin/atech validate . && ../../.venv/bin/atech build . && ../../.venv/bin/atech upload .
../../.venv/bin/atech check          # which modules answered; output modules need your eyes
cd - && apps/device/.venv/bin/python apps/device/src/atech_bridge.py
```

The first build downloads the ESP32 compiler (~2.4 GB, once). `atech_bridge.py` turns
`btn_<station>` presses into `button_pressed` events (push-to-talk) and posts throttled
temperature, humidity, CO₂ and distance readings to `/api/sensor`. It also shows each station's latest
command on the screen (custom `office_say` action) and its colour on `led_<station>_fill`. Only one
program can hold the port, so stop `atech monitor` first. Verified end to end on the real board on
2026-11-05: sensors reach `/api/sensor`, agent replies reach the screen and light grid, and a button
press opens push-to-talk ("Listening..."). The board's microphone is not usable where it sits; use the
laptop mic.

## USB hardware (legacy raw-serial bridge)

Superseded by `atech_bridge.py` above. Note that SDK firmware sends button presses as `value: 1`,
which this path ignores.

Use Python 3.10+ and an isolated environment:

```sh
python3 -m venv apps/device/.venv
apps/device/.venv/bin/python -m pip install './apps/device[usb]'
apps/device/.venv/bin/python -m serial.tools.list_ports
apps/device/.venv/bin/python apps/device/src/bridge.py --port /dev/cu.YOUR_ACTUAL_PORT
```

The default is 115200 baud. Use a USB-C **data** cable. Firmware must implement the
input events and custom feedback handler in [firmware/README.md](firmware/README.md).
No compiled firmware or verified port assignment is included.

Optional `--api http://127.0.0.1:3001` changes the backend address. Keep the API
local; this initial foundation has no remote-device authentication.

If firmware emits different button keys, create an untracked local mapping file:

```json
{"button_1":"kitchen","button_2":"bathroom","button_3":"printer"}
```

Then add `--mapping path/to/mapping.json`. These are logical names, not board ports.
Unknown button keys are rejected. Button-release events and generic telemetry are
ignored. Firmware must emit edges and debounce held buttons; the bridge does not
infer release transitions from polling.

## Contracts and failure behaviour

- Canonical contract: `packages/contracts/src/index.ts`. Generate its portable
  schema with root `npm run contracts:export`; output is
  `packages/contracts/device-event.schema.json`.
- Canonical events may also be sent as serial JSON lines. The bridge checks basic
  fields locally; the API remains the authoritative schema validator.
- Network failures and HTTP 429/502/503/504 retry twice using the **same event ID**.
  Validation failures are not retried. Server event IDs provide deduplication.
- This bridge has no disk outbox. After exhausted retries the event is reported
  as failed; restart/replay requires retaining its original event ID. Do not claim
  guaranteed offline delivery.
- USB returns immediate display/LED feedback and polls each configured station’s
  `GET /api/devices/:station/command` endpoint about once per second for dashboard
  approvals and resolution updates. Only changed commands are sent to serial. A
  null command leaves the current display unchanged; this is latest-state polling,
  not a durable queue of every intermediate update.
- Stock-low events should fire on a threshold transition; configure physical
  calibration and hysteresis before using a real distance sensor.

Serial timeout fragments are retained until a newline, including split UTF-8. Frames over 64 KiB are discarded through their next newline while feedback polling continues. This applies to the legacy raw-serial bridge; the verified Atech SDK path above remains unchanged.

## Checks

```sh
python3 -m unittest discover -s apps/device/tests
```
