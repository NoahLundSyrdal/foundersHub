# Use cases

The office assistant has two main use cases. Both start with someone speaking to it.

| Use case | Example | Result |
| --- | --- | --- |
| 1. CO₂ checker | "Is it bad air quality here or what?" | A spoken or on-screen answer based on a real reading |
| 2. Order by voice | "We need coffee" | A processed order and a ticket |

Neither use case works end to end yet. Each one lists what is missing.

## 1. CO₂ checker

Someone asks about the air in the room and gets a plain answer.

**Example questions**

- "Is it bad air quality here or what?"
- "Is the air okay in here?"
- "Should we open a window?"

**Flow**

1. A person presses the station button and asks the question.
2. Speech is turned into text.
3. The assistant recognises this as an air quality question, not an order.
4. It reads the latest CO₂ value.
5. It answers with the level and what to do, for example: "CO₂ is 1600 ppm, which is poor. Open a window."

**How to judge the reading**

| CO₂ (ppm) | Rating | Answer |
| --- | --- | --- |
| Under 800 | Fresh | The air is fine |
| 800–1200 | Acceptable | The air is okay |
| 1200–1500 | Getting stuffy | Consider opening a window |
| 1500–2000 | Poor | Open a window; people may feel sleepy or distracted |
| Over 2000 | Uncomfortable | Air the room now |

These follow the bands in the CO₂ section of [the component guide](components.md). That guide leaves 1200–1500 undefined, so "Getting stuffy" is our own label.

**Rules**

- Only state a level that came from a real reading. If there is no fresh reading, say so.
- Label any simulated reading as simulated.
- The sensor reads the air where it sits. "Here" only means the room the sensor is in.
- The sensor returns 0 for about 5 seconds after boot. Treat that as no reading, not as clean air.

**What is missing**

- **The CO₂ sensor.** [Our kit](components.md#our-kit-counted-from-photos-2026-11-05) does not include one. We need the Atech CO₂ module (SCD40, 469 NOK). The temperature and humidity module cannot measure CO₂.
- Firmware that reads the sensor and sends the value to the server.
- A new request type for questions. Today the assistant only handles `purchase`, `maintenance` and `clarification`. This changes `packages/contracts`, so agree it with the team first.
- A way to give the answer back. The Screen is on the board, but the Speaker is still loose and spoken answers are not built.
- Speech to text on the device path (see below).

## 2. Order by voice

Someone asks for something. The assistant understands the request, processes the order and creates a ticket.

**Example requests**

- "We need coffee"
- "We are out of paper" (the station decides if this means toilet paper or printer paper)

**Flow**

1. A person presses the station button and says what they need.
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
- Speech to text from the board's microphone into the server. The speech work is in progress on a separate task and is not in `main` yet.
- A real supplier. Real orders need separate work and explicit permission.

## Open questions

- **CO₂ sensor:** Are we buying one, or showing a clearly labelled simulated reading in the demo?
- **Ticket:** Is a local ticket in the dashboard enough, or should it also go to an outside system? Sending it elsewhere needs explicit permission.
- **Answers:** Should the assistant speak its answers, or is the screen enough?
