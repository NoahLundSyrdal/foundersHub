import { z } from "zod";
export const StationId = z.enum(["kitchen", "bathroom", "printer"]);
export const DeviceEventSchema = z
  .object({
    event_id: z.string().min(1).max(128),
    station_id: StationId,
    type: z.enum(["button_pressed", "speech_transcript", "stock_low"]),
    timestamp: z.string().datetime(),
    payload: z
      .object({
        text: z.string().min(1).max(2000).optional(),
        item_id: z.string().max(128).optional(),
        level: z.number().min(0).max(100).optional(),
      })
      .default({}),
  })
  .superRefine((event, ctx) => {
    if (event.type === "speech_transcript" && !event.payload.text)
      ctx.addIssue({
        code: "custom",
        message: "Speech events require text",
        path: ["payload", "text"],
      });
    if (event.type === "stock_low" && !event.payload.item_id)
      ctx.addIssue({
        code: "custom",
        message: "Stock events require item_id",
        path: ["payload", "item_id"],
      });
  });
export type DeviceEvent = z.infer<typeof DeviceEventSchema>;
export const InterpretationSchema = z.object({
  intent: z.enum(["purchase", "maintenance", "clarification"]),
  item_id: z.string().nullable(),
  summary: z.string().min(1).max(500),
  quantity: z.number().int().min(1).max(20),
  question: z.string().nullable(),
});
export type Interpretation = z.infer<typeof InterpretationSchema>;
export type Product = {
  id: string;
  name: string;
  station_id: z.infer<typeof StationId>;
  price_nok: number;
  delivery_estimate: string;
  supplier: string;
};
export type Proposal = {
  kind: "purchase";
  product: Product;
  quantity: number;
  total_nok: number;
  currency: "NOK";
  simulation: true;
};
export type RequestStatus =
  | "processing"
  | "awaiting_input"
  | "awaiting_approval"
  | "needs_clarification"
  | "maintenance_open"
  | "ordered"
  | "resolved"
  | "failed";
export type DeviceCommand = {
  station_id: z.infer<typeof StationId>;
  text: string;
  color: "blue" | "amber" | "green" | "red";
};
export type OfficeRequest = {
  id: string;
  station_id: z.infer<typeof StationId>;
  user_id: string | null;
  original_text: string;
  intent: Interpretation["intent"] | null;
  status: RequestStatus;
  summary: string;
  question: string | null;
  item_id: string | null;
  proposal: Proposal | null;
  supplier_reference: string | null;
  simulation: true;
  assigned_to: string | null;
  created_at: string;
  updated_at: string;
  trace?: { stage: string; detail: string; at: string }[];
};
export type EventResponse = {
  request: OfficeRequest;
  duplicate: boolean;
  command: DeviceCommand;
};
export type Station = {
  id: z.infer<typeof StationId>;
  name: string;
  responsible_team: string;
  equipment: string[];
  item_ids: string[];
};
export const TranscriptSchema = z.object({
  text: z.string().trim().min(1).max(2000),
});
export const ApprovalSchema = z.object({
  proposal_fingerprint: z.string().min(1),
});
// This exact snapshot must be presented by the UI and approved unchanged.
export function proposalFingerprint(proposal: Proposal): string {
  return JSON.stringify(proposal);
}
