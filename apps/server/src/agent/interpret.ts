import {
  type Interpretation,
  type Station,
  type OfficeRequest,
} from "@office/contracts";
export type AgentProgress = { stage: string; detail: string };
export type InterpreterContext = {
  openRequests?: OfficeRequest[];
  onProgress?: (step: AgentProgress) => void;
};
export type Interpreter = (
  text: string,
  station: Station,
  context?: InterpreterContext,
) => Promise<Interpretation>;
export const mockInterpret: Interpreter = async (text, station) => {
  const t = text.toLowerCase();
  if (
    /broken|leak|jammed|not working|ødelagt|lekker|virker ikke|stuck|tett/.test(
      t,
    )
  )
    return {
      intent: "maintenance",
      item_id: null,
      summary: text,
      quantity: 1,
      question: null,
    };
  const aliases: Record<string, RegExp> = {
    coffee: /coffee|kaffe/,
    cups: /cups|kopper|krus/,
    "toilet-paper": /toilet paper|toilet roll|dopapir|toalettpapir/,
    soap: /soap|såpe/,
    "printer-paper": /printer paper|a4|skriverpapir/,
    toner: /toner/,
  };
  let item = Object.keys(aliases).find((id) => aliases[id].test(t));
  if (!item && /paper|papir/.test(t))
    item =
      station.id === "bathroom"
        ? "toilet-paper"
        : station.id === "printer"
          ? "printer-paper"
          : undefined;
  if (
    item &&
    station.item_ids.includes(item) &&
    !/weird|strange|rar|smak/.test(t)
  )
    return {
      intent: "purchase",
      item_id: item,
      summary: text,
      quantity: Number(t.match(/\b(\d+)\b/)?.[1] ?? 1),
      question: null,
    };
  return {
    intent: "clarification",
    item_id: null,
    summary: text,
    quantity: 1,
    question: `What do you need at ${station.name}? Please name the supply or describe what is broken.`,
  };
};
export { anthropicInterpret, createAnthropicInterpreter } from "./anthropic.js";
