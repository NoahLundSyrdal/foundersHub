import { useCallback, useEffect, useState } from "react";
import { api, type Board, type Person, type State, type Task } from "./api";

type Activity = { at: string; kind: string; text: string };
const empty: State = { tasks: [], orders: [], tickets: [], commands: {} };

export function App() {
  const [boards, setBoards] = useState<Board[]>([]);
  const [people, setPeople] = useState<Person[]>([]);
  const [state, setState] = useState<State>(empty);
  const [activity, setActivity] = useState<Activity[]>([]);
  const [error, setError] = useState("");
  const [model, setModel] = useState("");

  const refresh = useCallback(() => api<State>("/state").then(setState).catch((e) => setError(e.message)), []);

  useEffect(() => {
    api<{ boards: Board[]; people: Person[] }>("/boards").then((r) => (setBoards(r.boards), setPeople(r.people)));
    api<{ model: string }>("/health").then((r) => setModel(r.model));
    refresh();
    const log = (kind: string, text: string) =>
      setActivity((a) => [{ at: new Date().toLocaleTimeString(), kind, text }, ...a].slice(0, 40));
    const es = new EventSource("/api/events");
    es.addEventListener("task.updated", refresh);
    es.addEventListener("state.changed", refresh);
    es.addEventListener("tool.called", (e) => {
      const d = JSON.parse(e.data);
      log("tool", `${d.tool}(${JSON.stringify(d.args)})`);
    });
    es.addEventListener("tool.result", (e) => log("result", `${JSON.parse(e.data).tool} → ${JSON.parse(e.data).content}`));
    es.addEventListener("board.say", (e) => log("say", `${JSON.parse(e.data).board_id}: ${JSON.parse(e.data).text}`));
    es.addEventListener("board.listen", (e) => log("listen", `${JSON.parse(e.data).board_id}: button pressed, listening`));
    es.onopen = refresh; // reconnect: refetch authoritative state
    const poll = setInterval(refresh, 5000);
    return () => (es.close(), clearInterval(poll));
  }, [refresh]);

  const act = (p: Promise<unknown>) => p.then(refresh).catch((e) => setError(e.message));
  const waiting = state.tasks.filter((t) => t.status === "awaiting_approval");

  return (
    <main>
      <header>
        <h1>Office agent</h1>
        <span className="pill">model: {model || "…"}</span>
        <span className="pill warn">orders are SIMULATED</span>
        {error && <span className="pill bad" onClick={() => setError("")}>{error} ✕</span>}
      </header>

      <section className="boards">
        {boards.map((b) => (
          <BoardCard key={b.id} board={b} people={people} command={state.commands[b.id]} onSent={refresh} onError={setError} />
        ))}
      </section>

      <div className="cols">
        <section>
          <h2>Needs approval</h2>
          {waiting.length === 0 && <p className="muted">Nothing waiting.</p>}
          {waiting.map((t) => (
            <div className="card amber" key={t.id}>
              <b>{t.prompt}</b>
              <div className="muted">{t.board_id} · asked by {t.person}</div>
              {t.pending.map((p) => (
                <div key={p.tool_call_id}>{p.details.summary ?? `${p.tool} ${JSON.stringify(p.args)}`}</div>
              ))}
              <button onClick={() => act(api(`/tasks/${t.id}/decide`, { approve: true }))}>Approve</button>
              <button className="ghost" onClick={() => act(api(`/tasks/${t.id}/decide`, { approve: false }))}>Deny</button>
            </div>
          ))}

          <h2>Tasks</h2>
          {state.tasks.map((t) => <TaskRow key={t.id} t={t} />)}
        </section>

        <section>
          <h2>Live activity</h2>
          <ol className="feed">
            {activity.map((a, i) => (
              <li key={i} className={a.kind}><span className="muted">{a.at}</span> {a.text}</li>
            ))}
          </ol>

          <h2>Orders <span className="muted">(simulated)</span></h2>
          {state.orders.map((o) => (
            <div className="row" key={o.id}>
              {o.quantity} × {o.product_id} · {o.total_nok} NOK · {o.ref} · <b>{o.status}</b>
              {o.status === "simulated" && <button className="ghost" onClick={() => act(api(`/orders/${o.id}/delivered`, {}))}>Delivered</button>}
            </div>
          ))}

          <h2>Tickets <span className="muted">(local only)</span></h2>
          {state.tickets.map((t) => (
            <div className="row" key={t.id}>
              {t.board_id}: {t.summary} → {t.team} · <b>{t.status}</b>
              {t.status === "open" && <button className="ghost" onClick={() => act(api(`/tickets/${t.id}/resolve`, {}))}>Resolve</button>}
            </div>
          ))}
        </section>
      </div>
    </main>
  );
}

function BoardCard(props: { board: Board; people: Person[]; command?: { text: string; color: string }; onSent: () => void; onError: (e: string) => void }) {
  const { board, people, command } = props;
  const [text, setText] = useState("");
  const [person, setPerson] = useState("");
  const [busy, setBusy] = useState(false);
  const send = async () => {
    if (!text.trim()) return;
    setBusy(true);
    try {
      await api("/input", { board_id: board.id, text, person: person || null });
      setText("");
      props.onSent();
    } catch (e) {
      props.onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="card board">
      <h3>{board.location}</h3>
      <div className="modules">{board.modules.join(" · ")}</div>
      <div className={`screen ${command?.color ?? ""}`}>{command?.text ?? "—"}</div>
      <select value={person} onChange={(e) => setPerson(e.target.value)} title="Stand-in for voice ID">
        <option value="">unknown speaker (guest)</option>
        {people.map((p) => <option key={p.name} value={p.name}>{p.name} · {p.role}</option>)}
      </select>
      <form onSubmit={(e) => (e.preventDefault(), send())}>
        <input value={text} onChange={(e) => setText(e.target.value)} placeholder="Say something…" disabled={busy} />
        <button disabled={busy}>{busy ? "…" : "Send"}</button>
      </form>
    </div>
  );
}

function TaskRow({ t }: { t: Task }) {
  return (
    <div className={`row status-${t.status}`}>
      <span className="muted">{t.board_id} · {t.person}</span> “{t.prompt}” → <b>{t.status}</b>
      {t.reply && <div className="reply">{t.reply}</div>}
    </div>
  );
}
