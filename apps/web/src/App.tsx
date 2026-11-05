import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  proposalFingerprint,
  type DeviceEvent,
  type EventResponse,
  type OfficeRequest,
  type Station,
} from "@office/contracts";
import { AgentStage } from "./AgentStage";
import { api } from "./api";
import { isMerged, money, requestTitle, stamp } from "./format";
import { HistoryView } from "./HistoryView";
import { OfficeTwin } from "./OfficeTwin";
import { ProcurementAlerts } from "./ProcurementAlerts";
import WorkQueue from "./WorkQueue";

type Health = {
  agent_provider?: string;
  supplier_provider?: string;
  simulation?: boolean;
  model?: string;
  orders?: string;
};
type Recognition = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  onresult:
    | ((event: {
        results: { [key: number]: { [key: number]: { transcript: string } } };
      }) => void)
    | null;
  onerror: ((event: { error: string }) => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
};
type RecognitionConstructor = new () => Recognition;
const statusLabels: Record<string, string> = {
  processing: "Agent working",
  awaiting_input: "Waiting for details",
  awaiting_approval: "Needs approval",
  needs_clarification: "Needs clarification",
  maintenance_open: "Maintenance open",
  ordered: "Order simulated",
  resolved: "Resolved",
  failed: "Failed",
};

