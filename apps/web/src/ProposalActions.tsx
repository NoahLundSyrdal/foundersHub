import type { OfficeRequest } from "@office/contracts";
import { useContext } from "react";
import { ApprovalChannelContext } from "./OdaFoodOrder";
import { isQuoteExpired } from "./format";

export type ProposalHandlers = {
  onApprove: (request: OfficeRequest) => void;
  onReject: (request: OfficeRequest) => void;
  onRequote: (request: OfficeRequest) => void;
};
/** The same choices everywhere a proposal is shown, so one can never get stuck waiting. */
export function ProposalActions({ request, busy, onApprove, onReject, onRequote }: ProposalHandlers & { request: OfficeRequest; busy: boolean }) {
  const channel = useContext(ApprovalChannelContext);
  if (channel === "whatsapp") return <p>Du får en melding på WhatsApp for å bekrefte dette nøyaktige tilbudet.</p>;
  const expired = isQuoteExpired(request);
  return (
    <div className="proposal-actions">
      {request.approval && <p className="hint">For {request.approval.approver} · {request.approval.reason}</p>}
      {expired && <p>This quote has expired.</p>}
      {expired ? (
        <button className="primary" disabled={busy} onClick={() => onRequote(request)}>Get a new quote</button>
      ) : (
        <button className="primary" disabled={busy} onClick={() => onApprove(request)}>Approve simulated order</button>
      )}
      <button className="secondary" disabled={busy} onClick={() => onReject(request)}>Don’t order</button>
    </div>
  );
}
