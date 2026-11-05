import { useEffect, useRef } from "react";
import type { OfficeRequest, Station } from "@office/contracts";
import { isMerged, isTicket, loopLabel, money, requestTitle, stamp, statusLabels, statusTone } from "./format";
import { ProposalActions, type ProposalHandlers } from "./ProposalActions";
import "./tickets.css";

const RESOLVED_SHOWN = 6;
type Props = {
  requests: OfficeRequest[];
  stations: Station[];
  opened: string | null;
  openCount: number;
  busy: boolean;
  onResolve: (request: OfficeRequest) => void;
} & ProposalHandlers;
export function TicketsView({ requests, stations, opened, openCount, busy, onApprove, onReject, onRequote, onResolve }: Props) {
  const tickets = requests.filter(isTicket).sort((a, b) => b.created_at.localeCompare(a.created_at));
  const open = tickets.filter((r) => r.status !== "resolved");
  const resolved = tickets.filter((r) => r.status === "resolved");
  const ticket = (request: OfficeRequest) => (
    <Ticket
      key={request.id}
      request={request}
      station={stations.find((s) => s.id === request.station_id)}
      highlighted={opened === request.id}
      openCount={openCount}
      busy={busy}
      onApprove={onApprove}
      onReject={onReject}
      onRequote={onRequote}
      onResolve={onResolve}
    />
  );
  return (
    <>
      <section aria-label="Open tickets">
        <div className="section-head">
          <h2>Open tickets</h2>
          <span className="tally">{open.length} in the loop</span>
        </div>
        {open.length === 0 ? <p className="empty">No open tickets. Tell the agent if something is missing or broken.</p> : <div className="tickets">{open.map(ticket)}</div>}
      </section>
      {resolved.length > 0 && (
        <section aria-label="Resolved tickets">
          <div className="section-head">
            <h2>Resolved</h2>
            <span className="tally">{Math.min(resolved.length, RESOLVED_SHOWN)} of {resolved.length}, the rest are in History</span>
          </div>
          <div className="tickets">{resolved.slice(0, RESOLVED_SHOWN).map(ticket)}</div>
        </section>
      )}
    </>
  );
}

function Ticket({ request, station, highlighted, openCount, busy, onApprove, onReject, onRequote, onResolve }: {
  request: OfficeRequest;
  station: Station | undefined;
  highlighted: boolean;
  openCount: number;
  busy: boolean;
  onResolve: (request: OfficeRequest) => void;
} & ProposalHandlers) {
  const element = useRef<HTMLElement>(null);
  useEffect(() => {
    if (highlighted) element.current?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [highlighted, openCount]);
  const proposal = request.proposal;
  const team = request.assigned_to ?? station?.responsible_team;
  const resolved = request.status === "resolved";
  const reported = !!request.original_text && !request.original_text.startsWith("[sensor]");
  return (
    <article ref={element} className={`ticket ticket-${statusTone(request.status)} ${highlighted ? "ticket-open" : ""}`} aria-label={`Ticket ${request.id.slice(0, 6)}: ${statusLabels[request.status]}`}>
      <div className="ticket-main">
        <h3>{reported ? request.original_text : requestTitle(request)}</h3>
        {reported && !proposal && request.summary !== request.original_text && <p className="ticket-summary">{requestTitle(request)}</p>}
        <dl>
          <div><dt>Where</dt><dd>{station?.name ?? request.station_id}</dd></div>
          {team && <div><dt>Team</dt><dd>{team}</dd></div>}
          {proposal && <div><dt>Item</dt><dd>{proposal.quantity} × {proposal.product.name}</dd></div>}
          {proposal && <div><dt>Supplier</dt><dd>{proposal.product.supplier}</dd></div>}
          {proposal && <div><dt>Delivery</dt><dd>{proposal.product.delivery_estimate}</dd></div>}
          {proposal && <div><dt>Total</dt><dd>{money(proposal.total_nok)} (simulated)</dd></div>}
          {request.supplier_reference && <div><dt>Reference</dt><dd><code>{request.supplier_reference}</code> (simulated)</dd></div>}
        </dl>
        {request.status === "failed" && proposal && (
          <div className="alert error">Order outcome uncertain. Check the supplier before attempting another purchase. Retrying is disabled.</div>
        )}
        {request.status === "awaiting_approval" && proposal && (
          <ProposalActions request={request} busy={busy} onApprove={onApprove} onReject={onReject} onRequote={onRequote} />
        )}
        {request.status === "ordered" && (
          <button className="primary" disabled={busy} onClick={() => onResolve(request)}>Confirm simulated delivery</button>
        )}
        {request.status === "maintenance_open" && (
          <button className="primary" disabled={busy} onClick={() => onResolve(request)}>Mark resolved</button>
        )}
        {request.trace && request.trace.length > 0 && (
          <details className="log">
            <summary>All steps ({request.trace.length})</summary>
            <ol>
              {request.trace.map((entry, index) => (
                <li key={`${entry.at}-${index}`}>
                  <time dateTime={entry.at}>{stamp(entry.at)}</time>
                  <span>{entry.detail}</span>
                </li>
              ))}
            </ol>
          </details>
        )}
      </div>
      <div className="ticket-stub">
        <span className="ticket-no">#{request.id.slice(0, 6).toUpperCase()}</span>
        {resolved ? <span className="ticket-stamp">{isMerged(request) ? "Merged" : proposal && !request.supplier_reference ? "Not ordered" : "Resolved"}</span> : <strong>{loopLabel(request)}</strong>}
        <time dateTime={request.created_at}>{stamp(request.created_at)}</time>
      </div>
    </article>
  );
}
