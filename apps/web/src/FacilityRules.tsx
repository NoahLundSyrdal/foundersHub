import { useEffect, useRef, useState, type FormEvent } from "react";
import type { FacilityIncident, FacilityReading, FacilityRule } from "@office/contracts";
import "./facility-rules.css";

type Snapshot = {
  rules: FacilityRule[];
  incidents: FacilityIncident[];
  readings: FacilityReading[];
  evaluated_at: string;
};
type Action = FacilityRule["actions"][number];
const actionLabels: Record<Action, string> = {
  alert: "Dashboard alert", ticket: "Maintenance task", replenish: "Replenishment request", sound: "Browser chime",
};
const units: Record<FacilityRule["metric"], string> = {
  co2_ppm: "ppm", temperature_c: "°C", humidity_pct: "% RH", stock_level: "%", bin_fill_pct: "%",
  obstruction_mm: "mm", noise_level: "relative level", tilt_deg: "°", heartbeat: "",
};
const time = (value: string) => new Date(value).toLocaleString([], {
  month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
});
const measurement = (value: number, metric: FacilityRule["metric"]) => `${Number(value.toFixed(1))} ${units[metric]}`.trim();
const condition = (rule: FacilityRule) => rule.direction === "stale"
  ? `No fresh reading for ${rule.trigger_value}s`
  : `${rule.direction === "above" ? "Above" : "Below"} ${measurement(rule.trigger_value, rule.metric)} for ${rule.min_duration_seconds}s · ${rule.min_samples} samples`;
function outcome(result: unknown): string {
  if (typeof result === "string") return result;
  if (!result || typeof result !== "object") return "Recorded";
  const value = result as Record<string, unknown>;
  if (typeof value.message === "string") return value.message;
  if (typeof value.detail === "string") return value.detail;
  if (typeof value.status === "string") return value.status.replaceAll("_", " ");
  return "Recorded";
}

