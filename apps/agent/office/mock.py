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
        history = next((p.content for p in returns if p.tool_name == "read_environment_history"), None)
        if isinstance(history, list):
            poor = [r for r in history if r.get("simulation") and r.get("metric") == "co2_ppm" and r.get("value", 0) >= 1500]
            if len({r["timestamp"] for r in poor}) >= 2:
                return ModelResponse(parts=[TextPart("Mock demo: two earlier simulated checks showed high CO₂ here. Check current conditions before meeting; no booking made.")])
            return ModelResponse(parts=[TextPart("Mock demo: insufficient simulated environmental history for this location. No meeting booked.")])
        return ModelResponse(parts=[TextPart(str(returns[-1].content))])

    text = " ".join(prompts).lower()
    tools = {t.name for t in info.function_tools}
    if "read_environment_history" in tools and re.search(r"\bmeeting\b", text):
        if re.search(r"\bstage\b|\bkitchen\b", text):
            board_id = "kitchen"
        else:
            board_id = next((b for b in config.BOARDS if re.search(rf"\b{re.escape(b)}\b", text)), None)
        return ModelResponse(parts=[ToolCallPart("read_environment_history", {"board_id": board_id})])
    if "office_status" in tools and re.search(r"\b(status|update)\b", text):
        return ModelResponse(parts=[ToolCallPart("office_status", {})])
    if "create_ticket" in tools and re.search(r"broken|leak|not working|jammed|stuck", text):
        return ModelResponse(parts=[ToolCallPart("create_ticket", {"summary": " ".join(prompts)})])
    for pid in config.PRODUCTS:
        keyword = pid.replace("-", " ")
        if keyword in text and "place_order" in tools:
            qty = int(m.group(1)) if (m := re.search(rf"\b(\d{{1,2}})\s+(?:x\s+)?{re.escape(keyword)}", text)) else 1
            return ModelResponse(parts=[ToolCallPart("place_order", {"product_id": pid, "quantity": qty})])
    return ModelResponse(parts=[TextPart("Mock mode: request received. General conversation and integration tools are available with the live agent; no action taken.")])
