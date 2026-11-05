import { useEffect, useId, useState } from "react";
import type { Station } from "@office/contracts";
import type { TwinDevice } from "./twinDevices";

type Props = {
  device: TwinDevice;
  stations: Station[];
  placing: boolean;
  onSave: (device: TwinDevice) => void;
  onPlace: () => void;
  onCancelPlace: () => void;
  onRemove: (id: string) => void;
  disabled: boolean;
  onTest: () => void;
};
function clone(device: TwinDevice): TwinDevice {
  return { ...device, position: { ...device.position }, tracking: { ...device.tracking } };
}
function validate(device: TwinDevice): string {
  if (!device.name.trim() || device.name.length > 80) return "Enter a device name of 1–80 characters.";
  if (!device.tracking.label.trim() || device.tracking.label.length > 60) return "Enter a tracked item of 1–60 characters.";
  if (device.tracking.unit.length > 16) return "Use a unit of 16 characters or fewer.";
  const { min, max, value, warningBelow } = device.tracking;
  if (![min, max, value, warningBelow].every(Number.isFinite)) return "Enter a number for every tracking field.";
  if (min >= max || !Number.isFinite(max - min)) return "The maximum must be greater than the minimum.";
  if (value < min || value > max) return "The simulated reading must be within the configured range.";
  if (warningBelow < min || warningBelow > max) return "The warning threshold must be within the configured range.";
  return "";
}
export function TwinDeviceSettings({ device, stations, placing, onSave, onPlace, onCancelPlace, onRemove, disabled, onTest }: Props) {
  const [draft, setDraft] = useState(() => clone(device));
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const prefix = useId();
  useEffect(() => {
    setDraft(clone(device));
    setError("");
    setSaved(false);
  }, [device]);
  function patch(change: Partial<TwinDevice>) {
    setDraft({ ...draft, ...change });
    setSaved(false);
    setError("");
  }
  function tracking(key: keyof TwinDevice["tracking"], value: string | number) {
    patch({ tracking: { ...draft.tracking, [key]: value } });
  }
  function save() {
    const message = validate(draft);
    if (message) {
      setError(message);
      return;
    }
    onSave({ ...draft, name: draft.name.trim(), tracking: { ...draft.tracking, label: draft.tracking.label.trim(), unit: draft.tracking.unit.trim() } });
    setSaved(true);
  }
  return (
    <details className="log twin-settings">
      <summary>Settings and testing</summary>
      <button className="secondary full" disabled={disabled} onClick={onTest}>
        {device.kind === "button" ? "Simulate a button press" : "Simulate low stock"}
      </button>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          save();
        }}
      >
        <label htmlFor={`${prefix}-name`}>Name</label>
        <input id={`${prefix}-name`} value={draft.name} maxLength={80} required onChange={(event) => patch({ name: event.target.value })} />
        <label htmlFor={`${prefix}-station`}>Station</label>
        <select id={`${prefix}-station`} value={draft.stationId} onChange={(event) => patch({ stationId: event.target.value as Station["id"] })}>
          {stations.map((station) => (
            <option key={station.id} value={station.id}>{station.name}</option>
          ))}
        </select>
        <label htmlFor={`${prefix}-tracked`}>{draft.kind === "level" ? "What it tracks" : "Input label"}</label>
        <input id={`${prefix}-tracked`} value={draft.tracking.label} maxLength={60} required onChange={(event) => tracking("label", event.target.value)} />
        {draft.kind === "level" && (
          <>
            <label htmlFor={`${prefix}-unit`}>Unit</label>
            <input id={`${prefix}-unit`} value={draft.tracking.unit} maxLength={16} onChange={(event) => tracking("unit", event.target.value)} />
            <div className="twin-settings-fields">
              {([["min", "Minimum"], ["max", "Maximum"], ["warningBelow", "Warn below"]] as const).map(([key, title]) => (
                <div key={key}>
                  <label htmlFor={`${prefix}-${key}`}>{title}</label>
                  <input
                    id={`${prefix}-${key}`}
                    type="number"
                    step="any"
                    required
                    value={Number.isFinite(draft.tracking[key]) ? draft.tracking[key] : ""}
                    onChange={(event) => tracking(key, event.target.value === "" ? NaN : Number(event.target.value))}
                  />
                </div>
              ))}
            </div>
            <label htmlFor={`${prefix}-value`}>
              Simulated reading: {Number.isFinite(draft.tracking.value) ? draft.tracking.value : "—"} {draft.tracking.unit}
            </label>
            <input
              id={`${prefix}-value`}
              type="range"
              min={Number.isFinite(draft.tracking.min) ? draft.tracking.min : 0}
              max={Number.isFinite(draft.tracking.max) && draft.tracking.max > draft.tracking.min ? draft.tracking.max : 100}
              step="any"
              value={Number.isFinite(draft.tracking.value) ? draft.tracking.value : 0}
              onChange={(event) => tracking("value", Number(event.target.value))}
            />
          </>
        )}
        {error && <div className="alert error" role="alert">{error}</div>}
        {saved && <p className="hint" role="status">Saved.</p>}
        <div className="form-actions">
          <button type="submit" className="primary" disabled={placing}>Save</button>
          <button
            type="button"
            className="secondary"
            onClick={() => {
              if (placing) {
                onCancelPlace();
                return;
              }
              const message = validate(draft);
              if (message) {
                setError(message);
                return;
              }
              onSave(clone(draft));
              onPlace();
            }}
          >
            {placing ? "Cancel moving" : "Move in office"}
          </button>
        </div>
        <button type="button" className="text-button" disabled={placing} onClick={() => onRemove(device.id)}>Remove from office</button>
        <p className="hint">Saved in this browser only. This doesn’t change the firmware or send sensor data.</p>
      </form>
    </details>
  );
}
