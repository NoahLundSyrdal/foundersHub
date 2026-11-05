# Durable office work

The common workflow lives in the existing Python API and SQLite database. It tracks an outcome beyond an interpreted request, order or ticket. The TypeScript dashboard exposes it in **Work**.

This implementation includes owned work items and dependent steps, deadlines and next reviews, exact action approvals, evidence-gated completion, local recurring routines, bounded follow-up and escalation, and history. Six human coordination templates cover replenishment, maintenance, onboarding, meeting actions, project milestones and vendor renewals. These templates collect confirmations; they do not connect calendar, inbox, identity, finance or supplier services.

## Start and use

Use the usual `npm run setup:python`, `npm install`, and `npm run dev` setup. The database schema is added at startup, without altering existing task, order or ticket tables. No new service or dependency is required.

1. Open **Work** and create an outcome from a template, with its accountable owner. Default unassigned coordination routes to the explicit `office-coordinator` owner; replace that label with the responsible person or team.
2. Open a step to see its dependencies and required evidence. Evidence records a summary, source, recorder and timestamp. A named recorder is a local operator assertion, not authenticated identity or purchasing authority.
3. Record the required evidence and complete the step. The backend rejects missing dependencies or evidence. When every step is satisfied, explicitly close the work.
4. If waiting or blocked, record a reason and future next review. Every open work item retains a next review even after escalation.

Existing purchasing approvals remain in the linked request. The common layer cannot approve or execute a linked order through its generic simulator. Order confirmation completes only the order step. Receipt and inventory remain open. Recording receipt evidence and completing the receipt step confirms the **full ordered quantity**, updates the existing order as delivered, and records one inventory receipt per order. The existing delivery-confirmation controls also feed this adapter.

Inventory is a ledger of **recorded received units**, grouped by item and location. It does not infer initial stock, consumption, physical count, partial delivery or returns. All current supplier orders and inventory movements are labelled simulated. A replay or restart cannot count the same order twice.

Maintenance completion is separate from verification. A resolved local ticket supplies repair confirmation; the verify step still needs independent evidence. Conversely, completing the repair step with evidence resolves its linked local ticket. Sensor recovery alone does not verify repair or close work. Closing/cancelling coordination does not cancel a supplier order or external commitment.

## Routines and follow-up

Create a routine in Work with an IANA timezone, local time and selected weekdays. Routines persist through restart; the application must be running to evaluate them. The startup check and a ten-second background loop use the same idempotent tick. Each local date/time/timezone slot has one occurrence identity.

- `latest` catches up at most the latest missed slot; it does not flood the queue with historical occurrences.
- `skip` never backfills earlier local calendar days; today's due occurrence can still run.
- `link` attaches a new occurrence to unfinished work and records a local escalation. `allow` creates a separate obligation.
- Nonexistent daylight-saving times are skipped. Repeated times produce one occurrence using the first fold.
- Each due review creates at most one local reminder per tick and advances the next review. The default is two reminders, then one escalation to the escalation owner. Human progress resets this cycle; scheduler updates do not. Routine settings can change the interval and reminder limit.

Notifications remain in the local inbox with durable acknowledgement. There are no outbound reminder sends. Channel authorization, delivery policies, quiet hours and live connectors must be implemented before enabling external notifications.

## API and state ownership

All new database routes are asynchronous and mutations run under the application's existing lock. The core command layer uses SQLite savepoints, payload-bound event keys and optimistic `expected_version` checks. A changed event payload or stale new command returns `409`; missing work returns `404`; malformed input returns `422`. Exact command retries return the persisted result, including adapter consequences. The dashboard refreshes authoritative state on `work.updated`, SSE reconnection and polling fallback.

| Route | Purpose |
| --- | --- |
| `GET /api/work` | `{items: WorkItem[]}` |
| `GET /api/work/templates` | Available coordination templates |
| `POST /api/work` | Create from a template or custom validated steps |
| `GET /api/work/:id` | Work, steps, evidence, history and domain links |
| `POST /api/work/:id/actions` | Assign, wait, block, resume, cancel, fail, start/complete step, simulate, reconcile or close |
| `POST /api/work/:id/evidence` | Record a required evidence kind for a step |
| `POST /api/work/:id/decisions` | Decide on the exact server-supplied action fingerprint |
| `GET/POST /api/work/routines` | List or create recurring work |
| `POST /api/work/routines/:id/enabled` | Pause or resume a routine |
| `GET /api/work/notifications` | Local reminder/escalation inbox |
| `POST /api/work/notifications/:id/acknowledge` | Acknowledge locally; does not complete work |
| `GET /api/work/inventory` | Received quantity ledger, explicitly excluding consumption |

Shared TypeScript/Zod contracts are in `packages/contracts/src/work.ts`; `npm run contracts:export` writes the portable schemas. Python `work_models.py` validates commands. Work records are additive; existing task/order/ticket states, catalogue restrictions, spending caps, quote fingerprints and uncertain-execution protections remain authoritative.

```json
{
  "event_key": "onboarding-2026-11-09",
  "template_key": "onboarding",
  "title": "Prepare the new starter",
  "outcome": "Equipment, authorized access and orientation are confirmed",
  "owner": "office-coordinator",
  "context": {"location_id": "office"},
  "due_at": "2026-11-09T08:00:00+01:00",
  "simulation": true
}
```

Custom steps with purchase, message, booking or access actions always require exact approval. Generic execution is simulation-only and returns a `SIM-` reference. A started or uncertain attempt must be explicitly reconciled with a result, reference and reason; it cannot be automatically retried or silently closed. Approval expiry blocks new execution; confirmed execution can still receive later completion evidence. The APIs are local operator controls, not production authentication. Speaker/requester labels never grant authority. Never expose this application beyond loopback without separate authentication and authorization work.

## Isolated worktree ports

Keep another checkout's live API, database and board connection untouched. For validation, use a separate database and ports in two terminals:

```sh
OFFICE_MODEL=mock AGENT_DATABASE_PATH=.data/work-validation.sqlite \
  uv run --project apps/agent uvicorn office.api:create_app --factory \
  --host 127.0.0.1 --port 3101 --timeout-graceful-shutdown 2
```

```sh
OFFICE_API_URL=http://127.0.0.1:3101 npm run dev -w @office/web -- --port 5175
```

Open `http://127.0.0.1:5175`. `OFFICE_API_URL` is an optional shell environment setting read by Vite; its default is `http://127.0.0.1:3001`. This explicit validation configuration uses mock interpretation and simulated execution. To use live interpretation, configure the supported Anthropic model and credential in this worktree's ignored environment; no credentials or runtime databases are copied from another checkout.

## Verification

Run `npm test`, `npm run test:procurement`, `npm run check`, `npm run contracts:export`, and `npm run build`. Workflow tests cover command retries, changed payloads, stale versions, approval scope, step dependencies, missing evidence, uncertain results, receipt accounting, restart recovery, schedule overlap, timezone/DST behavior and bounded local follow-up. Hardware and real provider execution require their own validation.

### Approval recovery and completion guards

Cancelled or failed linked work cannot approve a purchase. Reject the old linked request and create a new request if the need returns. Service work with a pending booking requires its confirmed simulated booking before repair steps or closure can be confirmed. Ticket-only internal maintenance continues to use recorded repair and verification evidence.

Expired standalone step approvals offer **Renew approval** in Work. Renewal preserves the action snapshot and returns it to pending review; it never executes the action. Rejected or already executed steps cannot be renewed. Receipt and repair confirmations publish a dashboard refresh after their domain updates commit. The background work monitor logs failed ticks without private details and continues local coordination on the next tick.
