import { createContext, useContext, useEffect, useState } from "react";
import { proposalFingerprint, type EventResponse, type OfficeRequest } from "@office/contracts";
import { api } from "./api";
import "./oda-food.css";

const money = (amount: number) => new Intl.NumberFormat("en-NO", { style: "currency", currency: "NOK" }).format(amount);
export const ApprovalChannelContext = createContext<"dashboard" | "whatsapp">("dashboard");

export function OdaConnection() {
  const [connected, setConnected] = useState<boolean | null>(null);
  const [enabled, setEnabled] = useState(true);
  useEffect(() => {
    let alive = true;
    api<{ connected: boolean; enabled: boolean }>("/integrations/oda").then((result) => {
      if (alive) { setConnected(result.connected); setEnabled(result.enabled); }
    }).catch(() => {});
    return () => { alive = false; };
  }, []);
  if (!enabled) return null;
  return <aside className="oda-connection" aria-label="Oda food connection">
    <div><strong>Food from Oda</strong><span>{connected ? "Connected · live products and prices" : "Connect for live food search and cart preparation"}</span></div>
    {connected ? <span className="oda-live">Connected</span> : <a className="secondary" href="/api/integrations/oda/connect">Connect Oda ↗</a>}
  </aside>;
}

export function OdaFoodOrder({ request, busy, perform }: {
  request: OfficeRequest; busy: boolean; perform?: (action: () => Promise<void>) => Promise<unknown>;
}) {
  const [loading, setLoading] = useState(false);
  const approvalChannel = useContext(ApprovalChannelContext);
  const [error, setError] = useState("");
  const run = async (action: () => Promise<void>) => {
    if (perform) return perform(action);
    setLoading(true); setError("");
    try { await action(); }
    catch (failure) { setError(failure instanceof Error ? failure.message : "Oda request failed"); }
    finally { setLoading(false); }
  };
  busy = busy || loading;
  const grocery = request.grocery;
  if (!grocery) return null;
  const proposal = grocery.proposal;
  const approve = () => void run(async () => {
    if (!proposal) return;
    await api(`/requests/${request.id}/approve`, { proposal_fingerprint: proposalFingerprint(proposal) });
  });
  return <section className="oda-food" aria-label="Oda food basket">
    <div className="oda-food-heading"><strong>Oda food basket</strong><span>Live supplier</span></div>
    {error && <p role="alert">{error}</p>}
    {grocery.state === "connection_required" && <>
      <p>Connect your Oda account to find available products and current prices.</p>
      <a className="primary" href="/api/integrations/oda/connect">Connect Oda ↗</a>
      <button className="secondary" disabled={busy} onClick={() => void run(async () => {
        await api<EventResponse>(`/requests/${request.id}/transcript`, { text: request.original_text });
      })}>Search again after connecting</button>
    </>}
    {proposal && <>
      <ul className="oda-lines">{proposal.lines.map((line) => <li key={line.product_id}>
        <div><strong>{line.quantity} × {line.name}</strong><small>{money(line.unit_price_nok)} per product</small></div>
        <span>{money(line.line_total_nok)}</span>
      </li>)}</ul>
      <div className="oda-subtotal"><strong>Products subtotal</strong><strong>{money(proposal.subtotal_nok)}</strong></div>
      <p className="oda-note">Delivery fees and delivery time are confirmed on Oda. This basket has not been ordered or paid for.</p>
      <p>{proposal.reason}</p>
      {grocery.state === "awaiting_approval" && approvalChannel === "whatsapp" && <p>Du får en melding på WhatsApp for å bekrefte dette nøyaktige tilbudet.</p>}
      {grocery.state === "awaiting_approval" && approvalChannel !== "whatsapp" && <>
        <small>Price snapshot expires {new Date(proposal.expires_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}.</small>
        <button className="primary" disabled={busy} onClick={approve}>Add these products to Oda cart</button>
        <button className="secondary" disabled={busy} onClick={() => void run(async () => {
          await api(`/requests/${request.id}/reject`, {});
        })}>Don’t add to cart</button>
      </>}
    </>}
    {grocery.state === "cart_ready" && grocery.cart_url && <a className="primary" href={grocery.cart_url} target="_blank" rel="noreferrer">Review delivery and pay on Oda ↗</a>}
    {grocery.state === "uncertain" && <>
      <p>The cart update needs verification. Check again to read the current cart; this will not add products again.</p>
      <button className="secondary" disabled={busy} onClick={approve}>Check Oda cart status</button>
      <a href="https://oda.com/no/" target="_blank" rel="noreferrer">Open Oda ↗</a>
    </>}
  </section>;
}
