# Local personal WhatsApp bridge

This optional demo worker delivers only messages explicitly authorized and queued
by the Python application. It uses the unofficial, MIT-licensed
[Baileys WebSockets client](https://github.com/WhiskeySockets/Baileys), pinned to
`7.0.0-rc14`. It does not need a WhatsApp Business account or a paid messaging API.
The laptop, API and worker must remain running. This is a demo integration rather
than a production-supported WhatsApp API.

## Pair once

Start the Python API first; it creates the private bridge token. Then:

```sh
npm --prefix apps/whatsapp ci
npm --prefix apps/whatsapp run pair
```

The worker prints the path to `.data/whatsapp/pairing.png`. Open that image and use
WhatsApp on your phone: **Settings → Linked devices → Link a device**. Pairing
grants this local client access to the WhatsApp session. The worker does not
ingest incoming chats or contacts, and explicitly disables history sync. A linked
client necessarily receives protocol data; there is no claim that WhatsApp sends
it no incoming data. Only receipts for this application's outgoing IDs are used.

The QR is overwritten when it expires and removed after connection. Treat it and
the auth directory as private authentication material; never commit or share
them. QR contents, tokens, message text and numbers are never printed in logs.

The pair command continues as the normal worker after connection. For subsequent
runs, use:

```sh
npm --prefix apps/whatsapp start
```

### Pair with a code instead

If QR pairing fails, place the account's verified E.164 phone number in the private
`.data/whatsapp/pairing-phone` file (mode `0600`) and run:

```sh
npm --prefix apps/whatsapp run pair-code
```

After WhatsApp's connection-ready QR event, the worker requests one eight-character
pairing code and saves it to `.data/whatsapp/pairing-code` (mode `0600`). It prints
only that file's path, never the code or number. On the phone, open **Linked devices
→ Link a device → Link with phone number instead** and enter the code. If the code
expires or the pairing connection closes, restart the command to explicitly request
a new code; reconnects never generate repeated requests. Existing auth files are
preserved. Pairing-mode disconnects report only a numeric status for diagnosis.

Normal start refuses to create a new session. If logged out, run the pair command
again and explicitly link from the phone. To revoke access, remove the linked
device in WhatsApp on the phone, then stop this worker.

## Local storage and delivery behavior

The worker uses ignored `.data/whatsapp/`: `auth/` for session material,
`consumer-id` for its stable identity, `receipts.json` for outgoing receipt
reconciliation, and `worker.lock` to prevent concurrent workers. Directories are
private and files use mode `0600`. `bridge-token` is owned by the API. The default
API is `http://127.0.0.1:3001`; `OFFICE_WHATSAPP_API_URL` can select another
**loopback origin only**. Redirects are rejected so the token cannot be forwarded.

The worker heartbeats every five seconds and claims at most one queued message
per second while connected. The backend expires old unclaimed requests and
persists a one-shot claim before returning any recipient or content. The worker
also persists the outgoing ID before calling WhatsApp. Neither reconnects,
timeouts, process restarts nor failed receipt callbacks resend that message.
Baileys message retries and session recreation are disabled.

`sent` requires a WhatsApp server acknowledgement; `delivered` and `read` require
the corresponding provider receipts. No receipt within 30 seconds, a network
error or an interrupted send remains uncertain. A late genuine receipt can
reconcile that outcome. API receipt failures retry only the acknowledgement;
conflicting receipts are retained for local reconciliation, with no resend.

No incoming messages cause a reply, no chat history enters the product, and this
worker cannot book a room or authorize a purchase. Its only send capability is a
single backend-claimed, already-authorized text message.

## Verification

```sh
npm --prefix apps/whatsapp run check
npm --prefix apps/whatsapp test
```

Tests use fake transport and temporary private files. They do not connect to
WhatsApp or send real messages. Physical pairing and provider delivery need a
separate live test after the user links their phone.
