# Room conversation and WhatsApp progress

Room requests use `POST /api/input`, the dashboard transcript route, and speech
DeviceEvent input. A room conflict and “send him a message” continue one durable
task. The live model interprets room/time and composes contextual content;
application code resolves the verified booking owner and revalidates the
booking, contact and exact message before dispatch.

Progress: checking → conflict → composing → queued → sending → sent/delivered/read.
The existing request trace, task.updated, requests.updated and board.say events
carry operational updates; room.progress also emits each step. Reconnects read
authoritative state from /api/requests, /api/state or
GET /api/room-conversations/{conversation_id}. Progress excludes phone numbers
and credentials. Uncertain sends never retry. Unclaimed jobs expire after two
minutes; interrupted claims become visibly uncertain after 30 seconds.

## Calendar and contacts

ROOM_CALENDAR_PATH defaults to ignored .data/room-calendar.json. Copy the
structure from config/room-calendar.example.json, add verified E.164 owner
numbers privately, and use explicit simulation flags. Overlapping bookings
determine the owner; ambiguous owners require clarification. Available rooms
are reported as available, never falsely claimed to be reserved.

The current adapter is read-only local JSON. The demo calendar is simulated;
a live calendar and real booking writes remain separate work.

## Pair and run WhatsApp

Start the API, then follow [the worker instructions](../apps/whatsapp/README.md).
The local worker uses an unofficial paired client. Session material stays in
ignored .data/whatsapp/. No Business account or paid messaging API is needed
for this demo. Pairing requires an explicit action on the user's phone.

```sh
npm --prefix apps/whatsapp ci
npm --prefix apps/whatsapp run pair
# After pairing, keep the worker running beside npm run dev:
npm --prefix apps/whatsapp start
```

For code pairing, privately write the account's E.164 number to
.data/whatsapp/pairing-phone and run npm --prefix apps/whatsapp run pair-code.
The short-lived code is saved in private pairing-code; enter it on the phone
under Linked devices → Link a device → Link with phone number instead.

The API creates a private token for the loopback worker. Jobs are claimed once
before network execution. Receipt retries never repeat a WhatsApp send. Only
provider acknowledgements mark sent, delivered or read. Queued jobs are not
delivery confirmation. The worker ingests no incoming chats and cannot authorize
spending or change bookings.

## Input and retries

Inputs accept optional event_id and conversation_id alongside board_id,
text/audio_b64 and the informational speaker label. Retried calls reuse the
event ID. Follow-ups should send the returned conversation ID. Existing board
clients can use the latest room context for their station for ten minutes;
another station cannot select that context. Supply requests still use procurement.

The live product test used /api/input, a simulated calendar, live composition,
the paired worker and an actual WhatsApp delivery receipt. Task progress updated
without desktop automation. No real booking changed.

## Checks

```sh
npm test
npm --prefix apps/whatsapp run check
npm --prefix apps/whatsapp test
```

Tests use fake providers and temporary private sessions, without real sends.
Keep session files, recipient numbers, databases and pairing artifacts out of Git.
