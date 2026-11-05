import { useEffect, useRef, useState, type CSSProperties } from "react";
import type { OfficeRequest } from "@office/contracts";

export type Mood = "idle" | "working" | "waiting" | "done" | "failed";
type Tone = "plain" | "polite" | "cool" | "rude" | "shouting";
type Prop = "mug" | "roll" | "soap" | "paper" | "wrench" | null;

const SAY_MS = 4200;
const pokeLines = [
  "Boop.",
  "Hey, that tickles!",
  "I’m on the clock, you know.",
  "Do it again. No, wait.",
];
const moodLines: Partial<Record<Mood, string>> = {
  waiting: "Your turn!",
  done: "Woohoo!",
  failed: "Uh-oh.",
};
const homes: Record<Mood, number> = {
  idle: 22,
  working: 50,
  waiting: 76,
  done: 50,
  failed: 30,
};

const propFor = (request?: OfficeRequest): Prop => {
  if (request?.intent === "maintenance") return "wrench";
  switch (request?.item_id) {
    case "coffee":
    case "cups":
      return "mug";
    case "toilet-paper":
      return "roll";
    case "soap":
      return "soap";
    case "printer-paper":
    case "toner":
      return "paper";
    default:
      return null;
  }
};

// Cosmetic only: this reads the words for flavour and never affects a request.
function personality(request?: OfficeRequest): { tone: Tone; line?: string } {
  const text = request?.original_text ?? "";
  const lower = text.toLowerCase();
  const letters = text.replace(/[^\p{L}]/gu, "");
  if (/\b(shit|fuck|damn|faen|helvete|dritt)\b/.test(lower))
    return { tone: "rude", line: "Ouch. Still helping, though." };
  if (
    /!{2,}/.test(text) ||
    (letters.length >= 4 && letters === letters.toUpperCase())
  )
    return { tone: "shouting", line: "Eek! I hear you!" };
  if (/\b(bro|dude|mate|kompis)\b/.test(lower))
    return { tone: "cool", line: "Got you, bro." };
  if (/\b(please|thanks|thank you|takk)\b|vær så snill/.test(lower))
    return { tone: "polite", line: "You said the nice word!" };
  const topic: Record<string, string> = {
    mug: "Coffee? Top priority.",
    roll: "I’m on a roll.",
    soap: "Let’s keep it squeaky.",
    paper: "Feeding the printer.",
    wrench: "Oh no, poor thing.",
  };
  const prop = propFor(request);
  if (prop) return { tone: "plain", line: topic[prop] };
  return {
    tone: "plain",
    line: request?.intent === "clarification" ? "Hmm. Say that again?" : undefined,
  };
}

