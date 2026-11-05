import type { ChatMessage } from "@office/contracts";
import { useEffect, useRef, useState } from "react";
import {
  proposalFingerprint,
  type DeviceEvent,
  type EventResponse,
  type OfficeRequest,
  type Station,
} from "@office/contracts";
import { AgentChat } from "./AgentChat";
import { AgentStage } from "./AgentStage";
import { api } from "./api";
import { inLoop, isOpen, isRecentChat, mostUrgent, statusLabels } from "./format";
import { HistoryView } from "./HistoryView";
import { OfficeTwin } from "./OfficeTwin";
import { ProcurementAlerts } from "./ProcurementAlerts";
import { TicketsView } from "./TicketsView";
import WorkQueue from "./WorkQueue";
import { OdaConnection, OdaFoodOrder } from "./OdaFoodOrder";

type Health = {
  agent_provider?: string;
  supplier_provider?: string;
  simulation?: boolean;
  model?: string;
  orders?: string;
};
const STREAM_RENEW_MS = 20000;
const newestOf = (list: OfficeRequest[]) =>
  list.reduce<OfficeRequest | undefined>(
    (newest, r) => (!newest || r.created_at > newest.created_at ? r : newest),
    undefined,
  );

export function App() {
  const [conversationId] = useState(() => {
    const saved = localStorage.getItem("office-conversation-id");
    if (saved) return saved;
    const id = crypto.randomUUID();
    localStorage.setItem("office-conversation-id", id);
    return id;
  });
  const [chatRequests, setChatRequests] = useState<OfficeRequest[]>([]);
  const pendingChat = useRef<{text: string; station: string; id: string} | null>(null);
  const [stations, setStations] = useState<Station[]>([]),
    [requests, setRequests] = useState<OfficeRequest[]>([]),
    [health, setHealth] = useState<Health | null>(null);
  const [opened, setOpened] = useState<string | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [connectionError, setConnectionError] = useState(""),
    [streamConnected, setStreamConnected] = useState(false),
    [view, setView] = useState<"twin" | "tickets" | "history" | "work">("twin"),
    [where, setWhere] = useState<Station["id"]>("office"),
    [askCount, setAskCount] = useState(0),
    [openCount, setOpenCount] = useState(0);
  const refreshSequence = useRef(0);
  // The agent follows live work and the current conversation. Otherwise it shows whatever
  // most needs attention, or the order just placed, and rests when nothing is left.
  const newest = newestOf(requests);
  const conversing =
    newest && !inLoop(newest) && newest.status !== "resolved" && isRecentChat(newest);
  const focus =
    newest && (newest.status === "processing" || conversing)
      ? newest
      : (mostUrgent(requests.filter((r) => isOpen(r) && inLoop(r))) ??
        (newest?.status === "ordered" ? newest : undefined));
  async function refresh() {
    const sequence = ++refreshSequence.current;
    const [nextStations, nextRequests, nextHealth, chat] = await Promise.all([
      api<Station[]>("/stations"),
      api<OfficeRequest[]>("/requests"),
      api<Health>("/health"),
      api<{requests: OfficeRequest[]}>(`/chat/${encodeURIComponent(conversationId)}`),
    ]);
    if (sequence !== refreshSequence.current) return;
    setStations(nextStations);
    setRequests(nextRequests);
    setHealth(nextHealth);
    setChatRequests(chat.requests);
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
    // Poll while the stream is down. A stream can also hang silently (a proxy keeps it open
    // after the API restarts), so it is renewed regularly; opening it re-reads the state.
    startFallback();
    let stream: EventSource | undefined;
    const connect = () => {
      stream?.close();
      const next = new EventSource("/api/events");
      stream = next;
      next.onopen = () => {
        if (!alive) return;
        setStreamConnected(true);
        stopFallback();
        void sync();
      };
      next.addEventListener("requests.updated", () => void sync());
      // Native task and board routes also update the dashboard adapter.
      next.addEventListener("task.updated", () => void sync());
      next.addEventListener("state.changed", () => void sync());
      next.addEventListener("board.listen", () => void sync());
      next.onerror = () => {
        if (!alive) return;
        setStreamConnected(false);
        startFallback();
      };
    };
    connect();
    const renewal = setInterval(connect, STREAM_RENEW_MS);
    return () => {
      alive = false;
      clearInterval(renewal);
      stream?.close();
      stopFallback();
    };
  }, []);
  async function perform(action: () => Promise<void>): Promise<boolean> {
    setBusy(true);
    setError("");
    try {
      await action();
      await refresh();
      return true;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
      return false;
    } finally {
      setBusy(false);
    }
  }
  function sendEvent(
    stationId: Station["id"],
    type: DeviceEvent["type"],
    payload: DeviceEvent["payload"],
  ) {
    return api<EventResponse>("/device-events", {
      event_id: crypto.randomUUID(),
      station_id: stationId,
      type,
      timestamp: new Date().toISOString(),
      payload,
    } as DeviceEvent);
  }
  function openTicket(id: string) {
    setOpened(id);
    setOpenCount((count) => count + 1);
    setView("tickets");
  }
  const approve = (request: OfficeRequest) =>
    void perform(async () => {
      await api<OfficeRequest>(`/requests/${request.id}/approve`, {
        proposal_fingerprint: proposalFingerprint(request.proposal!),
      });
    });
  const reject = (request: OfficeRequest) =>
    void perform(async () => {
      await api<OfficeRequest>(`/requests/${request.id}/reject`, {});
    });
  const requote = (request: OfficeRequest) =>
    void perform(async () => {
      await api<OfficeRequest>(`/requests/${request.id}/requote`, {
        proposal_fingerprint: proposalFingerprint(request.proposal!),
      });
    });
  const resolve = (request: OfficeRequest) =>
    void perform(async () => {
      await api<OfficeRequest>(`/requests/${request.id}/resolve`, {});
    });
  const mockAgent = (health?.agent_provider ?? health?.model) === "mock";
  return (
    <div className="desk">
      <h1 className="sr-only">Office status</h1>
      <AgentStage
        agentMode={!health ? "connecting" : mockAgent ? "mock" : "live"}
        request={focus}
        stationName={stations.find((s) => s.id === focus?.station_id)?.name}
        offline={!!connectionError}
      >
        {focus?.grocery && <OdaFoodOrder request={focus} busy={busy} perform={perform} />}
        <AgentChat
          requests={chatRequests}
          stations={stations}
          where={where}
          askCount={askCount}
          busy={busy}
          offline={!!connectionError}
          error={error}
          onWhere={setWhere}
          onSend={(text, stationId) => perform(async () => {
            if (!pendingChat.current || pendingChat.current.text !== text || pendingChat.current.station !== stationId)
              pendingChat.current = { text, station: stationId, id: crypto.randomUUID() };
            await api("/chat", { conversation_id: conversationId, event_id: pendingChat.current.id, text, board_id: stationId } satisfies ChatMessage);
            pendingChat.current = null;
          })}
          onApprove={approve}
          onReject={reject}
          onRequote={requote}
          onOpenTicket={openTicket}
        />
      </AgentStage>
      <main className={`work ${view === "twin" ? "work-twin" : ""}`}>
        <OdaConnection />
        {connectionError && (
          <div role="alert" className="alert error">
            Backend unavailable: {connectionError}. Start the API with{" "}
            <code>npm run dev:server</code>.
          </div>
        )}
        <nav className="views" aria-label="Views">
          <button aria-pressed={view === "work"} onClick={() => setView("work")}>Work</button>
          <button aria-pressed={view === "twin"} onClick={() => setView("twin")}>
            Office twin
          </button>
          <button aria-pressed={view === "tickets"} onClick={() => setView("tickets")}>
            Tickets
          </button>
          <button aria-pressed={view === "history"} onClick={() => setView("history")}>
            History
          </button>
        </nav>
        <ProcurementAlerts />
        {/* Stays mounted while hidden, so the camera and selection survive a look at a ticket. */}
        <OfficeTwin
          hidden={view !== "twin"}
          stations={stations}
          requests={requests}
          offline={!!connectionError}
          disabled={busy}
          onSimulatePress={(station) =>
            void perform(async () => {
              await sendEvent(station.id, "button_pressed", {});
            })
          }
          onSimulateReport={(stationId, text) =>
            void perform(async () => {
              await sendEvent(stationId, "speech_transcript", { text });
            })
          }
          onAsk={(stationId) => {
            setWhere(stationId);
            setAskCount((count) => count + 1);
          }}
          onRequest={openTicket}
        />
        {view === "work" && <WorkQueue onOpenRequest={openTicket} />}
        {view === "tickets" && (
          <TicketsView
            requests={requests}
            stations={stations}
            opened={opened}
            openCount={openCount}
            busy={busy}
            onApprove={approve}
            onReject={reject}
            onRequote={requote}
            onResolve={resolve}
          />
        )}
        {view === "history" && (
          <HistoryView
            requests={requests}
            stations={stations}
            statusLabels={statusLabels}
            disabled={busy}
            onOpen={openTicket}
          />
        )}
        {(!!connectionError || !streamConnected || mockAgent) && (
          <footer>
            <span>
              {connectionError
                ? "Disconnected"
                : streamConnected
                  ? ""
                  : "Reconnecting, checking every 5 seconds"}
            </span>
            {mockAgent && <span>Mock agent: scripted answers, no AI</span>}
          </footer>
        )}
      </main>
    </div>
  );
}
