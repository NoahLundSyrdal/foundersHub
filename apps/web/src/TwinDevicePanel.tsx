import { useEffect, useId, useState } from "react";
import type { OfficeRequest, Station } from "@office/contracts";
import type { TwinDevice } from "./twinDevices";
import "./twin-devices.css";

type Props = {
  devices: TwinDevice[];
  selectedId: string | null;
  stations: Station[];
  requests: OfficeRequest[];
  disabled: boolean;
  offline: boolean;
  placing: boolean;
  onSelect: (id: string) => void;
  onAdd: (kind: TwinDevice["kind"]) => void;
  onSave: (device: TwinDevice) => void;
  onPlace: () => void;
  onCancelPlace: () => void;
  onSimulate: (device: TwinDevice) => void;
  onRequest: (id: string) => void;
  onRemove: (id: string) => void;
};
const labels: Record<OfficeRequest["status"], string> = {
  processing: "Agent working", awaiting_input: "Waiting for details", awaiting_approval: "Approval needed", needs_clarification: "More details needed", maintenance_open: "Maintenance open", ordered: "Order simulated", resolved: "Resolved", failed: "Needs attention",
};
const presets = [
  { label: "Stage / front chairs", x: -5.65, z: 6.7 },
  { label: "Coffee counter", x: 5.25, z: -1.5 },
  { label: "Bathroom", x: 7.15, z: -5.35 },
  { label: "Print station", x: 0.55, z: -2.35 },
];
function clone(device: TwinDevice): TwinDevice { return { ...device, position: { ...device.position }, tracking: { ...device.tracking } }; }
function validate(device: TwinDevice): string {
  if (!device.name.trim() || device.name.length > 80) return "Enter a device name of 1–80 characters.";
  if (!device.tracking.label.trim() || device.tracking.label.length > 60) return "Enter a tracked item of 1–60 characters.";
  if (device.tracking.unit.length > 16) return "Use a unit of 16 characters or fewer.";
  const { min, max, value, warningBelow } = device.tracking;
  if (![min, max, value, warningBelow].every(Number.isFinite)) return "Enter a number for every tracking field.";
  if (min >= max || !Number.isFinite(max - min)) return "The maximum must be greater than the minimum.";
  if (value < min || value > max) return "The simulated reading must be within the configured range.";
  if (warningBelow < min || warningBelow > max) return "The warning threshold must be within the configured range.";
  if (![device.position.x, device.position.z].every(Number.isFinite) || device.position.x < -10 || device.position.x > 10 || device.position.z < -7 || device.position.z > 7) return "Choose a position within the office model.";
  return "";
}
export function TwinDevicePanel(props: Props) {
  const { devices, selectedId, stations, requests, disabled, offline, placing, onSelect, onAdd, onSave, onPlace, onCancelPlace, onSimulate, onRequest, onRemove } = props;
  const selected = devices.find((device) => device.id === selectedId);
  const [draft, setDraft] = useState<TwinDevice | null>(selected ? clone(selected) : null);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const prefix = useId();
  useEffect(() => { setDraft(selected ? clone(selected) : null); setError(""); setSaved(false); }, [selected]);
  const stationRequests = selected ? requests.filter((item) => item.station_id === selected.stationId).sort((a, b) => b.updated_at.localeCompare(a.updated_at)) : [];
  const request = stationRequests.find((item) => !["ordered", "resolved"].includes(item.status)) ?? stationRequests[0];
  function patch(change: Partial<TwinDevice>) { if (draft) { setDraft({ ...draft, ...change }); setSaved(false); setError(""); } }
  function tracking(key: keyof TwinDevice["tracking"], value: string | number) { if (draft) patch({ tracking: { ...draft.tracking, [key]: value } }); }
  function save() {
    if (!draft) return;
    const message = validate(draft);
    if (message) { setError(message); return; }
    onSave({ ...draft, name: draft.name.trim(), tracking: { ...draft.tracking, label: draft.tracking.label.trim(), unit: draft.tracking.unit.trim() } });
    setSaved(true); setError("");
  }
  return <aside className="twin-device-panel" aria-label="Device configuration">
    <div className="tdp-heading"><span>OFFICE DEVICES</span><span>{devices.length}/20</span></div>
    <div className="tdp-add"><button type="button" disabled={devices.length >= 20 || placing} onClick={() => onAdd("button")}>+ Add button</button><button type="button" disabled={devices.length >= 20 || placing} onClick={() => onAdd("level")}>+ Add sensor</button></div>
    <label className="tdp-label" htmlFor={`${prefix}-device`}>Selected device</label>
    <select id={`${prefix}-device`} value={selectedId ?? ""} onChange={(event) => onSelect(event.target.value)} disabled={placing || devices.length === 0}>
      <option value="" disabled>Choose a device</option>
      {devices.map((device) => <option key={device.id} value={device.id}>{device.kind === "button" ? "●" : "◌"} {device.name}</option>)}
    </select>
    {!draft || !selected ? <div className="tdp-empty"><strong>Give the office a touchpoint.</strong><p>Select a device in the model or add a button or level sensor.</p></div> : <>
      <div className={`tdp-source ${selected.kind === "level" ? "demo" : "physical"}`}>{selected.kind === "button" ? "Physical button mapping" : "Simulated reading"}</div>
      <h2>{selected.name}</h2>
      <p className="tdp-description">{selected.kind === "button" ? "Match this station to the physical button’s bridge mapping. Hardware events routed to that station update the request status here." : "Explore a tracked stock level. This reading is a local simulation, not connected sensor telemetry."}</p>
      {selected.kind === "level" && <div className={`tdp-level ${selected.tracking.value <= selected.tracking.warningBelow ? "low" : ""}`}>
        <div><span>{selected.tracking.label}</span><strong>{selected.tracking.value.toLocaleString()}<small>{selected.tracking.unit}</small></strong></div>
        <meter min={selected.tracking.min} max={selected.tracking.max} low={selected.tracking.warningBelow} optimum={selected.tracking.max} value={selected.tracking.value} aria-label={`${selected.tracking.label}, simulated`} />
        <small>{selected.tracking.value <= selected.tracking.warningBelow ? "Below warning threshold" : "Within configured range"} · simulated</small>
      </div>}
      <section className="tdp-request" aria-label="Mapped station request">
        <div className="tdp-heading"><span>{stations.find((station) => station.id === selected.stationId)?.name ?? selected.stationId} REQUEST</span><span>{offline ? "API offline" : "API state"}</span></div>
        {request ? <button type="button" disabled={disabled} onClick={() => onRequest(request.id)}><strong>{request.summary || "New request"}</strong><span>{labels[request.status]} <b>↗</b></span></button> : <p>{offline ? "Reconnect to load the station’s request status." : "No request reported for this station yet."}</p>}
      </section>
      <form className="tdp-form" onSubmit={(event) => { event.preventDefault(); save(); }}>
        <h3>Configure device</h3>
        <label htmlFor={`${prefix}-name`}>Device name</label><input id={`${prefix}-name`} value={draft.name} maxLength={80} onChange={(event) => patch({ name: event.target.value })} required />
        <label htmlFor={`${prefix}-station`}>Request station</label><select id={`${prefix}-station`} value={draft.stationId} onChange={(event) => patch({ stationId: event.target.value as Station["id"] })}>{["kitchen", "bathroom", "printer"].map((id) => <option key={id} value={id}>{stations.find((station) => station.id === id)?.name ?? id}</option>)}</select>
        <label htmlFor={`${prefix}-tracked`}>{draft.kind === "level" ? "What are you tracking?" : "Input label"}</label><input id={`${prefix}-tracked`} value={draft.tracking.label} maxLength={60} required onChange={(event) => tracking("label", event.target.value)} />
        {draft.kind === "level" && <>
          <label htmlFor={`${prefix}-unit`}>Unit</label><input id={`${prefix}-unit`} value={draft.tracking.unit} maxLength={16} placeholder="%, packs, litres…" onChange={(event) => tracking("unit", event.target.value)} />
          <div className="tdp-fields">{([['min', 'Minimum'], ['max', 'Maximum'], ['warningBelow', 'Warn below']] as const).map(([key, title]) => <div key={key}><label htmlFor={`${prefix}-${key}`}>{title}</label><input id={`${prefix}-${key}`} type="number" step="any" value={Number.isFinite(draft.tracking[key]) ? draft.tracking[key] : ""} required onChange={(event) => tracking(key, event.target.value === "" ? NaN : Number(event.target.value))} /></div>)}</div>
          <label htmlFor={`${prefix}-value`}>Simulated reading <span>{Number.isFinite(draft.tracking.value) ? draft.tracking.value : "—"} {draft.tracking.unit}</span></label>
          <input id={`${prefix}-value`} type="range" min={Number.isFinite(draft.tracking.min) ? draft.tracking.min : 0} max={Number.isFinite(draft.tracking.max) && draft.tracking.max > draft.tracking.min ? draft.tracking.max : 100} step="any" value={Number.isFinite(draft.tracking.value) ? draft.tracking.value : 0} onChange={(event) => tracking("value", Number(event.target.value))} />
        </>}
        <label htmlFor={`${prefix}-preset`}>Location preset</label><select id={`${prefix}-preset`} value={presets.findIndex((preset) => preset.x === draft.position.x && preset.z === draft.position.z)} onChange={(event) => { const preset = presets[Number(event.target.value)]; if (preset) patch({ position: { x: preset.x, z: preset.z } }); }}><option value={-1}>Custom position</option>{presets.map((preset, index) => <option key={preset.label} value={index}>{preset.label}</option>)}</select>
        <div className="tdp-position"><span>x {draft.position.x.toFixed(2)} · z {draft.position.z.toFixed(2)}</span><button type="button" onClick={() => { if (placing) { onCancelPlace(); return; } const message = validate(draft); if (message) { setError(message); return; } onSave(clone(draft)); onPlace(); }}>{placing ? "Cancel placement" : "Place in 3D ↗"}</button></div>
        {placing && <p className="tdp-feedback" role="status">Click the office floor to place this device.</p>}
        {error && <p className="tdp-error" role="alert">{error}</p>}
        {saved && <p className="tdp-feedback" role="status">Configuration applied in this browser.</p>}
        <button type="submit" className="tdp-save" disabled={placing}>Save configuration</button>
      </form>
      <button type="button" className="tdp-simulate" disabled={disabled || offline || placing} onClick={() => onSimulate(selected)}>{selected.kind === "button" ? "Simulate button press" : "Simulate stock report"}<span>↗</span></button>
      <p className="tdp-footnote">Device placement and configuration stay in this browser. They do not reconfigure firmware. {selected.kind === "level" ? "Reading changes do not send hardware telemetry." : "Simulation sends a request through the existing station API."}</p>
      <button type="button" className="tdp-remove" disabled={placing} onClick={() => onRemove(selected.id)}>Remove device from model</button>
    </>}
  </aside>;
}
