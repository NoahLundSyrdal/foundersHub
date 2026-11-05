# Working on the office assistant

Plan and timeline: `docs/agent-plan.md`. Pydantic AI reference: `docs/pydantic-ai/`. Atech reference: `docs/atech.md`.

## Ownership

- Keep one integration owner. Hardware owns `apps/device`; voice owns `apps/agent/office/voice.py`; agent core owns `apps/agent/office/{agent,api,db}.py`; plugins and dashboard own `apps/agent/office/plugins/` and `apps/web`.
- The HTTP API in `apps/agent/office/api.py` is the contract. Tell the others before changing a route or response shape.
- Keep changes bounded to the requested feature. No extra services, frameworks, ORMs or abstraction layers without a concrete requirement. We are moving fast.

## Architecture boundaries

- One Python backend (`apps/agent`: FastAPI + Pydantic AI + SQLite) owns state, task lifecycle, approvals and execution. Devices and the browser use its API.
- One Pydantic AI agent. Capabilities are plugins: a module in `office/plugins/` with a `toolset` and `requires` (hardware modules the board needs). Register it in `PLUGINS`.
- Application code, not the model, enforces prices (from the catalogue), the office spend cap, approval and duplicate checks. Tools that spend money raise `ApprovalRequired` above the speaker's limit.
- Approval resumes from the stored pending tool calls. The client sends only approve/deny, never arguments, so an approval binds to exactly what was shown. Reject a stale decision (409).
- Preserve event idempotency (`device_events`) and purchase duplicate checks (`orders.tool_call_id` unique + open-order check).
- Never automatically retry an uncertain real supplier execution. Reconcile with the supplier using a persisted execution identifier first.
- Voice ID only sets the speaker's role. A role may auto-approve **simulated** orders under its limit. Any real checkout needs a human approval on the dashboard regardless of role. Unknown voices are `guest` (limit 0).
- Stream operational progress (tool calls/results), not private model reasoning or secrets.

## Local development and demo honesty

- Default to the mock model (`OFFICE_MODEL=mock`) and simulated orders. Label simulation in UI and docs.
- Do not claim supplier orders, contractor bookings or messages happened without an actual confirmed provider result.
- Do not push, publish, deploy, send third-party messages, or enable real spending without user authorization.
- Atech: use the official `atech` SDK. Do not invent port mappings or claim firmware works. Confirm the actual board, modules and ports before `atech build`/`upload`; check with `atech check`/`atech monitor` or your eyes.
- Keep credentials, recordings, voice embeddings, runtime databases and personal data out of Git (`.data/` is ignored). Add only placeholders to `.env.example`.
- Keep local development on loopback.

## Verification

`npm test` (backend pytest on the mock model + device tests), `npm run check` and `npm run build` for web changes. Test what would embarrass us in a demo: duplicate events, duplicate purchases, stale approvals, spending limits, failed execution, device normalization.

Update setup instructions when scripts or environment variables change. State honestly which paths were tested, simulated, or left for hardware validation.