export function Avatar({
  mood,
  request,
}: {
  mood: Mood;
  request: OfficeRequest | undefined;
}) {
  const [x, setX] = useState(homes[mood]);
  const [walkMs, setWalkMs] = useState(0);
  const [walking, setWalking] = useState(false);
  const [left, setLeft] = useState(false);
  const [say, setSay] = useState<string | null>(null);
  const [hop, setHop] = useState(0);
  const position = useRef(homes[mood]);
  const sayTimer = useRef<ReturnType<typeof setTimeout>>(undefined);
  const saying = useRef(false);
  const { tone, line } = personality(request);
  const prop = propFor(request);

  function speak(text: string) {
    clearTimeout(sayTimer.current);
    saying.current = true;
    setSay(text);
    sayTimer.current = setTimeout(() => {
      saying.current = false;
      setSay(null);
    }, SAY_MS);
  }
  useEffect(() => () => clearTimeout(sayTimer.current), []);

  // Move between spots. The walk takes longer the further there is to go.
  useEffect(() => {
    const timers: ReturnType<typeof setTimeout>[] = [];
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const narrow = window.matchMedia("(max-width: 960px)").matches;
    const walkTo = (target: number, thenFaceRight = false) => {
      const distance = Math.abs(target - position.current);
      const duration = still ? 0 : distance * 26;
      setLeft(target < position.current);
      setWalkMs(duration);
      setWalking(duration > 0);
      setX(target);
      position.current = target;
      timers.push(
        setTimeout(() => {
          setWalking(false);
          if (thenFaceRight) setLeft(false);
        }, duration),
      );
      return duration;
    };
    const home = narrow && mood === "waiting" ? 50 : homes[mood];
    const first = walkTo(home, mood === "waiting");
    if (mood === "working" && !still) {
      const pace = () => {
        const duration = document.hidden
          ? 0
          : walkTo(14 + Math.random() * 70);
        timers.push(setTimeout(pace, duration + 500));
      };
      timers.push(setTimeout(pace, first + 400));
    }
    return () => timers.forEach(clearTimeout);
  }, [mood]);

  // Say something about new words, and otherwise about a change of mood.
  const spoken = `${request?.id}:${request?.original_text}`;
  useEffect(() => {
    if (line) speak(line);
    else if (request?.intent === null) speak("I’m all ears.");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [spoken]);
  useEffect(() => {
    const moodLine = moodLines[mood];
    const finished = mood === "done" || mood === "failed";
    if (moodLine && (finished || !saying.current)) speak(moodLine);
  }, [mood]);

  const asleep = mood === "idle";
  const happy = mood === "done";
  const worried = mood === "failed";
  const wide = tone === "shouting" && !happy;
  const shades = tone === "cool" && !asleep && !worried;
  const blush = tone === "polite" || happy;
  return (
    <div className="yard">
      <div
        className={`avatar-spot ${walking ? "walking" : ""}`}
        style={{ left: `${x}%`, transitionDuration: `${walkMs}ms` }}
      >
        {say && (
          <p
            key={say}
            className={`bubble ${x > 56 ? "bubble-left" : ""}`}
            aria-hidden="true"
          >
            {say}
          </p>
        )}
        {asleep && (
          <span className="snooze" aria-hidden="true">
            <i>z</i>
            <i>z</i>
            <i>z</i>
          </span>
        )}
        {happy && (
          <span className="confetti" aria-hidden="true">
            {Array.from({ length: 14 }, (_, index) => (
              <i key={index} style={{ "--n": index } as CSSProperties} />
            ))}
          </span>
        )}
        <button
          className={`avatar face-${asleep ? "asleep" : mood}`}
          aria-label="Poke the assistant"
          onClick={() => {
            setHop((value) => value + 1);
            speak(
              asleep
                ? "Five more minutes…"
                : pokeLines[hop % pokeLines.length],
            );
          }}
        >
          <span key={hop} className={`avatar-body ${hop ? "poked" : ""}`}>
          <svg
            viewBox="-8 -6 148 142"
            className={left ? "facing-left" : ""}
            aria-hidden="true"
          >
            <g className="leg leg-a">
              <rect x="40" y="104" width="15" height="24" rx="7.5" />
            </g>
            <g className="leg leg-b">
              <rect x="65" y="104" width="15" height="24" rx="7.5" />
            </g>
            <g className="torso">
              <path className="antenna" d="M60 16 C60 10 62 6 66 3" />
              <circle className="antenna-tip" cx="67" cy="3" r="5" />
              <path d="M22 62 C22 30 41 14 60 14 C79 14 98 30 98 62 L98 88 C98 104 86 112 72 112 L48 112 C34 112 22 104 22 88 Z" />
              <path className="arm arm-back" d="M25 72 Q13 78 9 90" />
              <g className="arm-front">
                <path className="arm" d="M95 72 Q106 68 113 56" />
                {prop && <AvatarProp prop={prop} />}
              </g>
              {asleep ? (
                <>
                  <path className="lid" d="M36 54 Q46 63 56 54" />
                  <path className="lid" d="M64 54 Q74 63 84 54" />
                </>
              ) : happy ? (
                <>
                  <path className="lid" d="M36 58 Q46 48 56 58" />
                  <path className="lid" d="M64 58 Q74 48 84 58" />
                </>
              ) : (
                <g className="blink">
                  <ellipse className="white" cx="46" cy="56" rx="12" ry={wide || worried ? 17 : 15} />
                  <ellipse className="white" cx="74" cy="56" rx="12" ry={wide || worried ? 17 : 15} />
                  <g className="pupils">
                    <circle cx="48" cy="56" r={wide ? 4.5 : 6.5} />
                    <circle cx="76" cy="56" r={wide ? 4.5 : 6.5} />
                    <circle className="white" cx="50" cy="53" r="2" />
                    <circle className="white" cx="78" cy="53" r="2" />
                  </g>
                </g>
              )}
              {shades && (
                <g className="shades">
                  <rect x="31" y="46" width="28" height="19" rx="7" />
                  <rect x="61" y="46" width="28" height="19" rx="7" />
                  <path d="M59 53 H61" />
                  <path className="glint" d="M37 51 L44 51" />
                  <path className="glint" d="M67 51 L74 51" />
                </g>
              )}
              {tone === "rude" && !asleep && !happy && (
                <g>
                  <path className="brow" d="M34 39 L56 46" />
                  <path className="brow" d="M86 39 L64 46" />
                </g>
              )}
              {blush && (
                <g className="cheeks">
                  <circle cx="33" cy="74" r="5.5" />
                  <circle cx="87" cy="74" r="5.5" />
                </g>
              )}
              {wide || worried ? (
                <ellipse className="mouth-o" cx="60" cy="88" rx="5" ry="6" />
              ) : asleep ? (
                <path className="mouth" d="M56 84 H64" />
              ) : (
                <path
                  className="mouth"
                  d={happy ? "M50 80 Q60 94 70 80" : "M53 82 Q60 89 67 82"}
                />
              )}
              {worried && (
                <path
                  className="drop"
                  d="M103 30 C108 38 110 41 110 44 A7 7 0 0 1 96 44 C96 41 98 38 103 30 Z"
                />
              )}
            </g>
          </svg>
          </span>
        </button>
      </div>
    </div>
  );
}

function AvatarProp({ prop }: { prop: NonNullable<Prop> }) {
  switch (prop) {
    case "mug":
      return (
        <g className="prop">
          <path className="steam" d="M112 26 Q109 21 112 16" />
          <path className="steam" d="M120 26 Q117 21 120 16" />
          <rect x="106" y="30" width="20" height="23" rx="3" />
          <path d="M126 35 Q134 41 126 47" fill="none" />
        </g>
      );
    case "roll":
      return (
        <g className="prop">
          <circle cx="116" cy="40" r="15" />
          <circle cx="116" cy="40" r="4.5" className="hole" />
        </g>
      );
    case "soap":
      return (
        <g className="prop">
          <path d="M112 22 H122 M116 22 V30" fill="none" />
          <rect x="106" y="30" width="20" height="24" rx="4" />
        </g>
      );
    case "paper":
      return (
        <g className="prop">
          <rect x="104" y="20" width="24" height="33" rx="2" />
          <path d="M109 29 H123 M109 36 H123 M109 43 H118" fill="none" />
        </g>
      );
    default:
      return (
        <path
          className="prop-tool"
          d="M113 54 L124 30 M117 20 A9 9 0 1 0 131 34"
        />
      );
  }
}
