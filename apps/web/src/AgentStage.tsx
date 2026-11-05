import { useEffect, useRef, useState } from "react";
import type { OfficeRequest } from "@office/contracts";
import { money, stamp } from "./format";

type Mood = "idle" | "working" | "waiting" | "done" | "failed";

const PACE_MS = 500;
const FRESH_MS = 5000;
const FEED_LINES = 3;
const moodLabels: Record<Mood, string> = {
  idle: "Resting",
  working: "Working",
  waiting: "Waiting for you",
  done: "Done",
  failed: "Needs attention",
};
const workingCaptions = [
  "Got it.",
  "Working out what you need.",
  "Checking the rules.",
];

// Where the request really is. Only the server state decides this.
function position(request: OfficeRequest): { step: number; mood: Mood } {
  switch (request.status) {
    case "awaiting_input":
      return { step: 0, mood: "waiting" };
    case "processing":
      return { step: 1, mood: "working" };
    case "needs_clarification":
      return { step: 1, mood: "waiting" };
    case "awaiting_approval":
    case "maintenance_open":
      return { step: 3, mood: "waiting" };
    case "failed":
      return { step: request.proposal ? 4 : 1, mood: "failed" };
    default:
      return { step: 4, mood: "done" };
  }
}

function caption(request: OfficeRequest): { title: string; note?: string } {
  switch (request.status) {
    case "awaiting_input":
      return { title: "I’m listening. What do you need?" };
    case "processing":
      return { title: workingCaptions[1] };
    case "needs_clarification":
      return { title: request.question ?? "I need a little more detail." };
    case "awaiting_approval":
      return request.proposal
        ? {
            title: `${request.proposal.product.name}, ${money(request.proposal.total_nok)}. OK to order?`,
            note: "Simulated order. No money is spent.",
          }
        : { title: "OK to order?" };
    case "maintenance_open":
      return {
        title: `Passed to ${request.assigned_to ?? "the responsible team"}.`,
        note: "Local record only. Nobody was contacted.",
      };
    case "ordered":
      return {
        title: "Order placed, in simulation.",
        note: "No real order was made.",
      };
    case "failed":
      return request.proposal
        ? {
            title: "I can’t tell if that order went through.",
            note: "Check the supplier before trying again.",
          }
        : { title: request.summary };
    default:
      return { title: "All sorted." };
  }
}

// The mock agent finishes in milliseconds, so finished steps are revealed one
// at a time to stay readable. This only delays the display, never the state.
function usePaced(target: number, key: string | undefined, fresh: boolean) {
  const [value, setValue] = useState(target);
  const lastKey = useRef(key);
  useEffect(() => {
    let current = value;
    if (lastKey.current !== key) {
      lastKey.current = key;
      current = fresh ? 0 : target;
      setValue(current);
    }
    if (current > target) setValue(target);
    if (current < target) {
      const timer = setTimeout(() => setValue(current + 1), PACE_MS);
      return () => clearTimeout(timer);
    }
  }, [target, key, fresh, value]);
  return Math.min(value, target);
}

export function AgentStage({
  request,
  stationName,
  provider,
  offline,
}: {
  request: OfficeRequest | undefined;
  stationName: string | undefined;
  provider: string | undefined;
  offline: boolean;
}) {
  const real = request ? position(request) : { step: -1, mood: "idle" as Mood };
  const trace = request?.trace ?? [];
  const fresh =
    !!request && Date.now() - new Date(request.created_at).getTime() < FRESH_MS;
  const step = usePaced(real.step, request?.id, fresh);
  const lines = usePaced(trace.length, request?.id, fresh);
  const catchingUp = step < real.step || lines < trace.length;
  const mood: Mood = offline ? "failed" : catchingUp ? "working" : real.mood;
  const [base, setBase] = useState<Mood>(mood);
  const shown = trace.slice(0, lines);
  const maintenance = request?.intent === "maintenance";
  const steps = [
    "Listening",
    "Understanding",
    "Checking rules",
    maintenance ? "Team assigned" : "Your approval",
    maintenance ? "Resolved" : "Simulated order",
  ];
  const text: { title: string; note?: string } = offline
    ? {
        title: "I can’t reach the server.",
        note: "I’ll keep trying.",
      }
    : !request
      ? {
          title: "All quiet.",
          note: "Press a station when something runs out or breaks.",
        }
      : catchingUp && step < workingCaptions.length
        ? { title: workingCaptions[step] }
        : caption(request);
  return (
    <section className={`stage field-${base}`} aria-label="Agent status">
      <div
        key={mood}
        className={`stage-flood field-${mood}`}
        onAnimationEnd={() => setBase(mood)}
      />
      <div className={`stage-inner mood-${mood}`}>
        <header className="stage-top">
          <a className="wordmark" href="/" aria-label="Office home">
            office
          </a>
          <span>
            Simulation{provider ? ` · ${provider} agent` : ""}
          </span>
        </header>
        <div className="eyes" aria-hidden="true">
          <span className="eye-look">
            <span className="eye" />
          </span>
          <span className="eye-look">
            <span className="eye" />
          </span>
        </div>
        <div className="stage-copy" aria-live="polite">
          <div className="stage-meta">
            <strong>
              {mood === "waiting" && request?.status === "maintenance_open"
                ? "With the team"
                : moodLabels[mood]}
            </strong>
            {stationName && !offline && <span>{stationName}</span>}
          </div>
          <p key={text.title} className="stage-title">
            {text.title}
          </p>
          {text.note && <p className="stage-note">{text.note}</p>}
        </div>
        {request && !offline && (
          <div className="stage-progress">
            <ol className="stage-steps" aria-label="Request progress">
              {steps.map((label, index) => {
                const reached =
                  index < step || (index === step && mood === "done");
                const state = reached
                  ? "reached"
                  : index === step
                    ? "current"
                    : "pending";
                return (
                  <li
                    key={label}
                    className={state}
                    aria-current={state === "current" ? "step" : undefined}
                  >
                    <i aria-hidden="true" />
                    {label}
                  </li>
                );
              })}
            </ol>
            {shown.length > 0 && (
              <ol className="stage-feed" aria-label="Latest agent steps">
                {shown.slice(-FEED_LINES).map((entry) => (
                  <li key={`${entry.at}-${entry.stage}-${entry.detail}`}>
                    <time dateTime={entry.at}>{stamp(entry.at)}</time>
                    <span>{entry.detail}</span>
                  </li>
                ))}
              </ol>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
