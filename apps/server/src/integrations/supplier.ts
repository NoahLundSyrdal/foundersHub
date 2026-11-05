import type { Proposal } from "@office/contracts";
export interface Supplier {
  placeOrder(
    proposal: Proposal,
    idempotencyKey: string,
  ): { reference: string; simulation: true };
}
// Deliberately synchronous and local. Real suppliers need persisted uncertain-state recovery.
export const mockSupplier: Supplier = {
  placeOrder(_proposal, key) {
    return { reference: `SIM-${key}`, simulation: true };
  },
};
