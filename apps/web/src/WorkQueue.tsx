import { useCallback, useEffect, useRef, useState } from "react";
import type { WorkItem, WorkStep, WorkTemplate, WorkNotification, WorkRoutine } from "@office/contracts";
import { api } from "./api";
import "./work-queue.css";

const terminal = new Set(["completed", "cancelled", "failed"]);
const filters = ["Open", "Waiting", "Blocked", "Overdue", "Completed", "All"] as const;
type Filter = typeof filters[number];
type Action = "assign" | "wait" | "block" | "resume" | "cancel" | "fail" | "start_step" | "complete_step" | "simulate_step" | "reconcile_step" | "close";

function stamp(value: string | null | undefined) {
  return value ? new Date(value).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "Not set";
}
function label(value: string) { return value.replaceAll("_", " "); }
function command(work: WorkItem) { return { event_key: crypto.randomUUID(), expected_version: work.version }; }
function today() {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}

export default function WorkQueue({ onOpenRequest }: { onOpenRequest?: (id: string) => void }) {
  const [items, setItems] = useState<WorkItem[]>([]);
  const [templates, setTemplates] = useState<WorkTemplate[]>([]);
  const [notifications, setNotifications] = useState<WorkNotification[]>([]);
  const [routines, setRoutines] = useState<WorkRoutine[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("Open");
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [now, setNow] = useState(Date.now());
  const mounted = useRef(false);
  const generation = useRef(0);
  const refresh = useCallback(async () => {
    const current = ++generation.current;
    const [work, available, inbox, scheduled] = await Promise.all([
      api<{ items: WorkItem[] }>("/work"),
      api<{ templates: WorkTemplate[] }>("/work/templates"),
      api<{ notifications: WorkNotification[] }>("/work/notifications"),
      api<{ routines: WorkRoutine[] }>("/work/routines"),
    ]);
    if (!mounted.current || generation.current !== current) return;
    setItems(work.items);
    setTemplates(available.templates);
    setNotifications(inbox.notifications);
    setRoutines(scheduled.routines);
    setSelected((id) => id && work.items.some((item) => item.id === id) ? id : work.items[0]?.id ?? null);
    setLoading(false);
    setLoadError("");
    setNow(Date.now());
  }, []);

  useEffect(() => {
    mounted.current = true;
    let alive = true;
    let poll: ReturnType<typeof setInterval> | undefined;
    const sync = () => { void refresh().catch((err: unknown) => {
      if (alive && mounted.current) { setLoadError(err instanceof Error ? err.message : "Could not load work"); setLoading(false); }
    }); };
    const startPoll = () => { if (poll === undefined) poll = setInterval(sync, 5000); };
    const stopPoll = () => { if (poll !== undefined) { clearInterval(poll); poll = undefined; } };
    const stream = new EventSource("/api/events");
    stream.addEventListener("work.updated", sync);
    stream.addEventListener("state.changed", sync);
    stream.onopen = () => { stopPoll(); sync(); };
    stream.onerror = startPoll;
    const clock = setInterval(() => setNow(Date.now()), 60000);
    startPoll();
    sync();
    return () => { alive = false; mounted.current = false; ++generation.current; stopPoll(); clearInterval(clock); stream.close(); };
  }, [refresh]);

  const mutate = async (path: string, body: unknown) => {
    setBusy(true); setError("");
    try {
      const result = await api<WorkItem>(path, body);
      if (!mounted.current) return false;
      if (result.id && path === "/work") { setSelected(result.id); setFilter("Open"); setShowCreate(false); }
      await refresh();
      return mounted.current;
    } catch (err) {
      if (!mounted.current) return false;
      setError(err instanceof Error ? err.message : "Could not update work");
      // A rejected version or an interrupted response may leave newer server state.
      await refresh().catch(() => undefined);
      return false;
    } finally { if (mounted.current) setBusy(false); }
  };
  const active = items.find((item) => item.id === selected);
  const visible = items.filter((item) => {
    if (filter === "Waiting") return item.status === "waiting" || item.status === "awaiting_approval";
    if (filter === "Blocked") return item.status === "blocked";
    if (filter === "Completed") return item.status === "completed";
    if (filter === "All") return true;
    if (filter === "Overdue") return !terminal.has(item.status) && [item.due_at, item.next_review_at].some((at) => at && Date.parse(at) <= now);
    return !terminal.has(item.status);
  });
  const inbox = notifications.filter((notification) => !notification.acknowledged_at);

  return <section className="work-queue" aria-label="Work and follow-up">
    <div className="wq-heading">
      <div><h2>Work and follow-up</h2><p>Track the outcome, the next step and the evidence.</p></div>
      <button className="secondary" onClick={() => setShowCreate((open) => !open)} disabled={busy} aria-expanded={showCreate}>{showCreate ? "Hide form" : "New work"}</button>
    </div>
    {error && <div className="alert error" role="alert">{error}</div>}
    {loadError && <div className="alert error" role="alert">{loadError}</div>}
    {showCreate && <CreateWork templates={templates} busy={busy} onCreate={(body) => void mutate("/work", body)} />}
    <Routines routines={routines} templates={templates} busy={busy} mutate={mutate} />
    {inbox.length > 0 && <details className="wq-inbox" open>
      <summary>Follow-up inbox ({inbox.length}) · local only</summary>
      {inbox.map((notice) => <div className="wq-notice" key={notice.id}>
        <div><strong>{label(notice.kind)} · {notice.target}</strong><p>{notice.detail}</p><small>{stamp(notice.created_at)} · No external message sent</small></div>
        <div className="wq-actions"><button className="secondary" onClick={() => setSelected(notice.work_id)}>Open work</button><button className="text-button" disabled={busy} onClick={() => void mutate(`/work/notifications/${encodeURIComponent(notice.id)}/acknowledge`, {})}>Acknowledge</button></div>
      </div>)}
    </details>}
    <div className="wq-filters" role="group" aria-label="Filter work">
      {filters.map((value) => <button key={value} aria-pressed={filter === value} onClick={() => setFilter(value)}>{value}</button>)}
    </div>
    <div className="wq-layout">
      <div className="wq-list" aria-label={`${filter} work`} aria-busy={loading}>
        {loading ? <p className="empty">Loading work…</p> : visible.length === 0 ? <p className="empty">No {filter.toLowerCase()} work.</p> : visible.map((item) => <button key={item.id} className={`wq-row ${selected === item.id ? "selected" : ""}`} aria-pressed={selected === item.id} onClick={() => setSelected(item.id)}>
          <span className={`wq-status wq-${item.status}`}>{label(item.status)}</span><strong>{item.title}</strong><span>{item.owner} · {item.priority}</span><small>Next review: {stamp(item.next_review_at)}</small>
        </button>)}
      </div>
      {active ? <WorkDetail key={active.id} work={active} busy={busy} mutate={mutate} onOpenRequest={onOpenRequest} /> : !loading && <p className="empty">Create work to start tracking an outcome.</p>}
    </div>
  </section>;
}

function CreateWork({ templates, busy, onCreate }: { templates: WorkTemplate[]; busy: boolean; onCreate: (body: unknown) => void }) {
  const [template, setTemplate] = useState("replenishment");
  const [title, setTitle] = useState("");
  const [outcome, setOutcome] = useState("");
  const [owner, setOwner] = useState("office-coordinator");
  const [location, setLocation] = useState("");
  const [priority, setPriority] = useState("routine");
  const [due, setDue] = useState("");
  return <form className="wq-form wq-create" onSubmit={(event) => {
    event.preventDefault();
    onCreate({ event_key: crypto.randomUUID(), title: title.trim(), outcome: outcome.trim(), owner: owner.trim(), template_key: template, context: location.trim() ? { location_id: location.trim() } : {}, requester_id: null, priority, due_at: due ? new Date(due).toISOString() : null, next_review_at: null, escalation_owner: "office-coordinator", simulation: true });
  }}>
    <h3>Create work</h3>
    <label>Workflow<select value={template} onChange={(e) => setTemplate(e.target.value)} disabled={busy}>{templates.map((item) => <option value={item.key} key={item.key}>{item.title}</option>)}</select></label>
    <p className="hint">{templates.find((item) => item.key === template)?.description}</p>
    <label>Title<input value={title} onChange={(e) => setTitle(e.target.value)} required maxLength={300} disabled={busy} /></label>
    <label>What must become true?<textarea value={outcome} onChange={(e) => setOutcome(e.target.value)} required maxLength={2000} disabled={busy} /></label>
    <div className="wq-fields"><label>Accountable owner<input value={owner} onChange={(e) => setOwner(e.target.value)} required maxLength={128} disabled={busy} /></label><label>Location (optional)<input value={location} onChange={(e) => setLocation(e.target.value)} maxLength={128} disabled={busy} /></label></div>
    <div className="wq-fields"><label>Priority<select value={priority} onChange={(e) => setPriority(e.target.value)} disabled={busy}><option value="routine">Routine</option><option value="urgent">Urgent</option><option value="critical">Critical</option></select></label><label>Deadline (optional)<input type="datetime-local" value={due} onChange={(e) => setDue(e.target.value)} disabled={busy} /></label></div>
    <p className="hint">Local workflow with simulated execution. Creating work does not order, book, contact anyone or change access.</p>
    <button className="primary" disabled={busy || !templates.length || !title.trim() || !outcome.trim() || !owner.trim()}>{busy ? "Saving…" : "Create work"}</button>
  </form>;
}

type Mutate = (path: string, body: unknown) => Promise<boolean>;
function Routines({ routines, templates, busy, mutate }: { routines: WorkRoutine[]; templates: WorkTemplate[]; busy: boolean; mutate: Mutate }) {
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [template, setTemplate] = useState("maintenance");
  const [outcome, setOutcome] = useState("");
  const [owner, setOwner] = useState("office-coordinator");
  const [timezone, setTimezone] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone || "Europe/Oslo");
  const [time, setTime] = useState("09:00");
  const [startsOn, setStartsOn] = useState(today);
  const [weekdays, setWeekdays] = useState([0, 1, 2, 3, 4]);
  const [missed, setMissed] = useState("latest");
  const [overlap, setOverlap] = useState("link");
  const days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  return <details className="wq-routines">
    <summary>Recurring routines ({routines.length})</summary>
    <p className="hint">Routines create local work and follow-up inbox entries. No external reminders are sent.</p>
    <div className="wq-routine-list">{routines.length === 0 ? <p className="empty">No routines yet.</p> : routines.map((routine) => <div className="wq-routine-row" key={routine.id}>
      <div><strong>{routine.name}</strong><p>{routine.weekdays.map((day) => days[day]).join(", ")} at {routine.local_time} · {routine.timezone}</p><small>{routine.enabled ? `Next occurrence: ${stamp(routine.next_run_at)}` : "Paused"}</small><small>Missed: {routine.missed_policy === "latest" ? "latest occurrence only" : "skip prior days"} · Overlap: {routine.overlap_policy === "link" ? "link unfinished work" : "allow separate work"}</small></div>
      <button className="secondary" disabled={busy} onClick={() => void mutate(`/work/routines/${encodeURIComponent(routine.id)}/enabled`, { enabled: !routine.enabled })}>{routine.enabled ? "Pause" : "Resume"}</button>
    </div>)}</div>
    <button className="secondary" disabled={busy} aria-expanded={creating} onClick={() => setCreating((value) => !value)}>{creating ? "Hide routine form" : "New routine"}</button>
    {creating && <form className="wq-form wq-create" onSubmit={(event) => {
      event.preventDefault();
      void mutate("/work/routines", { event_key: crypto.randomUUID(), name: name.trim(), timezone: timezone.trim(), local_time: time, weekdays, starts_on: startsOn, missed_policy: missed, overlap_policy: overlap, enabled: true, review_interval_hours: 24, max_reminders: 2, work: { event_key: "routine-occurrence", title: name.trim(), outcome: outcome.trim(), owner: owner.trim(), template_key: template, context: {}, requester_id: null, priority: "routine", due_at: null, next_review_at: null, escalation_owner: "office-coordinator", simulation: true } }).then((saved) => { if (saved) { setCreating(false); setName(""); setOutcome(""); } });
    }}>
      <h3>Create a recurring routine</h3>
      <label>Name and work title<input value={name} onChange={(event) => setName(event.target.value)} required maxLength={300} disabled={busy} /></label>
      <label>Workflow<select value={template} onChange={(event) => setTemplate(event.target.value)} disabled={busy}>{templates.map((item) => <option key={item.key} value={item.key}>{item.title}</option>)}</select></label>
      <label>Outcome for each occurrence<textarea value={outcome} onChange={(event) => setOutcome(event.target.value)} required maxLength={2000} disabled={busy} /></label>
      <label>Accountable owner<input value={owner} onChange={(event) => setOwner(event.target.value)} required maxLength={128} disabled={busy} /></label>
      <div className="wq-fields"><label>Local time<input type="time" value={time} onChange={(event) => setTime(event.target.value)} required disabled={busy} /></label><label>Timezone<input value={timezone} onChange={(event) => setTimezone(event.target.value)} required maxLength={128} disabled={busy} placeholder="Europe/Oslo" /></label></div>
      <label>Start date<input type="date" value={startsOn} onChange={(event) => setStartsOn(event.target.value)} required disabled={busy} /></label>
      <fieldset className="wq-weekdays"><legend>Repeat on</legend>{days.map((day, index) => <label key={day}><input type="checkbox" checked={weekdays.includes(index)} disabled={busy} onChange={(event) => setWeekdays((current) => event.target.checked ? [...current, index].sort() : current.filter((value) => value !== index))} />{day}</label>)}</fieldset>
      <div className="wq-fields"><label>If an occurrence was missed<select value={missed} onChange={(event) => setMissed(event.target.value)} disabled={busy}><option value="latest">Catch up latest only</option><option value="skip">Skip prior days</option></select></label><label>If previous work is unfinished<select value={overlap} onChange={(event) => setOverlap(event.target.value)} disabled={busy}><option value="link">Link unfinished work</option><option value="allow">Create separate work</option></select></label></div>
      <p className="hint">Simulated execution. Review every 24 hours, with up to two local reminders followed by escalation to the office coordinator.</p>
      <button className="primary" disabled={busy || !templates.length || !name.trim() || !outcome.trim() || !owner.trim() || !timezone.trim() || !time || !startsOn || weekdays.length === 0}>{busy ? "Saving…" : "Create routine"}</button>
    </form>}
  </details>;
}

