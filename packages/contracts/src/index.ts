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
export const AssessmentSchema = z.object({
  severity: z.enum(["routine", "urgent", "critical"]),
  confidence: z.number().finite().min(0).max(1),
  requires_confirmation: z.boolean(),
  reason: z.string().min(1).max(500),
});
export type Assessment = z.infer<typeof AssessmentSchema>;
export const InterpretationSchema = z.object({
  intent: z.enum(["purchase", "maintenance", "clarification"]),
  item_id: z.string().nullable(),
  summary: z.string().min(1).max(500),
  quantity: z.number().int().min(1).max(20),
  question: z.string().nullable(),
  assessment: AssessmentSchema.optional(),
});
export type Interpretation = z.infer<typeof InterpretationSchema>;
export type Product = {
  id: string;
  name: string;
  station_id: z.infer<typeof StationId>;
  price_nok: number;
  delivery_estimate: string;
  supplier: string;
  fulfillment_kind?: "goods" | "service";
};
export const OfferSchema = z.object({
  id: z.string().min(1).max(128),
  item_id: z.string().min(1).max(128),
  station_id: StationId,
  name: z.string().min(1).max(200),
  supplier: z.string().min(1).max(200),
  unit_price_nok: z.number().finite().positive().multipleOf(0.01),
  shipping_nok: z.number().finite().nonnegative().multipleOf(0.01),
  delivery_hours: z.number().int().min(0).max(8760),
  quality_score: z.number().min(0).max(100),
  quality_basis: z.string().min(1).max(500),
  available: z.boolean(),
  fulfillment_kind: z.enum(["goods", "service"]),
  source: z.literal("fixture"),
  expires_at: z.string().datetime(),
  simulation: z.literal(true),
});
export type Offer = z.infer<typeof OfferSchema>;
export type RankingWeights = { price: number; quality: number; delivery: number };
export type RankedOffer = {
  offer: Offer;
  total_nok: number;
  score: number;
  components: RankingWeights;
};
export type Sourcing = {
  weights: RankingWeights;
  selected_offer_id: string;
  reason: string;
  alternatives: RankedOffer[];
  rejected: { offer_id: string; reason: string }[];
};
export const ResearchSchema = z.object({
  title: z.string().min(1).max(300),
  url: z.string().url().refine((url) => url.startsWith("https://")),
  snippet: z.string().max(1500),
  source: z.literal("tavily"),
  executable: z.literal(false),
  marketplace: z.literal("finn").optional(),
});
export type Research = z.infer<typeof ResearchSchema>;
export type Proposal = {
  kind: "purchase";
  product: Product;
  quantity: number;
  total_nok: number;
  currency: "NOK";
  simulation: true;
  fulfillment_kind?: "goods" | "service";
  offer_id?: string;
  expires_at?: string;
  sourcing?: Sourcing;
  quote_revision?: number;
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
  research?: Research[];
  assessment?: Assessment;
  approval_required?: boolean;
};
export type Alert = {
  id: string; request_id: string; severity: "urgent" | "critical";
  title: string; detail: string; created_at: string; acknowledged_at: string | null;
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
export const SensorEventSchema = z.strictObject({
  event_id: z.string().min(1).max(128),
  station_id: StationId,
  rule_id: z.string().min(1).max(128),
  value: z.number().finite().nonnegative(),
  timestamp: z.string().datetime(),
});
export type SensorEvent = z.infer<typeof SensorEventSchema>;
export type SensorResponse = {
  triggered: boolean;
  reason: string;
  request: OfficeRequest | null;
  duplicate: boolean;
};
// This exact snapshot must be presented by the UI and approved unchanged.
export function proposalFingerprint(proposal: Proposal): string {
  return JSON.stringify(proposal);
}
