import { useEffect, useRef, useState } from "react";
import type { OfficeRequest, Station } from "@office/contracts";
import { isOpen, isRecentChat, isTicket, money, requestTitle, stamp } from "./format";
import { ProposalActions, type ProposalHandlers } from "./ProposalActions";
import "./agent-chat.css";

const VISIBLE_REQUESTS = 8;
type Recognition = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  onresult: ((event: { results: { [key: number]: { [key: number]: { transcript: string } } } }) => void) | null;
  onerror: ((event: { error: string }) => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
};
type RecognitionConstructor = new () => Recognition;
type Props = {
  requests: OfficeRequest[];
  stations: Station[];
  where: Station["id"];
  askCount: number;
  busy: boolean;
  offline: boolean;
  error: string;
  onWhere: (station: Station["id"]) => void;
  onSend: (text: string, station: Station["id"]) => Promise<boolean>;
  onReply: (request: OfficeRequest, text: string) => Promise<boolean>;
  onOpenTicket: (id: string) => void;
} & ProposalHandlers;
// Only a guess to save a click. The person can still change the place before sending.
function guessStation(text: string): Station["id"] | null {
  const t = text.toLowerCase();
  if (/toilet|soap|bathroom|dopapir|såpe/.test(t)) return "bathroom";
  if (/print|toner|a4|skriver/.test(t)) return "printer";
  if (/coffee|kaffe|cup|kopp|kitchen|kjøkken|fridge|dishwasher/.test(t)) return "kitchen";
  return null;
}
const SpeechRecognition =
  (window as unknown as { SpeechRecognition?: RecognitionConstructor }).SpeechRecognition ||
  (window as unknown as { webkitSpeechRecognition?: RecognitionConstructor }).webkitSpeechRecognition;

export function AgentChat({ requests, stations, where, askCount, busy, offline, error, onWhere, onSend, onReply, onApprove, onReject, onRequote, onOpenTicket }: Props) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState("");
  const [listening, setListening] = useState(false);
  const [speechError, setSpeechError] = useState("");
  const [newTopic, setNewTopic] = useState<string | null>(null);
  const [opened, setOpened] = useState(false);
  const chosen = useRef(false);
  const thread = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const recognition = useRef<Recognition | null>(null);
  const ordered = [...requests].sort((a, b) => a.created_at.localeCompare(b.created_at));
  const visible = ordered.filter((r, index) => isOpen(r) || index >= ordered.length - VISIBLE_REQUESTS).slice(-VISIBLE_REQUESTS);
  const latest = ordered.at(-1);
  // Continue informational conversations while actionable proposals remain immutable.
  const waiting =
    latest &&
    latest.id !== newTopic &&
    (["awaiting_input", "needs_clarification"].includes(latest.status) ||
      (latest.status === "resolved" && !latest.proposal && !latest.grocery?.proposal && !latest.supplier_reference && !latest.assigned_to)) &&
    isRecentChat(latest)
      ? latest
      : undefined;
  const needsReply = !!waiting || requests.some((r) => r.status === "awaiting_approval");
  const stationName = (id: string) => stations.find((s) => s.id === id)?.name ?? id;
  const sendingShown = sending && !requests.some((r) => r.status === "processing");
  const threadKey = `${visible.map((r) => `${r.id}:${r.status}`).join()}|${sendingShown}`;
  useEffect(() => {
    if (thread.current) thread.current.scrollTop = thread.current.scrollHeight;
  }, [threadKey]);
  // The panel above can grow and shrink the thread, so keep the newest message in view.
  useEffect(() => {
    const element = thread.current;
    if (!element) return;
    const observer = new ResizeObserver(() => {
      element.scrollTop = element.scrollHeight;
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (askCount > 0) {
      chosen.current = true;
      setOpened(true);
      setTimeout(() => input.current?.focus());
    }
  }, [askCount]);
  useEffect(() => () => recognition.current?.stop(), []);
  function change(next: string) {
    setText(next);
    const guess = guessStation(next);
    if (guess && !chosen.current) onWhere(guess);
  }
  async function submit() {
    const message = text.trim();
    if (!message || busy) return;
    setText("");
    setSending(message);
    chosen.current = false;
    // The backend retains conversation history; send only the new user message.
    const sent = waiting
      ? await onReply(waiting, message)
      : await onSend(message, where);
    setSending("");
    // Nothing reached the server, so give the words back instead of making them retype it.
    if (!sent) setText((current) => current || message);
  }
  function startSpeech() {
    if (!SpeechRecognition) return;
    setSpeechError("");
    const instance = new SpeechRecognition();
    recognition.current = instance;
    instance.lang = "nb-NO";
    instance.continuous = false;
    instance.interimResults = false;
    instance.onresult = (event) => change(event.results[0][0].transcript);
    instance.onerror = (event) => {
      setSpeechError(`Microphone: ${event.error}. You can type instead.`);
      setListening(false);
    };
    instance.onend = () => setListening(false);
    try {
      instance.start();
      setListening(true);
    } catch {
      setSpeechError("Could not start the microphone. You can type instead.");
    }
  }
  return (
    <section className={`chat ${opened ? "chat-open" : ""}`} aria-label="Chat with the agent">
      <button type="button" className="secondary chat-toggle" aria-expanded={opened} onClick={() => setOpened(!opened)}>
        {opened ? "Close chat" : needsReply ? "Open chat · waiting for you" : "Open chat"}
      </button>
      <div className="chat-thread" ref={thread} aria-live="polite">
        {visible.length === 0 && !sendingShown && <p className="chat-empty">Ask about orders, something broken, rooms or air quality.</p>}
        {visible.map((request) => (
          <ChatTurn key={request.id} request={request} stationName={stationName(request.station_id)} busy={busy} onApprove={onApprove} onReject={onReject} onRequote={onRequote} onOpenTicket={onOpenTicket} />
        ))}
        {sendingShown && (
          <>
            <p className="chat-bubble chat-you">{sending}</p>
            <Typing />
          </>
        )}
      </div>
      {(error || speechError) && <div className="alert error chat-error" role="alert">{error || speechError}</div>}
      <form
        className="chat-form"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        {waiting ? (
          <p className="chat-replying">
            Answering about {stationName(waiting.station_id)}
            <button type="button" onClick={() => setNewTopic(waiting.id)}>New topic</button>
          </p>
        ) : (
          <div className="chat-where" role="group" aria-label="Where is this about?">
            {stations.map((station) => (
              <button
                key={station.id}
                type="button"
                aria-pressed={where === station.id}
                onClick={() => {
                  chosen.current = true;
                  onWhere(station.id);
                }}
              >
                {station.name}
              </button>
            ))}
          </div>
        )}
        <label className="sr-only" htmlFor="chat-message">Message to the agent</label>
        <textarea
          id="chat-message"
          ref={input}
          rows={2}
          value={text}
          maxLength={2000}
          placeholder="Ask the office assistant"
          onChange={(event) => change(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              void submit();
            }
          }}
        />
        <div className="chat-actions">
          {SpeechRecognition && (
            <button
              type="button"
              className="secondary"
              disabled={busy}
              title="Your browser’s speech service may process the audio."
              onClick={() => (listening ? recognition.current?.stop() : startSpeech())}
            >
              {listening ? "Stop" : "Speak"}
            </button>
          )}
          <button type="submit" className="primary" disabled={busy || offline || listening || !text.trim()}>
            {busy ? "Working…" : "Send"}
          </button>
        </div>
      </form>
    </section>
  );
}