export function App() {
  const [stations, setStations] = useState<Station[]>([]),
    [requests, setRequests] = useState<OfficeRequest[]>([]),
    [health, setHealth] = useState<Health | null>(null);
  const [selected, setSelected] = useState<string | null>(null),
    [text, setText] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [connectionError, setConnectionError] = useState(""),
    [listening, setListening] = useState(false),
    [streamConnected, setStreamConnected] = useState(false),
    [view, setView] = useState<"desk" | "history" | "twin" | "work">("twin"),
    [followed, setFollowed] = useState<string | null>(null);
  const recognition = useRef<Recognition | null>(null);
  const refreshSequence = useRef(0);
  const active = requests.find((request) => request.id === selected);
  const open = requests.filter(
    (r) => !["ordered", "resolved"].includes(r.status),
  );
  const newestOf = (list: OfficeRequest[]) =>
    list.reduce<OfficeRequest | undefined>(
      (newest, r) => (!newest || r.created_at > newest.created_at ? r : newest),
      undefined,
    );
  const newest = newestOf(requests.filter((r) => !isMerged(r)));
  // Hold the screen while someone is working on a request, so a new one can
  // never swap the approval or the form under their hands.
  const holding =
    listening || !!text.trim() || (!!active && open.includes(active));
  const unseen =
    followed !== null && newest && newest.created_at > followed
      ? newest
      : undefined;
  const waitingToOpen = unseen?.id === selected ? undefined : unseen;
  useEffect(() => {
    if (!health) return;
    if (followed === null) {
      setFollowed(newest?.created_at ?? "");
      setSelected(newestOf(open)?.id ?? null);
    } else if (unseen && (unseen.id === selected || !holding)) {
      setFollowed(unseen.created_at);
      setSelected(unseen.id);
    }
  });
  function openRequest(id: string) {
    setSelected(id);
    setText("");
    setNotice("");
    setView("desk");
  }
  const SpeechRecognition =
    (
      window as unknown as {
        SpeechRecognition?: RecognitionConstructor;
        webkitSpeechRecognition?: RecognitionConstructor;
      }
    ).SpeechRecognition ||
    (window as unknown as { webkitSpeechRecognition?: RecognitionConstructor })
      .webkitSpeechRecognition;
  async function refresh() {
    const sequence = ++refreshSequence.current;
    const [nextStations, nextRequests, nextHealth] = await Promise.all([
      api<Station[]>("/stations"),
      api<OfficeRequest[]>("/requests"),
      api<Health>("/health"),
    ]);
    if (sequence !== refreshSequence.current) return;
    setStations(nextStations);
    setRequests(nextRequests);
    setHealth(nextHealth);
    setConnectionError("");
  }
  useEffect(() => {
    let alive = true;
    let fallback: ReturnType<typeof setInterval> | undefined;
    const sync = async () => {
      try {
        if (alive) await refresh();
      } catch (err) {
        if (alive)
          setConnectionError(
            err instanceof Error ? err.message : "Cannot reach backend",
          );
      }
    };
    const startFallback = () => {
      if (fallback === undefined)
        fallback = setInterval(() => void sync(), 5000);
    };
    const stopFallback = () => {
      if (fallback !== undefined) {
        clearInterval(fallback);
        fallback = undefined;
      }
    };
    void sync();
    // Native EventSource reconnects automatically. Poll only while the stream is unavailable.
    startFallback();
    const stream = new EventSource("/api/events");
    stream.onopen = () => {
      if (!alive) return;
      setStreamConnected(true);
      stopFallback();
      void sync();
    };
    stream.addEventListener("requests.updated", () => void sync());
    // Native task and board routes also update the dashboard adapter.
    stream.addEventListener("task.updated", () => void sync());
    stream.addEventListener("state.changed", () => void sync());
    stream.addEventListener("board.listen", () => void sync());
    stream.onerror = () => {
      if (!alive) return;
      setStreamConnected(false);
      startFallback();
    };
    return () => {
      alive = false;
      stream.close();
      stopFallback();
      recognition.current?.stop();
    };
  }, []);
  async function perform(action: () => Promise<void>) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await action();
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  }
  function onEvent(result: EventResponse) {
    setSelected(result.request.id);
    setText("");
    setNotice(
      result.duplicate ? "Already reported. Opened the existing request." : "",
    );
  }
  function press(station: Station) {
    void perform(async () => {
      const event: DeviceEvent = {
        event_id: crypto.randomUUID(),
        station_id: station.id,
        type: "button_pressed",
        timestamp: new Date().toISOString(),
        payload: {},
      };
      onEvent(await api<EventResponse>("/device-events", event));
      setView("desk");
    });
  }
  function stockLow() {
    void perform(async () => {
      const event: DeviceEvent = {
        event_id: crypto.randomUUID(),
        station_id: "bathroom",
        type: "stock_low",
        timestamp: new Date().toISOString(),
        payload: { item_id: "toilet-paper", level: 15 },
      };
      onEvent(await api<EventResponse>("/device-events", event));
    });
  }
  function runScenario(stationId: Station["id"], transcript: string) {
    void perform(async () => {
      const event: DeviceEvent = {
        event_id: crypto.randomUUID(),
        station_id: stationId,
        type: "speech_transcript",
        timestamp: new Date().toISOString(),
        payload: { text: transcript },
      };
      onEvent(await api<EventResponse>("/device-events", event));
    });
  }
  function submit() {
    if (!active || !text.trim()) return;
    void perform(async () => {
      onEvent(
        await api<EventResponse>(`/requests/${active.id}/transcript`, {
          text: text.trim(),
        }),
      );
    });
  }
  function startSpeech() {
    if (!SpeechRecognition) return;
    setError("");
    const instance = new SpeechRecognition();
    recognition.current = instance;
    instance.lang = "en-US";
    instance.continuous = false;
    instance.interimResults = false;
    instance.onresult = (event) => setText(event.results[0][0].transcript);
    instance.onerror = (event) => {
      setError(`Microphone: ${event.error}. You can type the request below.`);
      setListening(false);
    };
    instance.onend = () => setListening(false);
    try {
      instance.start();
      setListening(true);
    } catch {
      setError("Could not start the microphone. Type the request instead.");
    }
  }
  const waitingOnYou = requests.filter(
    (r) => r.status === "awaiting_approval",
  ).length;
  const handled = requests.length - open.length;
  return (
    <div className="desk">
      <h1 className="sr-only">Office operations desk</h1>
      <AgentStage
        request={active}
        stationName={stations.find((s) => s.id === active?.station_id)?.name}
        provider={health?.agent_provider ?? health?.model}
        offline={!!connectionError}
      />
      <main className={`work ${view === "twin" ? "work-twin" : ""}`}>
        {connectionError && (
          <div role="alert" className="alert error">
            Backend unavailable: {connectionError}. Start the API with{" "}
            <code>npm run dev:server</code>.
          </div>
        )}
        {error && (
          <div role="alert" className="alert error">
            {error}
          </div>
        )}
        {notice && (
          <div role="status" className="alert success">
            {notice}
          </div>
        )}
        {waitingToOpen && (
          <div role="status" className="new-request">
            <span>
              New request from{" "}
              {stations.find((s) => s.id === waitingToOpen.station_id)?.name ??
                waitingToOpen.station_id}
              : {requestTitle(waitingToOpen)}
            </span>
            <button
              className="secondary"
              disabled={busy || listening}
              onClick={() => openRequest(waitingToOpen.id)}
            >
              Open
            </button>
          </div>
        )}
        <nav className="views" aria-label="Views">
          <button
            aria-pressed={view === "work"}
            disabled={listening}
            onClick={() => setView("work")}
          >
            Work
          </button>
          <button
            aria-pressed={view === "twin"}
            disabled={listening}
            onClick={() => setView("twin")}
          >
            Office twin
          </button>
          <button
            aria-pressed={view === "desk"}
            disabled={listening}
            onClick={() => setView("desk")}
          >
            Desk
          </button>
          <button
            aria-pressed={view === "history"}
            disabled={listening}
            onClick={() => setView("history")}
          >
            History
          </button>
        </nav>
        <ProcurementAlerts />
        {view === "work" ? (
          <WorkQueue onOpenRequest={openRequest} />
        ) : view === "twin" ? (
          <OfficeTwin
            stations={stations}
            requests={requests}
            connected={streamConnected}
            offline={!!connectionError}
            disabled={busy || listening}
            onPress={press}
            onStockLow={stockLow}
            onScenario={runScenario}
            onRequest={openRequest}
          />
        ) : view === "history" ? (
          <HistoryView
            requests={requests}
            stations={stations}
            statusLabels={statusLabels}
            disabled={busy || listening}
            onOpen={openRequest}
          />
        ) : (
          <>
            <section aria-label="Station simulator">
              <div className="section-head">
                <h2>Stations</h2>
                <span>Simulated hardware</span>
              </div>
              <div className="stations">
                {stations.map((station) => (
                  <button
                    className="station"
                    key={station.id}
                    disabled={busy || listening}
                    onClick={() => press(station)}
                  >
                    <StationIcon id={station.id} />
                    <strong>{station.name}</strong>
                    <small>{station.equipment.join(" · ")}</small>
                  </button>
                ))}
              </div>
              <button
                className="text-button"
                disabled={busy || listening}
                onClick={stockLow}
              >
                Simulate the bathroom sensor at 15% toilet paper
              </button>
            </section>
            {active && (
              <section className="request" aria-label="Request details">
                <div className="section-head">
                  <h2>{requestTitle(active)}</h2>
                  <span>
                    {statusLabels[active.status] ?? active.status}
                  </span>
                </div>
                {active.original_text && (
                  <blockquote>“{active.original_text}”</blockquote>
                )}
                {active.question && <p className="question">{active.question}</p>}
                {(["awaiting_input", "needs_clarification"].includes(
                  active.status,
                ) ||
                  (active.status === "failed" && !active.proposal)) && (
                  <form
                    onSubmit={(e) => {
                      e.preventDefault();
                      submit();
                    }}
                  >
                    <label htmlFor="transcript">What’s missing or broken?</label>
                    <textarea
                      id="transcript"
                      value={text}
                      maxLength={2000}
                      onChange={(e) => setText(e.target.value)}
                      placeholder={
                        active.station_id === "kitchen"
                          ? "We’re out of coffee"
                          : active.station_id === "bathroom"
                            ? "We need more paper"
                            : "The printer is broken"
                      }
                      disabled={busy}
                    />
                    <div className="form-actions">
                      <button
                        type="submit"
                        className="primary"
                        disabled={busy || listening || !text.trim()}
                      >
                        {busy ? "Sending…" : "Send request"}
                      </button>
                      <button
                        type="button"
                        className="secondary"
                        disabled={!SpeechRecognition || busy}
                        onClick={() =>
                          listening ? recognition.current?.stop() : startSpeech()
                        }
                      >
                        {listening ? "Stop listening" : "Use microphone"}
                      </button>
                    </div>
                    <p className="hint">
                      {SpeechRecognition
                        ? "Your browser’s speech service may process the audio. Check the text before sending."
                        : "This browser has no speech recognition. Type your request instead."}
                    </p>
                  </form>
                )}
                {active.proposal && (
                  <div className="proposal">
                    <h3>{active.proposal.product.name}</h3>
                    <dl>
                      <div>
                        <dt>Supplier</dt>
                        <dd>{active.proposal.product.supplier}</dd>
                      </div>
                      <div>
                        <dt>Quantity</dt>
                        <dd>{active.proposal.quantity}</dd>
                      </div>
                      <div>
                        <dt>Delivery</dt>
                        <dd>{active.proposal.product.delivery_estimate}</dd>
                      </div>
                      <div className="total">
                        <dt>Total</dt>
                        <dd>{money(active.proposal.total_nok)}</dd>
                      </div>
                    </dl>
                    {active.status === "awaiting_approval" && (
                      <button
                        className="primary full"
                        disabled={busy}
                        onClick={() =>
                          void perform(async () => {
                            await api<OfficeRequest>(
                              `/requests/${active.id}/approve`,
                              {
                                proposal_fingerprint: proposalFingerprint(
                                  active.proposal!,
                                ),
                              },
                            );
                            setNotice(
                              "Simulated order confirmed. No money was spent.",
                            );
                          })
                        }
                      >
                        {busy ? "Processing…" : "Approve simulated order"}
                      </button>
                    )}
                    <p className="hint">
                      Simulated catalogue and estimated delivery. This does not place a
                      real order.
                    </p>
                  </div>
                )}
                {active.status === "failed" && active.proposal && (
                  <div className="alert error">
                    Order outcome uncertain. Check the supplier and reconcile this
                    request before attempting another purchase. Retrying is
                    disabled.
                  </div>
                )}
                {active.status === "ordered" && (
                  <button
                    className="primary full"
                    disabled={busy}
                    onClick={() =>
                      void perform(async () => {
                        await api<OfficeRequest>(
                          `/requests/${active.id}/resolve`,
                          {},
                        );
                        setNotice(
                          "Simulated delivery confirmed. A new purchase can now be requested.",
                        );
                      })
                    }
                  >
                    Confirm simulated delivery
                  </button>
                )}
                {active.assigned_to && (
                  <p className="fact">
                    <span>Assigned to</span> {active.assigned_to}
                  </p>
                )}
                {active.status === "maintenance_open" && (
                  <button
                    className="primary full"
                    disabled={busy}
                    onClick={() =>
                      void perform(async () => {
                        await api<OfficeRequest>(
                          `/requests/${active.id}/resolve`,
                          {},
                        );
                        setNotice("Maintenance request marked resolved.");
                      })
                    }
                  >
                    Mark resolved
                  </button>
                )}
                {active.supplier_reference && (
                  <p className="fact">
                    <span>Simulated reference</span>{" "}
                    <code>{active.supplier_reference}</code>
                  </p>
                )}
                <p className="fact">
                  <span>Requester</span>{" "}
                  {active.user_id ?? "Unknown, no speaker identification"}
                </p>
                {active.trace && active.trace.length > 0 && (
                  <details className="log">
                    <summary>All steps ({active.trace.length})</summary>
                    <ol>
                      {active.trace.map((entry, index) => (
                        <li key={`${entry.at}-${index}`}>
                          <time dateTime={entry.at}>{stamp(entry.at)}</time>
                          <span>{entry.detail}</span>
                        </li>
                      ))}
                    </ol>
                  </details>
                )}
              </section>
            )}
            <section aria-label="Request queue">
              <div className="section-head">
                <h2>Requests</h2>
                <span className="tally">
                  {open.length} open · {waitingOnYou} to approve · {handled} handled
                </span>
              </div>
              {requests.length === 0 ? (
                <p className="empty">Nothing yet. Press a station to start one.</p>
              ) : (
                <div className="queue">
                  {requests.map((request) => (
                    <button
                      key={request.id}
                      className={`row ${selected === request.id ? "active" : ""}`}
                      aria-pressed={selected === request.id}
                      disabled={busy || listening}
                      onClick={() => openRequest(request.id)}
                    >
                      <i className={`dot dot-${request.status}`} aria-hidden="true" />
                      <span className="row-main">
                        <strong>{requestTitle(request)}</strong>
                        <small>
                          {stations.find((s) => s.id === request.station_id)?.name ??
                            request.station_id}{" "}
                          · {stamp(request.created_at)}
                        </small>
                      </span>
                      <span className="row-status">
                        {statusLabels[request.status] ?? request.status}
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </section>
          </>
        )}
        <footer>
          <span>
            {connectionError
              ? "Disconnected"
              : streamConnected
                ? "Live updates connected"
                : "Reconnecting, checking every 5 seconds"}
          </span>
          <span>
            Agent: {health?.agent_provider ?? health?.model ?? "…"} · Supplier:{" "}
            {health?.supplier_provider ?? health?.orders ?? "…"}
          </span>
        </footer>
      </main>
    </div>
  );
}

function StationIcon({ id }: { id: string }) {
  const paths: Record<string, ReactNode> = {
    kitchen: (
      <>
        <path d="M5 9h12v6a5 5 0 0 1-5 5h-2a5 5 0 0 1-5-5V9Z" />
        <path d="M17 11h1.5a2.5 2.5 0 0 1 0 5H17" />
        <path d="M9 3v2.5M13 3v2.5" />
      </>
    ),
    bathroom: (
      <>
        <ellipse cx="8" cy="9" rx="4" ry="5" />
        <ellipse cx="8" cy="9" rx="1.25" ry="1.75" />
        <path d="M8 4h9c2.2 0 4 2.2 4 5v11h-9" />
        <path d="M12 9v11" />
      </>
    ),
    printer: (
      <>
        <path d="M7 8V3h10v5" />
        <path d="M7 17H4V8h16v9h-3" />
        <path d="M7 14h10v7H7z" />
      </>
    ),
  };
  return (
    <svg
      className="station-icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {paths[id]}
    </svg>
  );
}
