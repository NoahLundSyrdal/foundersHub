import test from "node:test";
import assert from "node:assert/strict";
import type { ToolUseBlock } from "@anthropic-ai/sdk/resources/messages";
import {
  createAnthropicInterpreter,
  agentTools,
  type AgentTransport,
} from "./anthropic.js";
import type { AgentProgress } from "./interpret.js";
import { stations } from "../config.js";
const station = stations.find((s) => s.id === "kitchen")!;
const decision = {
  intent: "purchase",
  item_id: "coffee",
  summary: "Coffee is out of stock",
  quantity: 1,
  question: null,
};
const call = (name: string, input: unknown): ToolUseBlock => ({
  type: "tool_use",
  id: `tool-${name}`,
  name,
  input,
  caller: { type: "direct" },
});
function script(
  turns: ToolUseBlock[][],
  inspect?: (params: Parameters<AgentTransport>[0]) => void,
): AgentTransport {
  let index = 0;
  return async (params, { onToolStart }) => {
    inspect?.(params);
    const content = turns[index++];
    if (!content) throw new Error("Unexpected extra turn");
    for (const c of content) onToolStart(c.name);
    return { content, stop_reason: "tool_use" };
  };
}
test("Claude tools are strict and schema-derived; purchase loop gets scoped evidence", async () => {
  assert.ok(
    agentTools.every(
      (t) =>
        t.strict &&
        t.input_schema.type === "object" &&
        t.input_schema.additionalProperties === false,
    ),
  );
  const history: AgentProgress[] = [];
  let lastMessages: unknown;
  const interpret = createAnthropicInterpreter(
    script(
      [
        [call("search_catalogue", { query: "coffee" })],
        [call("get_office_policy", {})],
        [call("list_open_requests", {})],
        [call("submit_decision", decision)],
      ],
      (params) => {
        lastMessages = JSON.parse(JSON.stringify(params.messages));
        assert.equal(params.tool_choice?.type, "auto");
      },
    ),
  );
  const result = await interpret("We need coffee", station, {
    onProgress: (s) => history.push(s),
  });
  assert.equal(result.item_id, "coffee");
  assert.equal(history.at(-1)?.stage, "agent.completed");
  const captured = JSON.stringify(lastMessages);
  assert.match(captured, /fixture_catalogue/);
  assert.doesNotMatch(captured, /Toilet paper/);
  assert.ok(history.every((s) => !s.detail.includes("We need coffee")));
});
test("purchase without consulted tools is rejected and can recover to clarification", async () => {
  const steps: AgentProgress[] = [];
  const interpret = createAnthropicInterpreter(
    script([
      [call("submit_decision", decision)],
      [
        call("submit_decision", {
          intent: "clarification",
          item_id: null,
          summary: "Need more details",
          quantity: 1,
          question: "Which supply do you need?",
        }),
      ],
    ]),
  );
  const result = await interpret("paper", station, {
    onProgress: (s) => steps.push(s),
  });
  assert.equal(result.intent, "clarification");
  assert.ok(steps.some((s) => s.stage === "tool.rejected"));
});
test("cross-station product cannot be submitted as purchase", async () => {
  let rejected = false;
  const interpret = createAnthropicInterpreter(
    script(
      [
        [
          call("search_catalogue", { query: "" }),
          call("get_office_policy", {}),
          call("list_open_requests", {}),
        ],
        [call("submit_decision", { ...decision, item_id: "toilet-paper" })],
        [
          call("submit_decision", {
            intent: "clarification",
            item_id: null,
            summary: "Item unavailable",
            quantity: 1,
            question: "Do you mean a kitchen supply?",
          }),
        ],
      ],
      (params) => {
        if (
          JSON.stringify(params.messages).includes(
            "Purchase item must have been returned",
          )
        )
          rejected = true;
      },
    ),
  );
  assert.equal((await interpret("Get paper", station)).intent, "clarification");
  assert.equal(rejected, true);
});
test("endless tool requests stop after five turns", async () => {
  let turns = 0;
  const interpret = createAnthropicInterpreter(async () => {
    turns++;
    return {
      content: [call("get_office_policy", {})],
      stop_reason: "tool_use",
    };
  });
  await assert.rejects(interpret("coffee", station), /five-turn budget/);
  assert.equal(turns, 5);
});
test("unknown tools never execute and malformed quantities fail validation", async () => {
  const steps: AgentProgress[] = [];
  const interpret = createAnthropicInterpreter(
    script([
      [
        call("place_order", { card: "fake" }),
        call("submit_decision", { ...decision, quantity: 1000 }),
      ],
      [
        call("submit_decision", {
          intent: "maintenance",
          item_id: null,
          summary: "Coffee machine needs inspection",
          quantity: 1,
          question: null,
        }),
      ],
    ]),
  );
  const result = await interpret("Broken machine", station, {
    onProgress: (s) => steps.push(s),
  });
  assert.equal(result.intent, "maintenance");
  assert.equal(steps.filter((s) => s.stage === "tool.rejected").length, 2);
});
test("truncated streamed response cannot execute even a complete-looking tool", async () => {
  const interpret = createAnthropicInterpreter(async () => ({
    content: [
      call("submit_decision", {
        intent: "maintenance",
        item_id: null,
        summary: "Broken",
        quantity: 1,
        question: null,
      }),
    ],
    stop_reason: "max_tokens",
  }));
  await assert.rejects(interpret("broken", station), /turn budget/);
});

test("strict API quantity schema omits unsupported implicit integer bounds", () => {
  const tool = agentTools.find((tool) => tool.name === "submit_decision")!;
  const quantity = (
    tool.input_schema.properties as Record<string, Record<string, unknown>>
  ).quantity;
  assert.equal(quantity.type, "integer");
  assert.equal(quantity.minimum, undefined);
  assert.equal(quantity.maximum, undefined);
});