function Typing() {
  return <p className="chat-bubble chat-agent chat-typing" aria-label="The agent is working"><i /><i /><i /></p>;
}

function ChatTurn({ request, stationName, busy, onApprove, onReject, onRequote, onOpenTicket }: {
  request: OfficeRequest;
  stationName: string;
  busy: boolean;
  onOpenTicket: (id: string) => void;
} & ProposalHandlers) {
  const sensor = request.original_text.startsWith("[sensor]");
  const proposal = request.proposal;
  const ticketLink = isTicket(request) && request.status !== "processing";
  return (
    <div className="chat-turn">
      <p className="chat-meta">{stationName} · {stamp(request.created_at)}</p>
      {sensor ? (
        <p className="chat-sensor">Sensor: {request.original_text.replace("[sensor] ", "")}</p>
      ) : (
        request.original_text && <p className="chat-bubble chat-you">{request.original_text}</p>
      )}
      {request.status === "processing" ? (
        <Typing />
      ) : request.status === "awaiting_input" ? (
        <p className="chat-bubble chat-agent">Button pressed. What do you need?</p>
      ) : request.status === "awaiting_approval" && proposal ? (
        <div className="chat-card">
          <strong>{proposal.product.name}</strong>
          <span>{proposal.quantity} × {proposal.product.supplier}</span>
          <span>{proposal.product.delivery_estimate}</span>
          <b>{money(proposal.total_nok)}</b>
          <ProposalActions request={request} busy={busy} onApprove={onApprove} onReject={onReject} onRequote={onRequote} />
          <small>Simulated. No real order or payment.</small>
        </div>
      ) : (
        <p className="chat-bubble chat-agent">
          {request.question ?? requestTitle(request)}
          {request.status === "maintenance_open" && <small>Local record only. Nobody was contacted.</small>}
          {request.status === "failed" && proposal && <strong className="chat-warning">Order outcome uncertain. Check the supplier before attempting another purchase. Retrying is disabled.</strong>}
        </p>
      )}
      {ticketLink && (
        <button className="chat-link" onClick={() => onOpenTicket(request.id)}>View ticket</button>
      )}
    </div>
  );
}
