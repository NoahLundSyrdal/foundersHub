import { useEffect, useRef, useState } from "react";
import type { Alert } from "@office/contracts";
import "./procurement-alerts.css";

const VISIBLE_LIMIT = 3;

async function readJson<T>(path: string, signal: AbortSignal, body?: unknown): Promise<T> {
  // The shared api helper has no AbortSignal support; this view cancels its own requests.
  const response = await fetch(`/api${path}`, {
    signal,
    ...(body === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }),
  });
  if (!response.ok) throw new Error("Alert request failed");
  return response.json() as Promise<T>;
}

/** Acknowledgement only records that a dashboard message was seen. */
export function ProcurementAlerts() {
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [loadError, setLoadError] = useState("");
  const [actionError, setActionError] = useState("");
  const [sending, setSending] = useState<Set<string>>(new Set());
  const [expanded, setExpanded] = useState<boolean | null>(null);
  const refreshRef = useRef<() => void>(() => {});
  const mounted = useRef(false);
  const requests = useRef(new Set<AbortController>());
  const pendingAcknowledgements = useRef(new Set<string>());

  useEffect(() => {
    mounted.current = true;
    let disposed = false;
    let version = 0;
    let reading: AbortController | undefined;
    let fallback: ReturnType<typeof setInterval> | undefined;
    const refresh = () => {
      reading?.abort();
      const controller = new AbortController();
      reading = controller;
      const current = ++version;
      requests.current.add(controller);
      void readJson<Alert[]>("/alerts", controller.signal).then((result) => {
        if (!disposed && !controller.signal.aborted && current === version) {
          setAlerts(result);
          setLoadError("");
        }
      }).catch(() => {
        if (!disposed && !controller.signal.aborted && current === version)
          setLoadError("Alerts could not be refreshed. Previously loaded messages remain visible.");
      }).finally(() => requests.current.delete(controller));
    };
    refreshRef.current = refresh;
    const startFallback = () => {
      if (fallback === undefined) fallback = setInterval(refresh, 5000);
    };
    const stopFallback = () => {
      if (fallback !== undefined) clearInterval(fallback);
      fallback = undefined;
    };
    refresh();
    startFallback();
    // The stream can hang silently after an API restart, so re-read regularly too.
    const renewal = setInterval(refresh, 20000);
    let stream: EventSource | undefined;
    try {
      stream = new EventSource("/api/events");
      stream.addEventListener("requests.updated", refresh);
      stream.addEventListener("task.updated", refresh);
      stream.addEventListener("state.changed", refresh);
      stream.onopen = () => {
        if (disposed) return;
        stopFallback();
        refresh();
      };
      stream.onerror = () => {
        if (disposed) return;
        startFallback();
      };
    } catch {
      // The polling fallback keeps running.
    }
    return () => {
      disposed = true;
      mounted.current = false;
      refreshRef.current = () => {};
      clearInterval(renewal);
      stopFallback();
      stream?.close();
      for (const controller of requests.current) controller.abort();
      requests.current.clear();
      pendingAcknowledgements.current.clear();
    };
  }, []);

  async function acknowledge(id: string) {
    if (pendingAcknowledgements.current.has(id)) return;
    pendingAcknowledgements.current.add(id);
    setSending(new Set(pendingAcknowledgements.current));
    setActionError("");
    const controller = new AbortController();
    requests.current.add(controller);
    try {
      const result = await readJson<Alert>(`/alerts/${encodeURIComponent(id)}/acknowledge`, controller.signal, {});
      if (mounted.current && !controller.signal.aborted) {
        // Invalidate any pre-acknowledgement refresh before applying the confirmed result.
        refreshRef.current();
        setAlerts((current) => current.map((alert) => alert.id === id ? result : alert));
      }
    } catch {
      if (mounted.current && !controller.signal.aborted)
        setActionError("Acknowledgement failed. The message remains visible; you can try again.");
    } finally {
      requests.current.delete(controller);
      pendingAcknowledgements.current.delete(id);
      if (mounted.current && !controller.signal.aborted)
        setSending(new Set(pendingAcknowledgements.current));
    }
  }

  const outstanding = alerts.filter((alert) => !alert.acknowledged_at &&
    (alert.severity === "urgent" || alert.severity === "critical"))
    .sort((a, b) => Number(b.severity === "critical") - Number(a.severity === "critical") ||
      b.created_at.localeCompare(a.created_at));
  // One line unless something is critical, so alerts don't push the office out of view.
  const critical = outstanding.some((alert) => alert.severity === "critical");
  // A new critical alert reopens the bar, even if someone collapsed it earlier.
  const criticalKey = outstanding.filter((alert) => alert.severity === "critical").map((alert) => alert.id).join();
  useEffect(() => setExpanded(null), [criticalKey]);
  if (!outstanding.length && !loadError && !actionError) return null;
  const top = outstanding[0];

  return (
    <section className="procurement-alerts" aria-label="Procurement alerts">
      <details open={expanded ?? (critical || !!actionError)} onToggle={(event) => setExpanded(event.currentTarget.open)}>
        <summary>
          <strong>Attention needed</strong>
          <span className="procurement-alerts__count">{outstanding.length} unacknowledged</span>
          {top && <span className="procurement-alerts__top"><b>{top.severity}:</b> {top.title}</span>}
        </summary>
        <p className="procurement-alerts__explanation">
          Acknowledging marks a message as seen. It does not approve spending or resolve the issue.
        </p>
        {loadError && <p className="procurement-alerts__error" role="status">{loadError}</p>}
        {actionError && <p className="procurement-alerts__error" role="alert">{actionError}</p>}
        <ul className="procurement-alerts__list">
          {outstanding.slice(0, VISIBLE_LIMIT).map((alert) => (
            <li key={alert.id} className={`procurement-alerts__item procurement-alerts__item--${alert.severity}`}>
              <div className="procurement-alerts__message" role={alert.severity === "critical" ? "alert" : "status"}>
                <div className="procurement-alerts__heading">
                  <span className="procurement-alerts__severity">{alert.severity}</span>
                  <strong>{alert.title}</strong>
                </div>
                <p>{alert.detail}</p>
              </div>
              <button className="procurement-alerts__ack" type="button" disabled={sending.has(alert.id)}
                aria-label={`Acknowledge message: ${alert.title}`} onClick={() => void acknowledge(alert.id)}>
                {sending.has(alert.id) ? "Saving…" : "Acknowledge"}
              </button>
            </li>
          ))}
        </ul>
        {outstanding.length > VISIBLE_LIMIT && (
          <p className="procurement-alerts__connection">
            Showing {VISIBLE_LIMIT} of {outstanding.length}. Acknowledging reveals the next one.
          </p>
        )}
      </details>
    </section>
  );
}
