import type { FacilityReading } from "@office/contracts";

export const environmentMetrics = [
  { metric: "temperature_c", label: "Temperature", unit: "°C" },
  { metric: "co2_ppm", label: "CO₂", unit: "ppm" },
  { metric: "humidity_pct", label: "Air humidity", unit: "% RH" },
] as const;

export function environmentReadings(readings: FacilityReading[], boardId: string, now: number) {
  return environmentMetrics.map(({ metric, label, unit }) => {
    const matching = readings.filter((r) => r.board_id === boardId && r.metric === metric && Number.isFinite(r.value) && Number.isFinite(Date.parse(r.timestamp)));
    // A simulation must never replace a physical reading, even if it is newer.
    const physical = matching.filter((r) => !r.simulation);
    const reading = (physical.length ? physical : matching).reduce<FacilityReading | undefined>(
      (latest, r) => !latest || Date.parse(r.timestamp) > Date.parse(latest.timestamp) ? r : latest, undefined);
    const age = reading ? now - Date.parse(reading.timestamp) : Infinity;
    // Same freshness window as the backend air-quality endpoint.
    return { metric, label, unit, reading, stale: age >= 30_000 || age < -5000 };
  });
}
