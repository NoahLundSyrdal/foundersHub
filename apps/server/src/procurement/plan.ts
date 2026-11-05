import type { OfficeRequest, Proposal, Station } from "@office/contracts";
import { products, office } from "../config.js";
export type PurchasePlan =
  | { kind: "duplicate"; request: OfficeRequest }
  | { kind: "clarification"; question: string }
  | { kind: "proposal"; proposal: Proposal };
/** Pure planning boundary: replace catalogue selection here; never execute a purchase. */
export function planPurchase(
  station: Station,
  itemId: string | null,
  quantity: number,
  requests: OfficeRequest[],
): PurchasePlan {
  const product = products.find(
    (p) => p.id === itemId && p.station_id === station.id,
  );
  if (!product)
    return {
      kind: "clarification",
      question: "Choose an approved item for this station.",
    };
  const duplicate = requests.find(
    (r) =>
      r.station_id === station.id &&
      r.item_id === product.id &&
      (["awaiting_approval", "ordered"].includes(r.status) ||
        (r.status === "failed" && r.proposal !== null)),
  );
  if (duplicate) return { kind: "duplicate", request: duplicate };
  const total = Math.round(product.price_nok * quantity * 100) / 100;
  if (total > office.max_order_nok)
    return {
      kind: "clarification",
      question: `Order exceeds the NOK ${office.max_order_nok} limit. Request a smaller quantity.`,
    };
  return {
    kind: "proposal",
    proposal: {
      kind: "purchase",
      product,
      quantity,
      total_nok: total,
      currency: "NOK",
      simulation: true,
    },
  };
}
