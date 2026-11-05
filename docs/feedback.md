# Office feedback

The dashboard feedback feed combines significant request updates and fixed facility alerts. `GET /api/feedback` returns the latest 30 durable events and channel readiness; `feedback.updated` on `/api/events` tells clients to refresh. Requests remain authoritative. A WhatsApp message, spoken update or chime cannot approve a purchase.

Domain publishers capture notification events synchronously after updating state, scoped to the application's database connection. A rapid order followed by delivery or an alert followed by recovery therefore retains both updates without waiting for the periodic scan. The scan remains a recovery fallback. Resolving a request updates its timestamp, so completion notifications reflect the actual completion time.

This change provides notification logic, without adding dashboard controls or styling. Browser or board consumers can use durable event IDs to play one chime and a concise spoken update, skip the initial history, and refresh on `feedback.updated`. Board audio belongs to the hardware owner's existing voice-station bridge; do not start a second serial-port owner. The API reports board sound as unverified because a queued command is not proof that a person heard it. Physical playback must be tested on the board.

## Optional WhatsApp setup

WhatsApp is **off by default**. Set these values only in the ignored root `.env`, then restart the Python API:

```dotenv
OFFICE_WHATSAPP_ENABLED=true
TWILIO_ACCOUNT_SID=<Twilio account SID>
TWILIO_AUTH_TOKEN=<Twilio auth token>
OFFICE_WHATSAPP_FROM=whatsapp:<approved sender in E.164 format>
OFFICE_WHATSAPP_TO=whatsapp:<consenting recipient in E.164 format>
OFFICE_DASHBOARD_URL=http://127.0.0.1:5173
OFFICE_WHATSAPP_CONTENT_SID=
```

Use a Twilio WhatsApp Sandbox with a joined recipient for a local demo, or a registered WhatsApp sender. Free-form messages require an active customer-service conversation window. For proactive notifications outside that window, configure an approved Twilio Content template with `OFFICE_WHATSAPP_CONTENT_SID=HX...`; its three variables must be `1` = event title, `2` = detail (including the exact NOK total and simulation label), and `3` = dashboard link. Source: [Twilio WhatsApp quickstart](https://www.twilio.com/docs/whatsapp/quickstart), [Twilio Messages API](https://www.twilio.com/docs/messaging/api/message-resource), [WhatsApp notification templates](https://www.twilio.com/docs/whatsapp/tutorial/send-whatsapp-notification-messages-templates).

The loopback URL opens this laptop's dashboard. It will not open the laptop from a phone. A phone-accessible authenticated deployment requires separate authorization and implementation; this feature does not publish or expose the local API. HTTPS dashboard URLs are accepted for that future setup. Credentials, URLs with embedded credentials, and recipient numbers are never returned by the feedback API.

New approval, simulated order, maintenance, clarification, failure, resolution and facility updates produce one outbound message per stable event. Listening prompts stay on the station/dashboard. Startup shows existing history but does not send historical messages. Configuring WhatsApp later does not replay old disabled events.

Before dispatch, actionable prompts are checked against the current request and exact proposal. Superseded approvals and recovered facility alerts are skipped. Concurrent delivery passes atomically claim each pending message before HTTP; the purchase mutation lock is released during the network request. Queued messages left unattempted at restart are skipped as history; attempted messages retain their uncertain outcome.

Before calling Twilio the send attempt is persisted. A timeout, interrupted process or ambiguous response is marked **uncertain** and never automatically retried. Provider rejections are marked **failed**. **Sent** means Twilio accepted the message; delivery is not confirmed. Check the provider console to reconcile uncertainty. No delivery webhook or incoming-message/approval handler is installed. No real WhatsApp sends were used in automated tests.
