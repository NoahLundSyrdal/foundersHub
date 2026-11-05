import type { Station } from "@office/contracts";

export type TwinDevice = {
  id: string;
  name: string;
  kind: "button" | "level";
  stationId: Station["id"];
  position: { x: number; z: number };
  tracking: { label: string; unit: string; min: number; max: number; value: number; warningBelow: number };
};
const storageKey = "office-twin-devices-v1";
export const defaultTwinDevices: TwinDevice[] = [
  { id: "stage-button", name: "Stage button", kind: "button", stationId: "kitchen", position: { x: -5.65, z: 6.7 }, tracking: { label: "Button input", unit: "", min: 0, max: 100, value: 0, warningBelow: 15 } },
  { id: "coffee-level-demo", name: "Coffee level", kind: "level", stationId: "kitchen", position: { x: 5.25, z: -1.5 }, tracking: { label: "Coffee remaining", unit: "%", min: 0, max: 100, value: 62, warningBelow: 20 } },
  { id: "bathroom-level-demo", name: "Paper level", kind: "level", stationId: "bathroom", position: { x: 7.15, z: -5.35 }, tracking: { label: "Paper remaining", unit: "%", min: 0, max: 100, value: 28, warningBelow: 20 } },
];
function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
function finite(value: unknown): value is number { return typeof value === "number" && Number.isFinite(value); }
function text(value: unknown, max: number, empty = false): value is string {
  return typeof value === "string" && value.length <= max && (empty || value.trim().length > 0);
}
function parseDevice(value: unknown): TwinDevice | null {
  if (!record(value) || !text(value.id, 100) || !text(value.name, 80) || (value.kind !== "button" && value.kind !== "level") || (value.stationId !== "kitchen" && value.stationId !== "bathroom" && value.stationId !== "printer")) return null;
  const p = value.position, t = value.tracking;
  if (!record(p) || !finite(p.x) || !finite(p.z) || p.x < -10 || p.x > 10 || p.z < -7 || p.z > 7) return null;
  if (!record(t) || !text(t.label, 60) || !text(t.unit, 16, true) || !finite(t.min) || !finite(t.max) || !finite(t.value) || !finite(t.warningBelow) || t.min >= t.max || !Number.isFinite(t.max - t.min) || t.value < t.min || t.value > t.max || t.warningBelow < t.min || t.warningBelow > t.max) return null;
  return { id: value.id, name: value.name.trim(), kind: value.kind as TwinDevice["kind"], stationId: value.stationId as Station["id"], position: { x: p.x, z: p.z }, tracking: { label: t.label.trim(), unit: t.unit.trim(), min: t.min, max: t.max, value: t.value, warningBelow: t.warningBelow } };
}
function defaults() { return defaultTwinDevices.map((device) => ({ ...device, position: { ...device.position }, tracking: { ...device.tracking } })); }
function parseDevices(value: unknown): TwinDevice[] | null {
  if (!Array.isArray(value) || value.length > 20) return null;
  const parsed = value.map(parseDevice);
  if (parsed.some((device) => device === null)) return null;
  const devices = parsed as TwinDevice[];
  return new Set(devices.map((device) => device.id)).size === devices.length ? devices : null;
}
export function loadTwinDevices(): TwinDevice[] {
  try {
    const raw = localStorage.getItem(storageKey);
    const devices = raw === null ? defaults() : parseDevices(JSON.parse(raw)) ?? defaults();
    const previousNames: Record<string, string> = { "stage-button": "Demo stage button", "coffee-level-demo": "Coffee level · demo", "bathroom-level-demo": "Paper level · demo" };
    return devices.map((device) => device.name === previousNames[device.id] ? { ...device, name: defaultTwinDevices.find((item) => item.id === device.id)!.name } : device);
  } catch { return defaults(); }
}
export function saveTwinDevices(devices: TwinDevice[]): boolean {
  try {
    const parsed = parseDevices(devices);
    if (!parsed) return false;
    localStorage.setItem(storageKey, JSON.stringify(parsed));
    return true;
  } catch { return false; }
}
export function createTwinDevice(kind: TwinDevice["kind"], ordinal: number): TwinDevice {
  return { id: crypto.randomUUID(), name: `${kind === "button" ? "Button" : "Level sensor"} ${ordinal}`, kind, stationId: "kitchen", position: { x: -5.65, z: 6.7 }, tracking: { label: kind === "button" ? "Button input" : "Stock remaining", unit: kind === "button" ? "" : "%", min: 0, max: 100, value: kind === "button" ? 0 : 50, warningBelow: 20 } };
}