function WorkDetail({ work, busy, mutate, onOpenRequest }: { work: WorkItem; busy: boolean; mutate: Mutate; onOpenRequest?: (id: string) => void }) {
  const [owner, setOwner] = useState(work.owner);
  const [reason, setReason] = useState("");
  const [review, setReview] = useState("");
  const finished = terminal.has(work.status);
  const act = (action: Action, extra: Record<string, unknown> = {}) => void mutate(`/work/${encodeURIComponent(work.id)}/actions`, { ...command(work), action, ...extra });
  useEffect(() => { setOwner(work.owner); }, [work.owner]);
  return <article className="wq-detail" aria-label="Work details">
    <div className="wq-detail-title"><span className={`wq-status wq-${work.status}`}>{label(work.status)}</span><span>{work.simulation ? "Simulated execution" : "Human coordination"}</span></div>
    <h3>{work.title}</h3><p className="wq-outcome">{work.outcome}</p>
    <dl className="wq-facts"><div><dt>Owner</dt><dd>{work.owner}</dd></div><div><dt>Priority</dt><dd>{work.priority}</dd></div><div><dt>Deadline</dt><dd>{stamp(work.due_at)}</dd></div><div><dt>Next review</dt><dd>{stamp(work.next_review_at)}</dd></div><div><dt>Escalation owner</dt><dd>{work.escalation_owner}</dd></div>{Object.entries(work.context).map(([key, value]) => <div key={key}><dt>{label(key)}</dt><dd>{value}</dd></div>)}</dl>
    {work.waiting_reason && <p className="wq-waiting">{work.waiting_reason}</p>}
    {work.links.task_id && <div className="wq-linked"><p>Linked request: <code>{work.links.task_id}</code>. Purchase approval and execution use the existing request controls.</p>{onOpenRequest ? <button className="secondary" onClick={() => onOpenRequest(work.links.task_id!)}>Open linked request</button> : <a href={`?request=${encodeURIComponent(work.links.task_id)}`}>Open linked request</a>}</div>}
    {!finished && <details className="wq-manage"><summary>Ownership and waiting</summary>
      <form className="wq-form" onSubmit={(e) => { e.preventDefault(); act("assign", { owner: owner.trim() }); }}><label>Accountable owner<input value={owner} onChange={(e) => setOwner(e.target.value)} maxLength={128} required disabled={busy} /></label><button className="secondary" disabled={busy || !owner.trim() || owner.trim() === work.owner}>Assign owner</button></form>
      <div className="wq-form"><label>Reason<textarea value={reason} onChange={(e) => setReason(e.target.value)} maxLength={2000} disabled={busy} /></label><label>Next review<input type="datetime-local" value={review} onChange={(e) => setReview(e.target.value)} disabled={busy} /></label><div className="wq-actions"><button className="secondary" disabled={busy || !reason.trim() || !review} onClick={() => act("wait", { reason: reason.trim(), next_review_at: new Date(review).toISOString() })}>Wait for response</button><button className="secondary" disabled={busy || !reason.trim() || !review} onClick={() => act("block", { reason: reason.trim(), next_review_at: new Date(review).toISOString() })}>Mark blocked</button>{["waiting", "blocked"].includes(work.status) && <button className="secondary" disabled={busy} onClick={() => act("resume")}>Resume</button>}</div></div>
    </details>}
    <h4>Steps</h4>
    <ol className="wq-steps">{work.steps.map((step) => <StepCard key={step.id} work={work} step={step} busy={busy || finished} mutate={mutate} />)}</ol>
    <details className="wq-history"><summary>History ({work.history.length})</summary><ol>{work.history.map((entry, index) => <li key={`${entry.at}-${index}`}><time dateTime={entry.at}>{stamp(entry.at)}</time><strong>{label(entry.action)}</strong><span>{entry.detail}</span></li>)}</ol></details>
    {!finished && <div className="wq-close"><p>Close only after every required step and its completion evidence are confirmed.</p><div className="wq-actions"><button className="primary" disabled={busy || !work.steps.every((step) => step.status === "completed")} onClick={() => act("close")}>Verify and close work</button><button className="text-button" disabled={busy || !reason.trim()} onClick={() => act("cancel", { reason: reason.trim() })}>Cancel with reason</button></div></div>}
  </article>;
}