async function request<T>(path: string, signal: AbortSignal, body?: unknown): Promise<T> {
  const response = await fetch(`/api/facility${path}`, {
    signal,
    ...(body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
  const result: unknown = await response.json();
  if (!response.ok) {
    const payload = result && typeof result === "object" ? result as Record<string, unknown> : {};
    const detail = payload.detail ?? payload.error;
    throw new Error(typeof detail === "string" ? detail : Array.isArray(detail)
      ? detail.map((entry) => entry && typeof entry === "object" && "msg" in entry ? String(entry.msg) : "Invalid setting").join("; ")
      : `Facility request failed (${response.status})`);
  }
  return result as T;
}

function RuleEditor({ rule, pending, onSave, onCancel }: {
  rule: FacilityRule; pending: boolean; onSave: (rule: FacilityRule) => void; onCancel: () => void;
}) {
  const [draft, setDraft] = useState<FacilityRule>({ ...rule, actions: [...rule.actions] });
  const [error, setError] = useState("");
  const number = (key: "trigger_value" | "reset_value" | "min_duration_seconds" | "min_samples" | "max_gap_seconds", label: string, min?: number) => (
    <label>{label}<input type="number" step={key === "min_samples" ? 1 : "any"} min={min} required
      value={Number.isNaN(draft[key]) ? "" : draft[key]} disabled={pending}
      onChange={(event) => setDraft({ ...draft, [key]: event.target.value === "" ? NaN : Number(event.target.value) })} /></label>
  );
  function submit(event: FormEvent) {
    event.preventDefault();
    if (![draft.trigger_value, draft.reset_value, draft.min_samples, draft.min_duration_seconds, draft.max_gap_seconds].every(Number.isFinite)) {
      setError("Enter a valid number for each setting."); return;
    }
    if (!draft.actions.length) { setError("Choose at least one action."); return; }
    if (draft.direction === "above" && draft.reset_value >= draft.trigger_value ||
      draft.direction === "below" && draft.reset_value <= draft.trigger_value) {
      setError(`Recovery must be ${draft.direction === "above" ? "below" : "above"} the trigger value.`); return;
    }
    if (!Number.isInteger(draft.min_samples) || draft.min_samples < 1) { setError("Minimum samples must be a positive whole number."); return; }
    setError(""); onSave(draft);
  }
  return <form className="facility-rules__editor" onSubmit={submit}>
    <label className="facility-rules__check"><input type="checkbox" checked={draft.enabled} disabled={pending}
      onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })} />Enable this check</label>
    <div className="facility-rules__fields">
      {number("trigger_value", draft.direction === "stale" ? "Timeout (seconds)" : `Trigger (${units[draft.metric] || "value"})`)}
      {draft.direction !== "stale" && <>
        {number("reset_value", `Recovery (${units[draft.metric] || "value"})`)}
        {number("min_duration_seconds", "Sustained for (seconds)", 0)}
        {number("min_samples", "Minimum samples", 1)}
        {number("max_gap_seconds", "Maximum sample gap (seconds)", 1)}
      </>}
    </div>
    <fieldset disabled={pending}><legend>When this check triggers</legend>
      {(Object.keys(actionLabels) as Action[]).filter((action) => {
        if (draft.direction === "stale") return action === "alert";
        if (draft.metric === "noise_level" && action === "sound") return false;
        return action !== "replenish" || draft.metric === "stock_level" && !!draft.item_id;
      }).map((action) => (
        <label className="facility-rules__check" key={action}><input type="checkbox" checked={draft.actions.includes(action)}
          onChange={(event) => setDraft({ ...draft, actions: event.target.checked
            ? [...draft.actions, action] : draft.actions.filter((current) => current !== action) })} />{actionLabels[action]}</label>
      ))}
    </fieldset>
    <p className="facility-rules__hint">Replenishment follows purchasing policy and approval. Chimes require sound to be enabled in this browser.</p>
    {error && <p className="facility-rules__error" role="alert">{error}</p>}
    <div className="facility-rules__buttons"><button type="submit" className="facility-rules__primary" disabled={pending}>{pending ? "Saving…" : "Save check"}</button>
      <button type="button" disabled={pending} onClick={onCancel}>Cancel</button></div>
  </form>;
}

