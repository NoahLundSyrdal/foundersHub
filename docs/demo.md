# Local stage demo

Preview: http://127.0.0.1:5181 (API on 127.0.0.1:3011).
Use live Anthropic interpretation with simulated supplier execution. The existing
board retains its firmware ID `kitchen`; its stage placement displays that feed.
No extra physical board is implied by the map's simulated stock placements.

Connect the existing USB board, then run from the repository:
```sh
apps/device/.venv/bin/python apps/device/src/atech_bridge.py --api http://127.0.0.1:3011
```
Use the hardware owner's installed Atech environment if this checkout has no device
venv. Do not flash firmware to start a dashboard demo. Environmental samples are
forwarded at most every five seconds, below the dashboard's 30-second stale window.
The agent and map read the same physical source. Missing or stale values are labelled,
never replaced with invented values.

The preview currently starts with two open local reports (event-hall chair and café
napkins). Keep other rooms clear: saving a report about the worktables creates their
marker. Resolved cases remain in History. A purchase ticket shows its proposal and
simulated supplier reference when an order exists; maintenance reports do not require
a fabricated purchase. Work opens on the next incomplete step; all steps, case
details, routines and history remain available in expandable sections.