function StepCard({ work, step, busy, mutate }: { work: WorkItem; step: WorkStep; busy: boolean; mutate: Mutate }) {
  const [kind, setKind] = useState(step.required_evidence[0] ?? "confirmation");
  const [summary, setSummary] = useState("");
  const [source, setSource] = useState("local confirmation");
  const [recordedBy, setRecordedBy] = useState("local-operator");
  const [reconcileResult, setReconcileResult] = useState("confirmed");
  const [reconcileReference, setReconcileReference] = useState(step.execution?.reference ?? "");
  const [reconcileReason, setReconcileReason] = useState("");
  const evidence = work.evidence.filter((item) => item.step_id === step.id);
  const dependenciesReady = step.depends_on.every((id) => work.steps.some((other) => other.id === id && other.status === "completed"));
  const hasEvidence = step.required_evidence.every((required) => evidence.some((item) => item.kind === required));
  const finished = step.status === "completed";
  const external = ["purchase", "message", "booking", "access"].includes(step.action_kind);
  const linked = !!work.links.task_id && external;
  const managed = !!work.links.task_id && work.template_key === "replenishment" && ["order", "inventory"].includes(step.id);
  const act = (action: Action, extra: Record<string, unknown> = {}) => void mutate(`/work/${encodeURIComponent(work.id)}/actions`, { ...command(work), action, step_id: step.id, ...extra });
  return <li className="wq-step">
    <div className="wq-step-title"><h5>{step.title}</h5><span className={`wq-status wq-${step.status}`}>{label(step.status)}</span></div>
    <p className="wq-step-meta">{step.owner} · {label(step.action_kind)}</p>
    {step.depends_on.length > 0 && <p className="hint">After: {step.depends_on.map((id) => work.steps.find((other) => other.id === id)?.title ?? id).join(", ")}{!dependenciesReady && " · waiting for dependencies"}</p>}
    {step.approval && !finished && <div className="wq-approval"><strong>Exact action review · {label(step.approval.status)}</strong><pre>{JSON.stringify(step.action_snapshot, null, 2)}</pre><p className="hint">Approval expires {stamp(step.approval.expires_at)}. {work.simulation ? "Execution is simulated." : "No external execution is enabled here."}</p>{!linked && step.approval.status === "pending" && <div className="wq-actions"><button className="secondary" disabled={busy || !dependenciesReady} onClick={() => void mutate(`/work/${encodeURIComponent(work.id)}/decisions`, { ...command(work), step_id: step.id, approve: true, proposal_fingerprint: step.approval!.fingerprint })}>Approve exact action</button><button className="text-button" disabled={busy} onClick={() => void mutate(`/work/${encodeURIComponent(work.id)}/decisions`, { ...command(work), step_id: step.id, approve: false, proposal_fingerprint: step.approval!.fingerprint })}>Reject</button></div>}</div>}
    {step.execution && <p className="wq-execution">{work.simulation ? "Simulated execution" : "Execution"}: {step.execution.status}{step.execution.reference && <> · <code>{step.execution.reference}</code></>}{step.execution.status === "uncertain" && " · reconcile the persisted execution before further action"}</p>}
    {!finished && !linked && !managed && step.execution && ["started", "uncertain"].includes(step.execution.status) && <details className="wq-reconcile" open><summary>Reconcile execution</summary><form className="wq-form" onSubmit={(event) => { event.preventDefault(); act("reconcile_step", { result: reconcileResult, reference: reconcileReference.trim(), reason: reconcileReason.trim() }); }}>
      <p className="hint">Check the recorded execution <code>{step.execution.id}</code> against its actual result. Reconciliation records what happened; it does not retry the action.</p>
      <label>Verified result<select value={reconcileResult} onChange={(event) => setReconcileResult(event.target.value)} disabled={busy}><option value="confirmed">Confirmed</option><option value="failed">Failed</option></select></label>
      <label>Verified reference<input value={reconcileReference} onChange={(event) => setReconcileReference(event.target.value)} required maxLength={2000} disabled={busy} /></label>
      <label>Verification evidence and reason<textarea value={reconcileReason} onChange={(event) => setReconcileReason(event.target.value)} required maxLength={2000} disabled={busy} /></label>
      <button className="secondary" disabled={busy || !reconcileReference.trim() || !reconcileReason.trim()}>Record reconciled result</button>
    </form></details>}
    {evidence.length > 0 && <ul className="wq-evidence">{evidence.map((item) => <li key={item.id}><strong>{label(item.kind)}</strong><span>{item.summary}</span><small>{item.recorded_by} · {item.source} · {stamp(item.created_at)}{item.simulation ? " · simulated" : ""}</small></li>)}</ul>}
    {!finished && managed && <p className="hint">Managed by the linked purchasing and inventory workflow.</p>}
    {!finished && !managed && <>
      <p className="hint">Required evidence: {step.required_evidence.map(label).join(", ") || "None"}</p>
      <details><summary>Record evidence</summary><form className="wq-form" onSubmit={(event) => { event.preventDefault(); void mutate(`/work/${encodeURIComponent(work.id)}/evidence`, { ...command(work), step_id: step.id, kind, summary: summary.trim(), source: source.trim(), recorded_by: recordedBy.trim() }); }}>
        <label>Evidence type<select value={kind} onChange={(e) => setKind(e.target.value)} disabled={busy}>{step.required_evidence.map((value) => <option key={value} value={value}>{label(value)}</option>)}</select></label>
        <label>What was verified?<textarea value={summary} onChange={(e) => setSummary(e.target.value)} required maxLength={2000} disabled={busy} /></label><div className="wq-fields"><label>Evidence source<input value={source} onChange={(e) => setSource(e.target.value)} required maxLength={300} disabled={busy} /></label><label>Recorded by<input value={recordedBy} onChange={(e) => setRecordedBy(e.target.value)} required maxLength={128} disabled={busy} /></label></div>
        <button className="secondary" disabled={busy || !dependenciesReady || !summary.trim() || !source.trim() || !recordedBy.trim()}>Record evidence</button>
      </form></details>
      <div className="wq-actions">
        {step.status === "pending" && <button className="secondary" disabled={busy || !dependenciesReady} onClick={() => act("start_step")}>Start step</button>}
        {external && !linked && work.simulation && !step.execution && <button className="secondary" disabled={busy || !dependenciesReady || (!!step.approval && step.approval.status !== "approved")} onClick={() => act("simulate_step")}>Simulate action</button>}
        {linked && <p className="hint">Use the linked request to approve and execute this action.</p>}
        <button className="secondary" disabled={busy || !dependenciesReady || !hasEvidence || (external && step.execution?.status !== "confirmed")} onClick={() => act("complete_step")}>Confirm step complete</button>
      </div>
    </>}
  </li>;
}
