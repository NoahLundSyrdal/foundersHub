import { useCallback, useEffect, useState } from "react";
import { api } from "./api";

type Rule = { id: string; label: string; keywords: string[]; name: string; phone: string; enabled: boolean };
type Sent = { execution_id: string; ticket_id: string; label: string; name: string; body: string; state: string; at: number };
type Payload = {
  rules: Rule[];
  sent: Sent[];
  whatsapp: { connected: boolean; configured?: boolean; queued?: number };
  suggestions: { label: string; keywords: string[] }[];
};

const stateText: Record<string, string> = {
  queued: "In the queue", sent: "Sent", delivered: "Delivered", read: "Read",
  uncertain: "Uncertain, check the phone", expired: "Not sent (bridge offline)", skipped: "Not sent (rule changed)",
  no_bridge: "Not sent (WhatsApp not connected)",
};

/** Who gets a WhatsApp message when a piece of equipment is reported out of order. */
export default function AlertRules() {
  const [data, setData] = useState<Payload | null>(null);
  const [error, setError] = useState("");
  const [form, setForm] = useState({ label: "", keywords: "", name: "", phone: "" });

  const load = useCallback(async () => {
    try {
      setData(await api<Payload>("/alerts/rules"));
      setError("");
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);
  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 4000);
    return () => clearInterval(timer);
  }, [load]);

  async function add(event: React.FormEvent) {
    event.preventDefault();
    try {
      await api("/alerts/rules", {
        label: form.label,
        keywords: form.keywords.split(",").map((k) => k.trim()).filter(Boolean),
        name: form.name,
        phone: form.phone,
        enabled: true,
      });
      setForm({ label: "", keywords: "", name: form.name, phone: form.phone });
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function toggle(rule: Rule) {
    await api(`/alerts/rules/${rule.id}/enabled`, { enabled: !rule.enabled });
    await load();
  }

  async function remove(rule: Rule) {
    await fetch(`/api/alerts/rules/${rule.id}`, { method: "DELETE" });
    await load();
  }

  return (
    <section className="alert-rules" aria-labelledby="alert-rules-title">
      <header>
        <h2 id="alert-rules-title">Alerts</h2>
        <p>When something is reported out of order, the right person gets a WhatsApp message right away.</p>
        <span className={`wa-status ${data?.whatsapp.connected ? "on" : "off"}`}>
          WhatsApp {data?.whatsapp.connected ? "connected" : "not connected"}
        </span>
      </header>
      {error && <div className="alert error">{error}</div>}

      <ul className="rule-list">
        {data?.rules.length === 0 && <li className="empty">No alert rules yet. Add one below.</li>}
        {data?.rules.map((rule) => (
          <li key={rule.id} className={rule.enabled ? "" : "disabled"}>
            <div>
              <strong>{rule.label}</strong> out of order → <strong>{rule.name}</strong> <span className="muted">({rule.phone})</span>
              <div className="muted">Keywords: {rule.keywords.join(", ")}</div>
            </div>
            <div className="rule-actions">
              <button onClick={() => void toggle(rule)}>{rule.enabled ? "Pause" : "Turn on"}</button>
              <button onClick={() => void remove(rule)}>Delete</button>
            </div>
          </li>
        ))}
      </ul>

      <form className="rule-form" onSubmit={(e) => void add(e)}>
        <h3>New alert</h3>
        <div className="suggestions">
          {data?.suggestions.map((s) => (
            <button type="button" key={s.label} onClick={() => setForm({ ...form, label: s.label, keywords: s.keywords.join(", ") })}>
              + {s.label}
            </button>
          ))}
        </div>
        <label>Equipment<input required value={form.label} onChange={(e) => setForm({ ...form, label: e.target.value })} placeholder="Toilet" /></label>
        <label>Keywords (comma separated)<input required value={form.keywords} onChange={(e) => setForm({ ...form, keywords: e.target.value })} placeholder="toalett, toilet, wc" /></label>
        <label>Send to (name)<input required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Noah" /></label>
        <label>WhatsApp number<input required value={form.phone} onChange={(e) => setForm({ ...form, phone: e.target.value })} placeholder="+47 123 45 678" inputMode="tel" /></label>
        <button className="primary" type="submit">Save alert</button>
      </form>

      {!!data?.sent.length && (
        <div className="sent-log">
          <h3>Recently sent</h3>
          <ul>
            {data.sent.map((s) => (
              <li key={s.execution_id}>
                <span>{new Date(s.at * 1000).toLocaleTimeString("nb-NO", { hour: "2-digit", minute: "2-digit" })}</span>{" "}
                <strong>{s.label}</strong> → {s.name}: <em>{stateText[s.state] ?? s.state}</em>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
