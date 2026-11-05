"""Offline scripted model (OFFICE_MODEL=mock). Keyword matching only; for tests and no-key development.
Not a claim of intelligence: say "mock mode" when demoing with it."""

import re

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo

from office import config


def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    last = messages[-1]
    parts = last.parts if isinstance(last, ModelRequest) else []
    returns = [p for p in parts if isinstance(p, ToolReturnPart)]
    prompts = [p.content for p in parts if isinstance(p, UserPromptPart) and isinstance(p.content, str)]
    if returns and not prompts:
        return ModelResponse(parts=[TextPart(str(returns[-1].content))])

    text = " ".join(prompts).lower()
    tools = {t.name for t in info.function_tools}
    if "create_ticket" in tools and re.search(r"broken|leak|not working|jammed|stuck", text):
        return ModelResponse(parts=[ToolCallPart("create_ticket", {"summary": " ".join(prompts)})])
    for pid in config.PRODUCTS:
        keyword = pid.replace("-", " ")
        if keyword in text and "place_order" in tools:
            qty = int(m.group(1)) if (m := re.search(rf"\b(\d{{1,2}})\s+(?:x\s+)?{re.escape(keyword)}", text)) else 1
            return ModelResponse(parts=[ToolCallPart("place_order", {"product_id": pid, "quantity": qty})])
    return ModelResponse(parts=[TextPart("Mock mode: I can order catalogue items or log something broken.")])