/** Fixed sensor rules; purchases still use the server's approval workflow. */
export function FacilityRules({ visible = true, onOpenChecks, onOpenRequest }: {
  visible?: boolean; onOpenChecks?: () => void; onOpenRequest?: (id: string) => void;
}) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [loadError, setLoadError] = useState("");
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const [connected, setConnected] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [simulationRule, setSimulationRule] = useState("");
  const [simulationValue, setSimulationValue] = useState("1300");
  const [soundEnabled, setSoundEnabled] = useState(false);
  const [soundError, setSoundError] = useState("");
  const audio = useRef<AudioContext | null>(null);
  const soundOn = useRef(false);
  const seen = useRef(new Set<string>());
  const refresh = useRef<() => void>(() => {});
  const controllers = useRef(new Set<AbortController>());
  const mounted = useRef(false);
  const busy = useRef(false);

  useEffect(() => {
    mounted.current = true;
    let disposed = false;
    let sequence = 0;
    let reading: AbortController | undefined;
    let reconnecting = true;
    let baselinePending = true;
    const sync = (baseline = false) => {
      reading?.abort();
      const controller = new AbortController();
      reading = controller;
      controllers.current.add(controller);
      const version = ++sequence;
      void request<Snapshot>("", controller.signal).then((next) => {
        if (disposed || controller.signal.aborted || version !== sequence) return;
        const newSound = next.incidents.some((incident) => !seen.current.has(incident.id) &&
          incident.status === "active" && incident.actions.includes("sound"));
        for (const incident of next.incidents) seen.current.add(incident.id);
        setSnapshot(next); setLoadError("");
        const mayChime = !baseline && !reconnecting && !baselinePending;
        baselinePending = false;
        if (mayChime && newSound && soundOn.current && audio.current) {
          const context = audio.current;
          if (context.state !== "running") {
            soundOn.current = false; setSoundEnabled(false); setSoundError("Browser sound paused. Enable sound again to hear new alerts.");
            return;
          }
          try {
            const oscillator = context.createOscillator();
            const gain = context.createGain();
            oscillator.type = "sine"; oscillator.frequency.value = 660;
            gain.gain.setValueAtTime(0, context.currentTime);
            gain.gain.linearRampToValueAtTime(0.12, context.currentTime + 0.015);
            gain.gain.exponentialRampToValueAtTime(0.001, context.currentTime + 0.3);
            oscillator.connect(gain); gain.connect(context.destination);
            oscillator.start(); oscillator.stop(context.currentTime + 0.32);
            oscillator.onended = () => { oscillator.disconnect(); gain.disconnect(); };
          } catch {
            setSoundError("The browser could not play the alert chime.");
          }
        }
      }).catch((error: unknown) => {
        if (!disposed && !controller.signal.aborted && version === sequence)
          setLoadError(error instanceof Error ? error.message : "Facility checks could not be refreshed.");
      }).finally(() => controllers.current.delete(controller));
    };
    refresh.current = () => sync(false);
    sync(true);
    // Polling also refreshes sensor age while the event connection is healthy.
    const poll = setInterval(() => sync(false), 5000);
    let stream: EventSource | undefined;
    try {
      stream = new EventSource("/api/events");
      stream.addEventListener("facility.updated", () => sync(false));
      stream.onopen = () => {
        if (disposed) return;
        reconnecting = false; baselinePending = true; setConnected(true); sync(true);
      };
      stream.onerror = () => {
        if (disposed) return;
        reconnecting = true; baselinePending = true; setConnected(false);
      };
    } catch { setConnected(false); }
    return () => {
      disposed = true; mounted.current = false;
      refresh.current = () => {}; clearInterval(poll); stream?.close();
      for (const controller of controllers.current) controller.abort();
      controllers.current.clear();
      soundOn.current = false;
      if (audio.current) { void audio.current.close().catch(() => {}); audio.current = null; }
    };
  }, []);

  async function mutate(path: string, body: unknown, key: string, success: string) {
    if (busy.current) return;
    busy.current = true; setPending(key); setActionError(""); setNotice("");
    const controller = new AbortController(); controllers.current.add(controller);
    try {
      await request(path, controller.signal, body);
      if (mounted.current && !controller.signal.aborted) {
        setNotice(success); if (key.startsWith("rule:")) setEditing(null); refresh.current();
      }
    } catch (error) {
      if (mounted.current && !controller.signal.aborted)
        setActionError(error instanceof Error ? error.message : "The action could not be saved.");
    } finally {
      controllers.current.delete(controller); busy.current = false;
      if (mounted.current && !controller.signal.aborted) setPending(null);
    }
  }

  async function toggleSound() {
    setSoundError("");
    if (soundOn.current) { soundOn.current = false; setSoundEnabled(false); return; }
    try {
      if (typeof window.AudioContext !== "function") throw new Error("This browser does not support alert sound.");
      const context = audio.current ?? new AudioContext(); audio.current = context;
      await context.resume();
      if (!mounted.current) return;
      if (context.state !== "running") throw new Error("The browser blocked sound. Try enabling it again.");
      soundOn.current = true; setSoundEnabled(true);
    } catch (error) { setSoundError(error instanceof Error ? error.message : "Sound could not be enabled."); }
  }

  const rules = snapshot?.rules ?? [];
  const currentSimulationRule = rules.find((rule) => rule.id === simulationRule) ?? rules[0];
  const active = snapshot?.incidents.filter((incident) => incident.status === "active") ?? [];
  const alerts = active.filter((incident) => incident.actions.includes("alert") && !incident.acknowledged_at);
  function simulate(event: FormEvent) {
    event.preventDefault();
    if (!currentSimulationRule || !Number.isFinite(Number(simulationValue)) || !simulationValue.trim()) return;
    void mutate("/readings", {
      event_id: crypto.randomUUID(), board_id: currentSimulationRule.board_id, metric: currentSimulationRule.metric,
      value: Number(simulationValue), timestamp: new Date().toISOString(), simulation: true,
    }, "simulate", "One simulated reading published. The configured duration and sample count still apply.");
  }

  return <>
    {!visible && alerts.length > 0 && <aside className="facility-notice" role="status">
      <div><strong>{alerts[0].title}{alerts[0].simulation ? " · Simulated" : ""}</strong>
        <p>{alerts.length} facility {alerts.length === 1 ? "check needs" : "checks need"} attention.</p></div>
      <button type="button" onClick={onOpenChecks}>Review checks</button>
    </aside>}
    <section hidden={!visible} className="facility-rules" aria-label="Facility rules">
    <div className="facility-rules__header"><div><span className="facility-rules__eyebrow">AUTOMATED CHECKS</span>
      <h2>Facility rules</h2><p>Fixed sensor checks, clear actions, tracked recovery.</p></div>
      <button type="button" className={soundEnabled ? "facility-rules__sound-on" : ""} onClick={() => void toggleSound()} aria-pressed={soundEnabled}>
        {soundEnabled ? "Mute browser sound" : "Enable browser sound"}</button>
    </div>
    <p className="facility-rules__hint">Thresholds are configurable operating settings. Replenishment uses the existing approval policy. Sound plays in this browser.</p>
    <div className="facility-rules__summary"><span><i className={connected ? "is-connected" : ""} />{connected ? "Live updates" : "Checking every 5 seconds"}</span>
      <span>{rules.filter((rule) => rule.enabled).length} checks enabled</span><span>{active.length} active incidents</span>
      {snapshot && <span>Updated {time(snapshot.evaluated_at)}</span>}</div>
    {loadError && <p className="facility-rules__error" role="status">{loadError} {snapshot ? "Previously loaded information remains visible." : "Check the backend connection."}</p>}
    {actionError && <p className="facility-rules__error" role="alert">{actionError}</p>}
    {soundError && <p className="facility-rules__error" role="status">{soundError}</p>}
    {notice && <p className="facility-rules__notice" role="status">{notice}</p>}
    {!snapshot && !loadError && <p className="facility-rules__hint" role="status">Loading facility checks…</p>}
    <div className="facility-rules__grid">{rules.map((rule) => {
      const incidents = active.filter((incident) => incident.rule_id === rule.id);
      const readings = (snapshot?.readings ?? []).filter((reading) => reading.board_id === rule.board_id && reading.metric === rule.metric)
        .sort((a, b) => b.timestamp.localeCompare(a.timestamp));
      const actual = readings.find((reading) => !reading.simulation);
      const simulated = readings.find((reading) => reading.simulation);
      const stale = actual && snapshot && Date.parse(snapshot.evaluated_at) - Date.parse(actual.timestamp) > rule.max_gap_seconds * 1000;
      return <article key={rule.id} className={`facility-rules__card${incidents.length ? " has-incident" : ""}${!rule.enabled ? " is-disabled" : ""}`}>
        <div className="facility-rules__card-header"><span>{rule.board_id.replaceAll("_", " ")}</span>
          <span className={`facility-rules__badge${incidents.length ? " is-active" : ""}`}>{incidents.length ? "Attention" : rule.enabled ? "Enabled" : "Disabled"}</span></div>
        <h3>{rule.title}</h3><p className="facility-rules__condition">{condition(rule)}</p>
        <div className="facility-rules__reading"><strong>{actual ? measurement(actual.value, rule.metric) : "Awaiting live reading"}</strong>
          {actual && <small>{stale ? "Last live reading · stale" : "Live"} · {time(actual.timestamp)}</small>}
          {simulated && <small className="facility-rules__simulation">Simulated: {measurement(simulated.value, rule.metric)} · {time(simulated.timestamp)}</small>}</div>
        <div className="facility-rules__actions">{rule.actions.map((action) => <span key={action}>{actionLabels[action]}</span>)}</div>
        {!rule.enabled && <p className="facility-rules__hint">Enable after choosing sensor placement and checking calibration.</p>}
        {rule.direction !== "stale" && <p className="facility-rules__hint">Re-arm at {rule.direction === "above" ? "or below" : "or above"} {measurement(rule.reset_value, rule.metric)}.</p>}
        {editing === rule.id ? <RuleEditor key={rule.id} rule={rule} pending={pending !== null}
          onSave={(updated) => void mutate(`/rules/${encodeURIComponent(rule.id)}`, updated, `rule:${rule.id}`, "Check settings saved.")}
          onCancel={() => setEditing(null)} /> : <button type="button" disabled={pending !== null} className="facility-rules__configure" onClick={() => setEditing(rule.id)}>Configure check <span aria-hidden="true">↗</span></button>}
      </article>;
    })}</div>
    {!!snapshot && <details className="facility-rules__simulator"><summary>Test with simulated readings</summary>
      <p className="facility-rules__hint">Publishes one reading at the current time. Simulated readings are kept separate from live readings. Repeated samples and the configured duration are still required.</p>
      <form onSubmit={simulate}><label>Check<select value={currentSimulationRule?.id ?? ""} onChange={(event) => setSimulationRule(event.target.value)} disabled={pending !== null}>
        {rules.map((rule) => <option key={rule.id} value={rule.id}>{rule.title}{rule.enabled ? "" : " (disabled)"}</option>)}</select></label>
        <label>Reading {currentSimulationRule && units[currentSimulationRule.metric]}<input type="number" step="any" required value={simulationValue} onChange={(event) => setSimulationValue(event.target.value)} disabled={pending !== null} /></label>
        <button type="submit" disabled={pending !== null || !currentSimulationRule || !Number.isFinite(Number(simulationValue))}>{pending === "simulate" ? "Publishing…" : "Publish simulated reading"}</button></form>
    </details>}
    <details className="facility-rules__history" open={active.length > 0}><summary>Incident history <span>{snapshot?.incidents.length ?? 0}</span></summary>
      <p className="facility-rules__hint">Acknowledging records that you saw an incident. Recovery follows the sensor rule.</p>
      {!snapshot?.incidents.length && <p className="facility-rules__hint">No incidents recorded.</p>}
      <ul>{snapshot?.incidents.slice(0, 30).map((incident) => <li key={incident.id} className={incident.status === "active" ? "is-active" : ""}>
        <div className="facility-rules__incident-heading"><strong>{incident.title}</strong><span>{incident.status === "active" ? incident.severity : "Recovered"}</span>
          <span>{incident.simulation ? "Simulated" : "Live"}</span></div><p>{incident.summary}</p>
        <small>Opened {time(incident.opened_at)}{incident.recovered_at ? ` · Recovered ${time(incident.recovered_at)}` : ""}{incident.acknowledged_at ? ` · Seen ${time(incident.acknowledged_at)}` : ""}</small>
        {Object.keys(incident.action_results).length > 0 && <dl className="facility-rules__results">{Object.entries(incident.action_results).map(([action, result]) => <div key={action}><dt>{actionLabels[action as Action] ?? action}</dt><dd>{outcome(result)}
          {onOpenRequest && result !== null && typeof result === "object" && "task_id" in result && typeof result.task_id === "string" &&
            <button type="button" onClick={() => onOpenRequest(result.task_id as string)}>Open request</button>}
        </dd></div>)}</dl>}
        {incident.status === "active" && !incident.acknowledged_at && <button type="button" disabled={pending !== null}
          onClick={() => void mutate(`/incidents/${encodeURIComponent(incident.id)}/acknowledge`, {}, `incident:${incident.id}`, "Incident acknowledged.")}>{pending === `incident:${incident.id}` ? "Saving…" : "Acknowledge"}</button>}
      </li>)}</ul>
    </details>
  </section></>;
}
