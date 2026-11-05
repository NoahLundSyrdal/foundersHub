# Office digital twin

The TypeScript dashboard opens on the office twin. Its Three.js cutaway follows the supplied office photographs and sketch: event seating, worktables, café lounge, coffee counter, cloakroom and WC corridor. Dimensions, hidden interiors and the printer position are approximate.

Run `npm run setup:python` once to install Leo's Python app, then `npm run dev` to start the Python API on `127.0.0.1:3001` and the TypeScript frontend on `127.0.0.1:5173`. The dashboard uses the Python app's compatibility routes for stations, requests, exact approvals and device events; native task, state and button-listen events also refresh the view. Python state is stored separately from the old Node database in ignored `.data/agent.sqlite` (`AGENT_DATABASE_PATH` overrides it).

The **Stage button** sits on the small stage in front of the burgundy chairs. Select its label or the 3D button to see it, then open Settings to configure it. Device settings cover the name, station route and a position chosen directly on the floor. Add more buttons or level sensors from the list beside the model. Layout changes are saved only in the current browser; they do not reconfigure a board or change server policy.

## Physical button mapping

The stage button defaults to the `kitchen` request station. An actual button routed to `kitchen` through the existing bridge updates the same station request state displayed in the twin. This is a station association, not a verified device identity or connection status. Multiple visual devices mapped to one station share its request state.

Use the observed firmware key in the bridge's local `--mapping` file, for example `{"<observed-key>":"kitchen"}`. Do not infer a key or port from the scene. The bridge's `--station` flag selects a **simulated** event; physical serial events use `--mapping`. See [the hardware bridge guide](../apps/device/README.md) to connect a verified board and port.

The on-screen **Simulate button press** control uses the same API event boundary. It does not press the hardware button. Approvals still bind to the exact server proposal, and supplier execution remains simulated.

## Tracked levels

Coffee and paper level indicators are explicitly simulated. Select one to configure its tracked item, unit, range, warning threshold and sample reading. Save configuration to update its 3D gauge. A reading at or below the threshold changes the visual warning; this browser setting does not create an automatic purchasing rule. **Simulate stock report** submits the configured item's text through the existing interpretation workflow, where unsupported items can require clarification.

The current scene does not claim live stock telemetry. Connect a measured sensor feed and server-side calibration before presenting readings as real. The authoritative request lifecycle continues to refresh through the API and its SSE/poll recovery path.

## View controls

Drag to orbit, scroll or use the zoom controls, and switch to Floor plan to place devices. Click any object or room in the model to see its status, reports and team beside it. The twin is a status page: people ask for things in the chat under the agent, and each report updates the matching object, station pin and details as soon as the server changes it. A ticket exists only for a real need, something to buy or fix; greetings and questions stay in the chat. Tickets and approvals live on the Tickets page, and approvals also appear in the chat. The lists beside the model remain usable if WebGL is unavailable.
