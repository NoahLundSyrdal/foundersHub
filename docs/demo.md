# Demo guide

## Before presenting

Run `npm run setup`, create `.env` from `.env.example`, and start `npm run dev`. Keep `OFFICE_MODEL=mock` for a repeatable offline demo, or set `OFFICE_MODEL=anthropic:claude-sonnet-5-5` with `ANTHROPIC_API_KEY` for the live agent. Orders are always simulated. Open http://localhost:5173 and verify the API is available.

The default demo does not make a real purchase. Prices, delivery estimates, supplier name, and order reference are simulated. State this clearly when presenting.

## Main flow: kitchen supplies

1. On the dashboard's Kitchen board, pick the speaker (stand-in for voice ID until it works). As `guest` the order needs approval; as Leo it goes through under 500 NOK.
2. Enter `We need coffee`.
3. Show how the request is attached to the kitchen and matched to the approved coffee product. The live activity panel shows each tool call and result as it happens.
4. Review the quantity, price, and demo delivery estimate.
5. Approve the displayed proposal.
6. Show its simulated order reference and updated status.
7. Submit the same need again. Explain that the existing active order is reused instead of creating another purchase.
8. Confirm delivery with **Delivered** under Orders (`POST /api/orders/{id}/delivered`); future restocking can then create a new order.

## Physical context

Use `We are out of paper` at the bathroom station and the printer station. The intended matches are toilet paper and printer paper respectively. This needs the live model; the mock model only matches exact product names. If the input is ambiguous for a station, show the clarification question and answer it in the transcript field.

## Automatic detection

Run `python3 apps/device/src/bridge.py --simulate --station bathroom --item toilet-paper --level 15`, or post a reading that crosses the bathroom threshold: `curl -X POST localhost:3001/api/sensor -H 'content-type: application/json' -d '{"board_id":"bathroom","kind":"stock_level","value":12}'`. Either one starts the same agent and approval workflow without anyone speaking. When showing a real sensor later, use a calibrated fixed holder or stack; describe exactly what the sensor measures.

## Maintenance

At the kitchen station, submit `The coffee machine is leaking`. Show the maintenance ticket and responsible team under Tickets. Use **Resolve** only to demonstrate local workflow completion. No plumber or technician has been contacted by the foundation.

## Device bridge

With the API running:

```sh
npm run device:simulate
```

This posts a canonical kitchen transcript event and prints the response and device feedback command. With a real board, run `apps/device/src/atech_bridge.py` (uses the `atech` SDK); see `apps/device/README.md`. A command printed by the simulator is not proof that a physical screen or LED has responded.

## Recovery

- If voice input is unavailable, type the same words on the dashboard.
- If the board or USB connection fails, use dashboard buttons or the Python simulator; call out that hardware input is being simulated.
- If the live model is unavailable, set `OFFICE_MODEL=mock` and restart the API; do not silently describe mock results as live Claude output.
- If requests from rehearsals remain, use their actual status or stop the backend and reset the local ignored database before restarting. Never reset while the server is writing.
- If a purchase fails, inspect the recorded state. Do not bypass approval or repeatedly retry a potentially executed real purchase when adding live suppliers later.

## First live-purchase milestone after the foundation

Choose one supplier and one low-cost product. Add authenticated approval, exact price/delivery confirmation, an execution identifier, provider confirmation, and reconciliation before enabling checkout. A live supplier adapter should be a separate explicitly authorized change. Until that exists, keep the confirmation labelled simulated.
