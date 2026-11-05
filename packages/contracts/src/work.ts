import { z } from "zod";

const Text = z.string().trim().min(1).max(2000);
const Key = z.string().trim().min(1).max(128);
const Timestamp = z.string().datetime({ offset: true });
export const WorkPrioritySchema = z.enum(["routine", "urgent", "critical"]);
export const WorkStatusSchema = z.enum(["new", "needs_information", "ready", "awaiting_approval", "active", "waiting", "blocked", "completed", "cancelled", "failed"]);
export const WorkStepCreateSchema = z.strictObject({
  id: Key, title: Text, owner: Key,
  depends_on: z.array(Key).max(30).default([]),
  required_evidence: z.array(Key).min(1).max(20).default(["confirmation"]),
  action_kind: z.enum(["human", "local", "purchase", "message", "booking", "access"]).default("human"),
  action_snapshot: z.record(z.string(), z.unknown()).default({}),
  requires_approval: z.boolean().default(false),
});
export const WorkCreateSchema = z.strictObject({
  event_key: Key, title: Text, outcome: Text,
  owner: Key.default("office-coordinator"), template_key: Key.default("custom"),
  context: z.record(Key, Text).default({}), requester_id: Key.nullable().default(null),
  priority: WorkPrioritySchema.default("routine"),
  due_at: Timestamp.nullable().default(null), next_review_at: Timestamp.nullable().default(null),
  escalation_owner: Key.default("office-coordinator"), simulation: z.boolean().default(true),
  steps: z.array(WorkStepCreateSchema).min(1).max(30).nullable().optional(),
});
export const WorkActionSchema = z.strictObject({
  event_key: Key, expected_version: z.number().int().min(1),
  action: z.enum(["assign", "wait", "block", "resume", "cancel", "fail", "start_step", "complete_step", "simulate_step", "reconcile_step", "renew_approval", "close"]),
  step_id: Key.nullable().optional(), owner: Key.nullable().optional(), reason: Text.nullable().optional(),
  next_review_at: Timestamp.nullable().optional(), reference: Text.nullable().optional(),
  result: z.enum(["confirmed", "failed", "uncertain"]).nullable().optional(),
});
export const WorkEvidenceCreateSchema = z.strictObject({
  event_key: Key, expected_version: z.number().int().min(1), step_id: Key, kind: Key,
  summary: Text, source: Text, recorded_by: Key,
});
export const WorkDecisionSchema = z.strictObject({
  event_key: Key, expected_version: z.number().int().min(1), step_id: Key,
  approve: z.boolean(), proposal_fingerprint: Key,
});
export const WorkEvidenceSchema = z.strictObject({
  id: Key, step_id: Key, kind: Key, summary: Text, source: Text,
  recorded_by: Key, created_at: Timestamp, simulation: z.boolean(),
});
export const WorkStepSchema = WorkStepCreateSchema.extend({
  status: z.enum(["pending", "active", "awaiting_approval", "waiting", "blocked", "completed", "failed", "uncertain"]),
  approval: z.strictObject({
    fingerprint: Text, status: z.enum(["pending", "approved", "rejected"]),
    expires_at: Timestamp, decided_by: Key.nullable(),
  }).nullable(),
  execution: z.strictObject({
    id: Key, status: z.enum(["started", "confirmed", "failed", "uncertain"]), reference: Text.nullable(),
  }).nullable(),
});
export const WorkItemSchema = z.strictObject({
  id: Key, event_key: Key, template_key: Key, title: Text, outcome: Text, owner: Key,
  context: z.record(Key, Text), requester_id: Key.nullable(), priority: WorkPrioritySchema,
  status: WorkStatusSchema, due_at: Timestamp.nullable(), next_review_at: Timestamp.nullable(),
  waiting_reason: Text.nullable(), escalation_owner: Key, simulation: z.boolean(),
  version: z.number().int().min(1), created_at: Timestamp, updated_at: Timestamp,
  steps: z.array(WorkStepSchema), evidence: z.array(WorkEvidenceSchema),
  history: z.array(z.strictObject({ at: Timestamp, action: Key, detail: Text })),
  links: z.record(z.string(), z.string()),
});
export const WorkTemplateSchema = z.strictObject({ key: Key, title: Text, description: Text, steps: z.array(WorkStepCreateSchema) });
export const WorkNotificationSchema = z.strictObject({
  id: Key, work_id: Key, kind: z.enum(["reminder", "escalation"]), target: Key, detail: Text,
  created_at: Timestamp, acknowledged_at: Timestamp.nullable(), delivery: z.literal("local_only"),
});
export type WorkItem = z.infer<typeof WorkItemSchema>;
export type WorkStep = z.infer<typeof WorkStepSchema>;
export type WorkTemplate = z.infer<typeof WorkTemplateSchema>;
export type WorkNotification = z.infer<typeof WorkNotificationSchema>;
export type WorkCreate = z.infer<typeof WorkCreateSchema>;
export type WorkAction = z.infer<typeof WorkActionSchema>;
export type WorkEvidenceCreate = z.infer<typeof WorkEvidenceCreateSchema>;
export type WorkDecision = z.infer<typeof WorkDecisionSchema>;
export const WorkRoutineCreateSchema = z.strictObject({
  event_key: Key, name: Text, timezone: Key, local_time: z.string().regex(/^(?:[01]\d|2[0-3]):[0-5]\d$/),
  weekdays: z.array(z.number().int().min(0).max(6)).min(1).max(7).default([0, 1, 2, 3, 4, 5, 6]),
  starts_on: z.string().regex(/^\d{4}-\d{2}-\d{2}$/),
  missed_policy: z.enum(["skip", "latest"]).default("latest"), overlap_policy: z.enum(["link", "allow"]).default("link"),
  work: WorkCreateSchema, enabled: z.boolean().default(true),
  review_interval_hours: z.number().int().min(1).max(168).default(24), max_reminders: z.number().int().min(0).max(5).default(2),
});
export const WorkRoutineSchema = WorkRoutineCreateSchema.extend({
  id: Key, next_run_at: Timestamp.nullable(), last_occurrence_at: Timestamp.nullable(), created_at: Timestamp,
});
export type WorkRoutine = z.infer<typeof WorkRoutineSchema>;
export type WorkRoutineCreate = z.infer<typeof WorkRoutineCreateSchema>;
