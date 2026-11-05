import type { OfficeRequest } from "@office/contracts";

export const money = (value: number) =>
  new Intl.NumberFormat("en-NO", {
    style: "currency",
    currency: "NOK",
    maximumFractionDigits: 0,
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
