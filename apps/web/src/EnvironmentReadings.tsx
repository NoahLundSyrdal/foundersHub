import type { FacilityState } from "@office/contracts";
import { environmentReadings } from "./environmentTelemetry";

export type EnvironmentState = { snapshot: FacilityState | null; now: number; unavailable: boolean };

export function EnvironmentReadings({ boardId, location, state }: { boardId: string; location: string; state: EnvironmentState }) {
  return <section className="twin-environment" aria-label="Environmental readings">
    <h3>Environment</h3>
    <p className="hint">{location} · location readings</p>
    {state.unavailable && <p role="status" className="hint">Telemetry unavailable. Values below are last received readings.</p>}
    <dl className="twin-environment-grid">
      {environmentReadings(state.snapshot?.readings ?? [], boardId, state.now).map(({ metric, label, unit, reading, stale }) => <div key={metric}>
        <dt>{label}</dt>
        <dd>{reading ? <>{reading.value.toLocaleString(undefined, { maximumFractionDigits: metric === "co2_ppm" ? 0 : 1 })} <small>{unit}</small></> : <span className="environment-empty">No reading</span>}
        {reading && <span className="environment-status">{`${reading.simulation ? "Simulated" : "Physical"} · ${state.unavailable ? "Offline" : stale ? "Stale" : "Fresh"}`}</span>}
        {reading && <time dateTime={reading.timestamp}>{new Date(reading.timestamp).toLocaleTimeString()}</time>}
        </dd>
      </div>)}
    </dl>
  </section>;
}
