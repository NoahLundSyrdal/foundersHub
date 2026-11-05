# Local stage demo

Preview: http://127.0.0.1:5181 (API on 127.0.0.1:3011).
Use live Anthropic interpretation with simulated supplier execution. The existing
board retains its firmware ID `kitchen`; its stage placement displays that feed.
No extra physical board is implied by the map's simulated stock placements.

The board is connected to a separate computer. Its bridge and this dashboard must
use the same reachable API. Set `OFFICE_API_URL` on the dashboard computer to that
backend address and restart Vite. Do not point a remote board at this computer's
127.0.0.1 address: loopback refers to each computer itself. Keep the local preview
on loopback; shared network access needs the team's existing secured backend.
Environmental samples are forwarded at most every five seconds, below the
dashboard's 30-second stale window. The agent and map read the same physical
source. Missing or stale values are labelled, never replaced with invented values.

The preview currently starts with two open local reports (event-hall chair and café
napkins). Keep other rooms clear: saving a report about the worktables creates their
marker. Resolved cases remain in History. A purchase ticket shows its proposal and
simulated supplier reference when an order exists; maintenance reports do not require
a fabricated purchase. Work opens on the next incomplete step; all steps, case
details, routines and history remain available in expandable sections.

## Main flow: kitchen supplies

1. Press the kitchen station's simulated button.
2. Enter `We need coffee`, or use browser voice input if supported and tested.
3. Show how the request is attached to the kitchen and matched to the approved coffee product. The activity trace shows operational stages; in live Claude mode, it also makes tool use visible.
4. Review the quantity, price, and demo delivery estimate.
5. Approve the displayed proposal.
6. Show its simulated order reference and updated status.
7. Submit the same need again. Explain that the existing active order is reused instead of creating another purchase.
8. Confirm delivery using the dashboard if available, or resolve the ordered request through `/api/requests/:id/resolve`; future restocking can then create a new order.

## Physical context

Use `We are out of paper` at the bathroom station and the printer station. The intended matches are toilet paper and printer paper respectively. If the input is ambiguous for a station, show the clarification question and answer it in the transcript field.

## Automatic detection

Trigger the bathroom low-stock simulation. It submits a `stock_low` event for `toilet-paper` and enters the same purchase/approval workflow. When showing a real sensor later, use a calibrated fixed holder or stack; describe exactly what the sensor measures.

## Maintenance

At the kitchen station, submit `The coffee machine is leaking`. Show the maintenance ticket and responsible team. Mark it resolved only to demonstrate local workflow completion. No plumber or technician has been contacted by the foundation.

## Device bridge

With the API running:

```sh
npm run device:simulate
```

This posts a canonical kitchen transcript event and prints the response and device feedback command. See `apps/device/README.md` for serial bridge options and supported firmware messages. A command printed by the simulator is not proof that a physical screen or LED has responded.

## Recovery

- If browser voice input is unavailable or denied, submit the same words as text.
- If the board or USB connection fails, use dashboard buttons or the Python simulator; call out that hardware input is being simulated.
- If optional Claude interpretation is unavailable, switch to the mock provider and restart the API; do not silently describe mock results as live Claude output.
- If requests from rehearsals remain, use their actual status or stop the backend and reset the local ignored database before restarting. Never reset while the server is writing.
- If a purchase fails, inspect the recorded state. Do not bypass approval or repeatedly retry a potentially executed real purchase when adding live suppliers later.

## First live-purchase milestone after the foundation

Choose one supplier and one low-cost product. Add authenticated approval, exact price/delivery confirmation, an execution identifier, provider confirmation, and reconciliation before enabling checkout. A live supplier adapter should be a separate explicitly authorized change. Until that exists, keep the confirmation labelled simulated.

## Small environmental-history fixture

Seed a chosen **local demo database** (use the same path as `AGENT_DATABASE_PATH`):

```sh
uv run --project apps/agent python -m office.demo_history --database /private/tmp/foundershub-main-preview.sqlite
```

This adds exactly two completed, explicitly simulated stage checks from four and two
hours before the first seed, with CO₂ / temperature / humidity of
1650 ppm / 27.2 °C / 64% and 1820 ppm / 28.1 °C / 67%. Re-running does not duplicate
or refresh the fixtures. The stage button uses board `kitchen`; these samples do
not describe other rooms. They never enter physical telemetry or execute actions.
The stage inspector shows the latest simulated, stale values; History shows both checks.

Ask **“Can you book a meeting here at the stage?”** The read-only history tool makes
both snapshots available to the live agent for seven days. Mock mode has a labelled,
scripted response warning about the two simulated high-CO₂ checks. Neither mode may
claim these are current conditions or that a booking occurred without a provider result.
