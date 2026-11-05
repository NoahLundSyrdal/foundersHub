import { z } from "zod";

const Timestamp = z.string().datetime({ offset: true });
export const RoomEnvironmentSnapshotSchema = z.strictObject({
  board_id: z.string().min(1).max(64).nullable(), evaluated_at: Timestamp,
  basis: z.enum(["physical", "synthetic"]).optional(), simulation: z.boolean().optional(),
  readings: z.array(z.strictObject({
    kind: z.enum(["temperature_c", "humidity_pct", "co2_ppm"]),
    value: z.number().finite().nullable(), last_value: z.number().finite().nullable(),
    at: Timestamp.nullable(), state: z.enum(["fresh", "stale", "missing", "demo"]),
  })),
});
export const RoomEnvironmentHistorySchema = z.strictObject({
  state: z.enum(["insufficient", "usually_hot", "no_hot_pattern"]),
  basis: z.enum(["physical", "synthetic", "none"]).optional(), simulation: z.boolean().optional(),
  dataset_id: z.string().nullable().optional(),
  days: z.number().int().min(0), hot_days: z.number().int().min(0),
  hot_fraction: z.number().min(0).max(1).nullable(), required_days: z.number().int().min(1),
  timezone: z.string(), window_days: z.number().int().min(1), weekday_class: z.enum(["weekday", "weekend"]),
  qualified_days: z.array(z.strictObject({
    date: z.string(), average_temperature_c: z.number().finite(), samples: z.number().int().min(0),
    bins: z.number().int().min(0), coverage_minutes: z.number().min(0), hot: z.boolean(),
  })), reason: z.string(),
});
export const RoomEnvironmentAdviceSchema = z.strictObject({
  current: RoomEnvironmentSnapshotSchema, history: RoomEnvironmentHistorySchema,
  recommendation: z.enum(["avoid", "caution", "suitable", "unknown"]), summary: z.string(),
});
export type RoomEnvironmentSnapshot = z.infer<typeof RoomEnvironmentSnapshotSchema>;
export type RoomEnvironmentAdvice = z.infer<typeof RoomEnvironmentAdviceSchema>;
export type RoomEnvironment = { current: RoomEnvironmentSnapshot } | RoomEnvironmentAdvice;

const DemoBoard = z.enum(["kitchen", "bathroom", "printer"]);
export const RoomDemoRequestSchema = z.strictObject({ board_id: DemoBoard, action: z.enum(["seed", "clear"]) });
export const RoomDemoDatasetSchema = z.strictObject({
  dataset_id: z.string(), board_id: DemoBoard, simulation: z.literal(true), samples: z.number().int().min(1),
  days: z.literal(28), seeded_at: Timestamp, label: z.string(), timezone: z.string(), local_date: z.string(),
  date_range: z.strictObject({ start: z.string(), end: z.string() }), interval_minutes: z.literal(10),
  current_values: z.strictObject({ temperature_c: z.number().min(-40).max(125),
    humidity_pct: z.number().min(0).max(100), co2_ppm: z.number().min(1).max(100000) }),
});
export const RoomDemoClearResultSchema = z.strictObject({
  board_id: DemoBoard, simulation: z.literal(true), cleared: z.boolean(), dataset_id: z.string().nullable(), samples: z.number().int().min(0),
});
export type RoomDemoRequest = z.infer<typeof RoomDemoRequestSchema>;
export type RoomDemoDataset = z.infer<typeof RoomDemoDatasetSchema>;
