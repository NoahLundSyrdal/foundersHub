# Demo guide

## Before presenting

Run `npm install`, create `.env` from `.env.example`, and start `npm run dev`. Keep `AGENT_PROVIDER=mock` and `SUPPLIER_PROVIDER=mock` for a repeatable local demo. Open http://localhost:5173 and verify the API is available.

The default demo does not make a real purchase. Prices, delivery estimates, supplier name, and order reference are simulated. State this clearly when presenting.

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
