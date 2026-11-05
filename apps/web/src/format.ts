import type { OfficeRequest } from "@office/contracts";

export const statusLabels: Record<OfficeRequest["status"], string> = {
  checkout_ready: "Ready for Oda checkout",
  processing: "Agent working",
  awaiting_input: "Waiting for details",
  awaiting_approval: "Needs approval",
  needs_clarification: "Needs clarification",
  maintenance_open: "Maintenance open",
  ordered: "Order simulated",
  resolved: "Resolved",
  failed: "Failed",
};
/** Which dashboard colour field a status belongs to. */
export const statusTone = (status: OfficeRequest["status"]) =>
  status === "processing"
    ? "working"
    : status === "failed"
      ? "failed"
      : status === "ordered" || status === "resolved"
        ? "done"
        : "waiting";
/** Who has the ball right now. */
export const loopLabel = (request: OfficeRequest) =>
  ({
    processing: "Agent is working",
    awaiting_input: "Waiting for details",
    awaiting_approval: "Waiting for your approval",
    needs_clarification: "Waiting for your answer",
    maintenance_open: `With ${request.assigned_to ?? "the team"}`,
    ordered: "Waiting for delivery",
    checkout_ready: "Review delivery and pay on Oda",
    resolved: "Resolved",
    failed: "Needs attention",
  })[request.status];
/** A ticket is a real need: something to buy or fix. Everything else stays in the chat. */
export const isTicket = (request: OfficeRequest) => !!request.intent;
/** Live on the office view: the agent working now, or a ticket that isn't finished. */
export const inLoop = (request: OfficeRequest) =>
  request.status === "processing" || (isTicket(request) && request.status !== "resolved");
const urgency: OfficeRequest["status"][] = [
  "failed",
  "awaiting_approval",
  "processing",
  "needs_clarification",
  "awaiting_input",
  "maintenance_open",
  "checkout_ready",
  "ordered",
  "resolved",
];
export const isMoreUrgent = (a: OfficeRequest["status"], b: OfficeRequest["status"]) =>
  urgency.indexOf(a) < urgency.indexOf(b);
/** Most urgent first, newest first within that. A newer order must not hide an older approval. */
export const byUrgency = (requests: OfficeRequest[]) =>
  [...requests].sort(
    (a, b) =>
      urgency.indexOf(a.status) - urgency.indexOf(b.status) ||
      b.updated_at.localeCompare(a.updated_at),
  );
export const mostUrgent = (requests: OfficeRequest[]) =>
  byUrgency(requests)[0] as OfficeRequest | undefined;
export const isQuoteExpired = (request: OfficeRequest) =>
  !!request.proposal?.expires_at && new Date(request.proposal.expires_at).getTime() <= Date.now();
/** The agent's question stays current this long; after that a new message starts fresh. */
export const isRecentChat = (request: OfficeRequest) =>
  Date.now() - new Date(request.updated_at).getTime() < 10 * 60 * 1000;
export const isOpen = (request: OfficeRequest) =>
  !["ordered", "resolved"].includes(request.status);
export const money = (value: number) =>
  new Intl.NumberFormat("en-NO", {
    style: "currency",
    currency: "NOK",
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  }).format(value);
export const stamp = (value: string) =>
  new Date(value).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });
// Only a merged duplicate can be resolved as a purchase without a proposal.
export const isMerged = (request: OfficeRequest) =>
  request.status === "resolved" &&
  request.intent === "purchase" &&
  !request.proposal;
export const requestTitle = (request: OfficeRequest) =>
  isMerged(request)
    ? "Merged with an earlier request"
    : request.summary || "New request";
