# Pydantic AI procurement worker

This Python subprocess interprets one office request and optionally discovers supplier links. Leo’s FastAPI application in apps/agent owns SQLite, approval and simulated execution; it calls this package directly for structured assessment and discovery and reuses its planning helpers for ranking. The JSON subprocess remains available for standalone testing. The worker has no purchase, payment, booking, shell, file-write or unrestricted MCP tool.

Python **3.11+** is required. The worker was verified locally on Python 3.14.5 with Pydantic AI 2.66.0, Pydantic 2.14.3, official Anthropic SDK 2.5.0 and HTTPX2 2.16.0. The direct Pydantic AI dependency is pinned in `pyproject.toml`.

```sh
python3 -m venv apps/procurement/.venv
apps/procurement/.venv/bin/python -m pip install -e apps/procurement
apps/procurement/.venv/bin/python -m unittest discover -s apps/procurement/tests -v
```

Run `apps/procurement/.venv/bin/python -m office_procurement` with one JSON document on stdin. It returns one JSON document on stdout and exits. The parent must close stdin. Input is limited to 128 KB; the server should also cap output and enforce its own timeout. Invalid input/provider failures return a fixed `error` and nonzero exit code without echoing secrets or request content. This is not a standalone HTTP service.

Input:

```json
{
  "text": "We need 2 coffee",
  "station": {
    "id": "kitchen", "name": "Kitchen", "responsible_team": "Facilities",
    "equipment": ["coffee machine"], "item_ids": ["coffee"]
  },
  "catalogue": [{
    "id": "coffee", "name": "Office coffee 1 kg", "station_id": "kitchen",
    "price_nok": 189, "delivery_estimate": "Demo tomorrow", "supplier": "Demo Supplies"
  }],
  "policy": {"max_order_nok": 1000, "require_approval": true, "preferences": []},
  "open_requests": [],
  "provider": "mock"
}
```

Output contains `interpretation`, `research` and `progress`. Interpretation is `{intent,item_id,summary,quantity,question,assessment}`. The required `assessment` is `{severity:"routine"|"urgent"|"critical",confidence:0..1,requires_confirmation:boolean,reason:string}`. The same structured Pydantic AI response classifies the need, business severity and confirmation requirement; there is no separate assessment model call. Research is a maximum of ten `{title,url,snippet,source:"tavily",executable:false}` records, optionally with `marketplace:"finn"`; no prices or ratings are extracted from snippets. Progress contains concise operational `{stage,detail}` records, including a severity/confirmation summary, never hidden model reasoning.

Routine, well-specified replenishment can return `requires_confirmation:false`; office policy can still require approval. Urgent means material disruption or repair, while critical means a reported immediate safety hazard. Critical assessments and confidence below 0.8 require confirmation by schema validation. Instructions also require confirmation for hazardous/safety work, repairs and air-quality service scope. Clarifications always need confirmation. Urgency never grants spending authority. Reasons are concise operational explanations rather than private reasoning. Mock tests cover normal coffee, ambiguous needs, low CO2, air-quality repair, gas/fire/smoke/flooding/electric shock, and explicit plumber/printer-technician requests. The deterministic hazard keyword fallback is a demo aid, not a diagnosis or safety monitor.

`provider:"mock"` needs no API key. `provider:"anthropic"` uses Pydantic AI with the official Anthropic SDK; it needs `ANTHROPIC_API_KEY`. Optional input `model` or environment `ANTHROPIC_MODEL` selects the account-supported model. Default is the repository's `claude-sonnet-5-5`; account availability was not verified. The run has a 35-second total timeout, 15-second model request timeout, five model turns, ten tool calls and a 12,000-token ceiling. The model only receives four read-only tools: station catalogue, policy, open station requests and supplier discovery. Application code must revalidate all interpretations before execution.

`TAVILY_API_KEY` enables live discovery against the fixed `https://api.tavily.com/search` endpoint. Queries contain an approved catalogue name and `Norway supplier price delivery`; raw transcripts, open requests, personal details and office addresses are not sent to Tavily. Search runs at basic depth, with at most five results per source, at most two source calls per approved item, three network calls total per run, eight-second network timeout and ten retained links. Search failure retains approved demo offers and adds a sanitized progress record. Model-free tests use an injected HTTP transport and never call Tavily. No live search was performed during verification.

`FINN_DISCOVERY_ENABLED=true` is the default when Tavily is configured. It preserves general supplier discovery and adds a second Tavily request with `include_domains:["finn.no"]`. Only HTTPS result hosts equal to `finn.no` or a subdomain are retained for this source, tagged `marketplace:"finn"`; lookalike or unrelated domains are rejected. Set the variable to `false` to disable that source. FINN links remain unverified and non-executable. [FINN's API](https://www.finn.no/api/) and [API help](https://hjelpesenter.finn.no/hc/no/articles/22629030477202-API) describe business-partner access for managing one's own ads, not a public buyer checkout. This worker does not use internal FINN endpoints, scrape pages, contact sellers, connect a FINN account or place purchases.

Search snippets are untrusted discovery evidence. They are not verified prices, stock, quality ratings, delivery promises, executable quotes or confirmed contractor availability. They never become approved catalogue entries automatically. To execute a real product or service, add a trusted supplier adapter that obtains a current structured quote including exact product/service scope, taxes, fees, total, expiry and availability, then let server policy and exact approval/idempotency checks authorize execution. Enabling live interpretation or search does not enable real purchasing.

For repairs, include the approved service category in both catalogue and station `item_ids` with `fulfillment_kind:"service"`. The deterministic mock recognizes `ventilation-service`, `plumbing-service` and `printer-service`; unknown maintenance becomes a local team ticket. Ambiguous supplies and multiple recognized supply needs require clarification. Low CO2 alone does not imply a ventilation fault; numeric sensor policy belongs to the server, not the language model.

Research informed this choice: Pydantic AI supports [typed structured output](https://pydantic.dev/docs/ai/core-concepts/output/), [bounded agent usage](https://pydantic.dev/docs/ai/core-concepts/agent/#usage-limits), [TestModel verification](https://pydantic.dev/docs/ai/guides/testing/), and [the official Anthropic provider](https://pydantic.dev/docs/ai/models/anthropic/). [Tavily Search](https://docs.tavily.com/documentation/api-reference/endpoint/search) offers explicit depth/result controls. This small fixed search adapter is sufficient for the current discovery task. Pydantic AI also supports [MCP clients](https://pydantic.dev/docs/ai/mcp/client/), but no arbitrary MCP configuration is loaded here: exposing an entire commerce server could expose write tools. A later MCP integration should explicitly wrap approved read-only catalogue/quote tools, restrict server URLs and keep execution in Fastify's supplier adapter.
