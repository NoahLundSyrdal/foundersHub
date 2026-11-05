import { useEffect, useState } from "react";
import type { FacilityState } from "@office/contracts";
import { api } from "./api";
import type { EnvironmentState } from "./EnvironmentReadings";

/** One read-only poll for all placements, paused when the twin is hidden. */
export function useEnvironment(hidden: boolean): EnvironmentState {
  const [state, setState] = useState<EnvironmentState>({ snapshot: null, now: Date.now(), unavailable: false });
  useEffect(() => {
    if (hidden) return;
    let active = true;
    let pending = false;
    let clockOffset = 0;
    const refresh = async () => {
      if (pending) return;
      pending = true;
      try {
        const snapshot = await api<FacilityState>("/facility");
        if (!active) return;
        clockOffset = Date.parse(snapshot.evaluated_at) - Date.now();
        setState({ snapshot, now: Date.now() + clockOffset, unavailable: false });
      } catch {
        if (active) setState((previous) => ({ ...previous, now: Date.now() + clockOffset, unavailable: true }));
      } finally { pending = false; }
    };
    const stream = new EventSource("/api/events");
    stream.addEventListener("facility.updated", refresh);
    stream.addEventListener("state.changed", refresh);
    stream.onopen = refresh;
    void refresh();
    const poll = setInterval(() => {
      // Expire readings even while a network request is hanging.
      setState((previous) => ({ ...previous, now: Date.now() + clockOffset }));
      void refresh();
    }, 5000);
    return () => { active = false; clearInterval(poll); stream.close(); };
  }, [hidden]);
  return state;
}
