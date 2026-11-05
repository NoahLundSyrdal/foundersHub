# Atech hardware integration

## Current state

The repo includes a Python event bridge and simulator. The team confirmed a 14-port motherboard. Module inventory and actual port assignments still need confirmation by the hardware owner. No firmware has been flashed or hardware behavior verified as part of the local foundation.

Atech modules plug into a motherboard; the buttons are not separate wireless stations. For the hackathon, one board with three labelled buttons represents kitchen, bathroom, and printer. Separate rooms would normally require separate powered boards and a network transport to the same backend.

## Hardware reference supplied by the team

The supplied repository is [Atech Atelier](https://github.com/atechdotdev/atech-atelier),
Atech's FreeCAD-based CAD application. It contains board/module geometry and
example assemblies, useful for designing the station enclosure. It is not the
firmware SDK or the office device bridge.

Its [README](https://github.com/atechdotdev/atech-atelier/blob/main/README.md#common-questions)
states that Atech's 10-port and 14-port motherboards use ESP32-S3 and communicate
via USB serial or Wi-Fi with JSON. That is compatible with the intended bridge
architecture; the actual board's firmware must still emit the documented office
events and implement `office_feedback`. Compatibility has not been physically
verified.

For enclosure work, reference
[`models/presets.yaml`](https://github.com/atechdotdev/atech-atelier/blob/main/models/presets.yaml)
and the original model assets rather than guessing dimensions. The reference
14-port board is listed as 60 × 7 × 120 mm in its model axes; this is CAD geometry,
not a GPIO/port mapping. Keep Atech Atelier outside this app's runtime dependency
chain: it can design the enclosure while our server and bridge run independently.

The repository link does not establish which modules are physically available in
our kit or their numbered ports. Record those below before writing firmware.

## Confirm before firmware work

Record the actual setup here or alongside the firmware project:

| Component | Confirmed hardware / port |
| --- | --- |
| Motherboard model | 14-port (confirmed by the team) |
| Kitchen button | To confirm |
| Bathroom button | To confirm |
| Printer button | To confirm |
| Display | To confirm |
| LED grid | To confirm |
| Distance sensor | Optional; to confirm |
| Microphone | Optional; to confirm |

Use a USB-C **data** cable. Read the current Atech SDK guide for the actual board and modules before creating firmware. Do not guess pin assignments from a different board. The first SDK build may download a large compiler bundle; do not wait until the pitch to test setup.

## Event and feedback contract

Firmware emits JSON events over USB; the Python bridge translates them into the canonical `DeviceEvent` contract. Assign the station from the actual button mapping, not from free-form speech. See `apps/device/README.md` for accepted raw messages and CLI options.

The backend responds with a display/light command containing `station_id`, `text`, and `color`. The bridge sends the firmware an `office_feedback` action. That is this project's agreed action name, not a claim that stock Atech firmware already implements it. The hardware owner must add the corresponding firmware handler and map it to the actual screen/LED APIs.

Use the simulator first, then verify one button and one feedback action, then add the other stations. Stable event IDs must be retained through network retries.

## Distance sensing

Calibrate empty/full positions for a single container, stack, or holder. Convert the measurement into a bounded fill level and emit `stock_low` with an approved item ID only after the threshold persists. Add hysteresis/debouncing so readings near the threshold do not generate a flood of new events. The backend also handles equivalent active purchase requests.

An Atech distance sensor does not identify an arbitrary item; the station configuration says which supply is being measured.

## Speech

Start with typed transcripts or browser microphone input. A physical button can select the station before recording on the laptop. Browser voice input requires support and permission; keep a typed fallback.

Atech microphone recording and transporting audio to a speech service are a separate integration task. The bridge's event transport does not automatically implement audio streaming or speaker recognition. Do not use an inferred voice identity as payment authorization.

Official reference: https://atech.dev/docs
