import { useEffect, useRef, useState } from "react";
import {
  proposalFingerprint,
  type DeviceEvent,
  type EventResponse,
  type OfficeRequest,
  type Station,
} from "@office/contracts";
import { api } from "./api";

type Health = {
  agent_provider: string;
  supplier_provider: string;
  simulation: true;
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
const stationIcons: Record<string, string> = {
  kitchen: "☕",
  bathroom: "◉",
  printer: "▤",
};
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
const money = (value: number) =>
  new Intl.NumberFormat("en-NO", {
    style: "currency",
    currency: "NOK",
    maximumFractionDigits: 0,
  }).format(value);
const stamp = (value: string) =>
  new Date(value).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
  });

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
    [streamConnected, setStreamConnected] = useState(false);
  const recognition = useRef<Recognition | null>(null);
  const refreshSequence = useRef(0);
  const active = requests.find((request) => request.id === selected);
  useEffect(() => {
    if (selected === null) {
      const incoming = requests.find(
        (request) => request.status === "awaiting_input",
      );
      if (incoming) setSelected(incoming.id);
    }
  }, [requests, selected]);
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
      result.duplicate
        ? "Already reported. Opened the existing request."
        : result.command.text,
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
  const open = requests.filter(
    (r) => !["ordered", "resolved"].includes(r.status),
  );
  return (
    <div className="shell">
      <aside className="sidebar">
        <a className="brand" href="/" aria-label="Office home">
          <span className="brand-mark">
            o<span />
          </span>
          office<span className="brand-dot">.</span>
        </a>
        <div className="workspace">
          <span className="avatar">FH</span>
          <div>
            Founders Hub<small>Demo workspace</small>
          </div>
          <span className="chevron">⌄</span>
        </div>
        <div className="nav-label">WORKSPACE</div>
        <div className="nav-item selected">
          <span>▦</span>Operations desk
          <span className="nav-count">{open.length}</span>
        </div>
        <div className="sidebar-note">
          <span className="small-orb" /> ONE PLACE FOR THE LITTLE THINGS
          <p>From “we’re out” to “it’s handled.”</p>
        </div>
        <div className="sidebar-bottom">
          <span className="avatar small">4</span>
          <div>
            Built together<small>Founders Hub × DNB</small>
          </div>
        </div>
      </aside>
      <main>
        <header className="topbar">
          <span>
            Workspace <span className="slash">/</span> Operations desk
          </span>
          <span className="local-tag">
            <i /> Local development
          </span>
        </header>
        <div className="content">
          <div className="page-heading">
            <div>
              <div className="eyebrow">YOUR OFFICE, TAKEN CARE OF</div>
              <h1>
                Operations desk<span>.</span>
              </h1>
              <p>Small requests. Clear ownership. Nothing forgotten.</p>
            </div>
            <div className="mode-badge">
              <span>◈</span>
              <div>
                Simulation mode<small>No purchases or bookings are made</small>
              </div>
            </div>
          </div>
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
          <section className="stats" aria-label="Request overview">
            <div>
              <span>Open requests</span>
              <strong>{open.length.toString().padStart(2, "0")}</strong>
              <small>Across all stations</small>
            </div>
            <div>
              <span>Awaiting approval</span>
              <strong>
                {requests
                  .filter((r) => r.status === "awaiting_approval")
                  .length.toString()
                  .padStart(2, "0")}
              </strong>
              <small>Your next move</small>
            </div>
            <div>
              <span>Handled</span>
              <strong>
                {requests
                  .filter((r) => ["ordered", "resolved"].includes(r.status))
                  .length.toString()
                  .padStart(2, "0")}
              </strong>
              <small>Simulated orders + resolved issues</small>
            </div>
          </section>
          <div className="section-heading">
            <div>
              <h2>Your stations</h2>
              <p>Press a button to start a request from that location.</p>
            </div>
            <span className="quiet-label">HARDWARE SIMULATOR</span>
          </div>
          <section className="stations" aria-label="Station simulator">
            {stations.map((station) => (
              <button
                className="station"
                key={station.id}
                disabled={busy || listening}
                onClick={() => press(station)}
              >
                <div className="station-top">
                  <span className="station-icon">
                    {stationIcons[station.id]}
                  </span>
                  <span className="online">
                    <i /> Virtual station
                  </span>
                </div>
                <h3>{station.name}</h3>
                <p>{station.equipment.join(" · ")}</p>
                <div className="station-bottom">
                  <span>Press to report</span>
                  <span className="physical-button" aria-hidden="true">
                    ↗
                  </span>
                </div>
              </button>
            ))}
          </section>
          <div className="sensor-row">
            <div>
              <span className="sensor-icon">⌁</span>
              <div>
                <strong>Automatic stock detection</strong>
                <p>
                  Simulate a bathroom sensor reporting 15% toilet paper
                  remaining.
                </p>
              </div>
            </div>
            <button
              className="secondary"
              disabled={busy || listening}
              onClick={stockLow}
            >
              Trigger low stock <span>↗</span>
            </button>
          </div>
          <div className="request-layout">
            <section className="panel queue">
              <div className="panel-heading">
                <div>
                  <h2>Request queue</h2>
                  <p>Everything coming in, in one place.</p>
                </div>
                <span className="count">{requests.length}</span>
              </div>
              {requests.length === 0 ? (
                <div className="empty">
                  <span>↗</span>
                  <h3>A quiet office. For now.</h3>
                  <p>
                    Press a station button or trigger the sensor to see your
                    first request here.
                  </p>
                </div>
              ) : (
                <div className="request-list">
                  {requests.map((request) => (
                    <button
                      key={request.id}
                      className={`request-row ${selected === request.id ? "active" : ""}`}
                      disabled={busy || listening}
                      onClick={() => {
                        setSelected(request.id);
                        setText("");
                        setNotice("");
                      }}
                    >
                      <span className="request-icon">
                        {stationIcons[request.station_id]}
                      </span>
                      <div className="request-summary">
                        <strong>{request.summary || "New request"}</strong>
                        <span>
                          {stations.find((s) => s.id === request.station_id)
                            ?.name ?? request.station_id}{" "}
                          <span className="separator">·</span>{" "}
                          {stamp(request.created_at)}
                        </span>
                      </div>
                      <span className={`status status-${request.status}`}>
                        {statusLabels[request.status] ?? request.status}
                      </span>
                    </button>
                  ))}
                </div>
              )}
            </section>
            <section className="panel detail" aria-label="Request details">
              <div className="panel-heading">
                <h2>{active ? "Request details" : "Ready when you are"}</h2>
                <span className="detail-icon">↗</span>
              </div>
              {!active ? (
                <div className="empty detail-empty">
                  <span>◎</span>
                  <h3>Give the office a voice.</h3>
                  <p>
                    Select a station, tell us what’s needed, and follow the
                    request through approval.
                  </p>
                </div>
              ) : (
                <div className="detail-content">
                  <div className="detail-location">
                    <span>{stationIcons[active.station_id]}</span>
                    <div>
                      <strong>
                        {stations.find((s) => s.id === active.station_id)
                          ?.name ?? active.station_id}
                      </strong>
                      <small>
                        Requester:{" "}
                        {active.user_id ??
                          "Unknown · no speaker identification"}
                      </small>
                    </div>
                  </div>
                  <span className={`status status-${active.status}`}>
                    {statusLabels[active.status]}
                  </span>
                  <h3 className="detail-title">{active.summary}</h3>
                  {active.original_text && (
                    <blockquote>“{active.original_text}”</blockquote>
                  )}
                  {active.question && (
                    <div className="question">{active.question}</div>
                  )}
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
                      <label htmlFor="transcript">
                        What’s missing or broken?
                      </label>
                      <textarea
                        id="transcript"
                        value={text}
                        maxLength={2000}
                        onChange={(e) => setText(e.target.value)}
                        placeholder={
                          active.station_id === "kitchen"
                            ? "e.g. We’re out of coffee"
                            : active.station_id === "bathroom"
                              ? "e.g. We need more paper"
                              : "e.g. The printer is broken"
                        }
                        disabled={busy}
                      />
                      <div className="input-actions">
                        <button
                          type="button"
                          className="secondary"
                          disabled={!SpeechRecognition || busy}
                          onClick={() =>
                            listening
                              ? recognition.current?.stop()
                              : startSpeech()
                          }
                        >
                          {listening ? "■ Stop listening" : "◉ Use microphone"}
                        </button>
                        <button
                          type="submit"
                          className="primary"
                          disabled={busy || listening || !text.trim()}
                        >
                          {busy ? "Working…" : "Send request →"}
                        </button>
                      </div>
                      <p className="hint">
                        {SpeechRecognition
                          ? "Optional browser speech recognition; audio may be processed by your browser’s speech service. Review the text before sending."
                          : "Speech recognition is unavailable in this browser. Type your request to continue."}
                      </p>
                    </form>
                  )}
                  {active.status === "processing" && (
                    <div role="status" className="processing-note">
                      <span className="processing-orb" /> Agent is checking the
                      request and preparing the next action…
                    </div>
                  )}
                  {active.trace && active.trace.length > 0 && (
                    <section className="activity" aria-label="Agent activity">
                      <h4>Activity</h4>
                      <ol>
                        {active.trace.map((entry, index) => (
                          <li key={`${entry.at}-${index}`}>
                            <span className="activity-dot" />
                            <div>
                              <strong>
                                {entry.stage.replaceAll("_", " ")}
                              </strong>
                              <p>{entry.detail}</p>
                            </div>
                            <time dateTime={entry.at}>{stamp(entry.at)}</time>
                          </li>
                        ))}
                      </ol>
                    </section>
                  )}
                  {active.proposal && (
                    <div className="proposal">
                      <div className="proposal-eyebrow">
                        PROPOSED PURCHASE · SIMULATED
                      </div>
                      <h4>{active.proposal.product.name}</h4>
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
                          {busy ? "Processing…" : "Approve simulated order →"}
                        </button>
                      )}
                      <p className="hint">
                        Demo catalogue and estimated delivery. This does not
                        place a real order.
                      </p>
                    </div>
                  )}
                  {active.status === "failed" && active.proposal && (
                    <div className="alert error">
                      Order outcome uncertain. Check the supplier and reconcile
                      this request before attempting another purchase. Retrying
                      is disabled.
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
                      Confirm simulated delivery ✓
                    </button>
                  )}
                  {active.assigned_to && (
                    <div className="assignment">
                      <span>Assigned to</span>
                      <strong>{active.assigned_to}</strong>
                    </div>
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
                      Mark resolved ✓
                    </button>
                  )}
                  {active.supplier_reference && (
                    <div className="confirmation">
                      <strong>✓ Simulated confirmation</strong>
                      <code>{active.supplier_reference}</code>
                      <p>No real order was placed.</p>
                    </div>
                  )}
                  <div className="request-id">
                    REQUEST {active.id.slice(0, 8).toUpperCase()}
                  </div>
                </div>
              )}
            </section>
          </div>
          <footer>
            <span>
              <i className={connectionError ? "offline-dot" : "live-dot"} />{" "}
              {connectionError
                ? "Disconnected"
                : streamConnected
                  ? "Live updates connected"
                  : "Reconnecting live updates · checks every 5s"}
            </span>
            <span>
              Agent: {health?.agent_provider ?? "…"}{" "}
              <span className="separator">·</span> Supplier:{" "}
              {health?.supplier_provider ?? "…"}
            </span>
          </footer>
        </div>
      </main>
    </div>
  );
}
