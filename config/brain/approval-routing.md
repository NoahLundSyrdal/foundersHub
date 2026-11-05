# Approval routing

Who approves what. The office agent reads this table; application code (not the model) routes each approval to the person named here. The approver must exist in `config/people.json`. Approval happens on the dashboard, which has no login yet: the name says who should press the button.

| Approval type | Approver | When |
|---|---|---|
| purchase | Leo | An order above the speaker's own limit, up to the office cap |
| purchase_over_cap | Leo | Any order above the office cap (1,000 NOK per order). Always a human, whoever asks |
| contractor | Leo | Booking a technician, plumber or any paid repair |
| default | Leo | Anything not listed above |
