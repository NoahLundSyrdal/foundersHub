# Use cases

The office assistant has two main use cases. Both start with someone speaking to it.

| Use case | Example | Result |
| --- | --- | --- |
| 1. CO₂ checker | "Is it bad air quality here or what?" | A spoken or on-screen answer based on a real reading |
| 2. Order by voice | "We need coffee" | A processed order and a ticket |

Neither use case works end to end on `main` yet. Each one lists what is missing. Speech to text and the air quality check are on the `air-quality-stt` branch, which is not merged.

## 1. CO₂ checker

Someone asks about the air in the room and gets a plain answer.

**Example questions**

- "Is it bad air quality here or what?"
- "Is the air okay in here?"
- "Should we open a window?"

**Flow**

1. A person asks the question.
2. Speech is turned into text.
3. The speech script spots an air quality question, so it does not treat it as an order.
4. It gets the latest readings from `GET /api/air-quality`.
5. It answers with the level and what to do, for example: "Air quality is poor: CO2 1700 ppm. Open a window."

**How to judge the reading**

| CO₂ (ppm) | Level | Answer |
| --- | --- | --- |
| Up to 1000 | Good | The air is fine |
| 1000–1500 | Moderate | Consider opening a window |
| Over 1500 | Poor | Open a window |

These are the thresholds in `apps/server/src/environment/air.ts` on the `air-quality-stt` branch. Temperature and humidity also count, and the worst reading sets the level.

**Rules**

- Only state a level that came from a real reading. If there are no readings, say so.
- Say when readings are more than 2 minutes old.
- Label any simulated reading as simulated.
- The sensor reads the air where it sits. "Here" only means the room the sensor is in.
- The sensor returns 0 for about 5 seconds after boot. Treat that as no reading, not as clean air.

**What works on the `air-quality-stt` branch**

- Speech to text from the laptop microphone
- Air quality answers from temperature and humidity, read aloud on a Mac with `--speak`

Without a CO₂ sensor the answer says "no CO2 sensor", so today it is an air comfort check and not a CO₂ check.

**What is missing**

- **Merging the branch into `main`.**
- **The CO₂ sensor.** [Our kit](components.md#our-kit-counted-from-photos-2026-11-05) does not include one. We need the Atech CO₂ module (SCD40, 469 NOK). The temperature and humidity module cannot measure CO₂.
- Firmware that reads the CO₂ sensor and sends the value. The bridge already forwards any sensor key containing `co2`.

## 2. Order by voice

Someone asks for something. The assistant understands the request, processes the order and creates a ticket.

**Example requests**

- "We need coffee"
- "We are out of paper" (the station decides if this means toilet paper or printer paper)

**Flow**

1. A person says what they need at a station.
2. Speech is turned into text.
3. The assistant works out the item and quantity, using the station for context. If the request is unclear, it asks a question.
4. The server checks the catalogue, price and spending rules, then creates a proposal.
5. A person approves that exact proposal.
6. The server places the order.
7. The server creates a ticket with the item, quantity, price, order reference and status.
8. The person gets a confirmation that includes the ticket.

**Rules**

- The AI only proposes. Application code validates the proposal and places the order.
- Approval is tied to the exact product, quantity and price shown. A changed proposal must be approved again.
- Asking twice must not create a second order or a second ticket. The existing one is reused.
- A recognised voice does not give anyone the right to buy.
- Orders are simulated by default. Do not say an order was placed unless a supplier confirmed it.

**What works today**

- Typed requests, or browser voice input where supported, through to interpretation and a proposal
- Approval tied to the exact proposal
- Duplicate protection
- A simulated order reference

**What is missing**

- **The ticket for orders.** Today a request is the only record. Orders end with the status `ordered`, and only maintenance requests are described as tickets, with the status `maintenance_open`. Those are local records.
- Speech to text on `main`. The `air-quality-stt` branch sends transcripts from the laptop microphone to the server. The board's microphone is not used.
- A real supplier. Real orders need separate work and explicit permission.

## Open questions

- **CO₂ sensor:** Are we buying one, or showing a clearly labelled simulated reading in the demo?
- **Ticket:** Is a local ticket in the dashboard enough, or should it also go to an outside system? Sending it elsewhere needs explicit permission.
- **Answers:** Is the laptop voice enough for the demo, or should answers also come from the board's screen or speaker?
