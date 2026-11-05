import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import "./development.css";

type Change = {
  id: string; text: string; fingerprint: string; merge_fingerprint: string; auto_merge: boolean; state: string;
  run_url: string | null; pr_url: string | null; pr_number: number | null;
  merge_sha: string | null; error: string | null;
};
type Snapshot = { enabled: boolean; repository: string; requests: Change[] };
type Recognition = {
  lang: string; continuous: boolean; interimResults: boolean;
  onresult: ((event: { results: { [key: number]: { [key: number]: { transcript: string } } } }) => void) | null;
  onerror: ((event: { error: string }) => void) | null;
  onend: (() => void) | null; start: () => void; stop: () => void;
};
type RecognitionConstructor = new () => Recognition;
const Speech = (window as unknown as { SpeechRecognition?: RecognitionConstructor; webkitSpeechRecognition?: RecognitionConstructor });
const SpeechRecognition = Speech.SpeechRecognition || Speech.webkitSpeechRecognition;
const labels: Record<string, string> = {
  draft: "Review before sending", failed_to_dispatch: "Not sent", dispatching: "Sending to GitHub",
  submitted: "Waiting for cloud Claude", queued: "Cloud run queued", in_progress: "Claude is working in the cloud",
  uncertain: "Check the existing run", pr_ready: "Change ready for review", merged: "Merged · local update needs approval",
  closed: "PR closed", failed: "Cloud run stopped",
};

export default function Development() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [listening, setListening] = useState(false);
  const recognition = useRef<Recognition | null>(null);
  const draftId = useRef(crypto.randomUUID());

  useEffect(() => {
    let alive = true;
    let polling = false;
    async function refresh() {
      if (polling) return;
      polling = true;
      try {
        let next = await api<Snapshot>("/development");
        if (next.enabled) {
          for (const item of next.requests.filter((r) => ["submitted", "queued", "in_progress", "uncertain", "pr_ready"].includes(r.state))) {
            await api<Change>(`/development/${item.id}/refresh`, {});
          }
          next = await api<Snapshot>("/development");
        }
        if (alive) { setSnapshot(next); setError(""); }
      } catch (err) {
        if (alive) setError(err instanceof Error ? err.message : "Could not refresh cloud development");
      } finally { polling = false; }
    }
    void refresh();
    const timer = setInterval(() => void refresh(), 15000);
    return () => { alive = false; clearInterval(timer); recognition.current?.stop(); };
  }, []);

  async function perform(action: () => Promise<unknown>) {
    setBusy(true); setError("");
    try { await action(); setSnapshot(await api<Snapshot>("/development")); }
    catch (err) { setError(err instanceof Error ? err.message : "Development request failed"); }
    finally { setBusy(false); }
  }
  function listen() {
    if (!SpeechRecognition) return;
    recognition.current?.stop();
    const instance = new SpeechRecognition();
    recognition.current = instance;
    instance.lang = "en-US"; instance.continuous = false; instance.interimResults = false;
    instance.onresult = (event) => setText((previous) => (previous ? previous + "\n" : "") + event.results[0][0].transcript);
    instance.onerror = (event) => { setError(`Microphone: ${event.error}. You can type your request.`); setListening(false); };
    instance.onend = () => setListening(false);
    try { instance.start(); setListening(true); }
    catch { setError("Could not start the microphone. You can type your request."); }
  }
  return <section className="development" aria-labelledby="development-title">
    <div className="development-heading"><span className="development-eyebrow">Improve your assistant</span>
      <h2 id="development-title">Tell Claude what to change</h2>
      <p>Report a bug or describe a feature. Claude edits and tests the code in the cloud, then opens a change for your review.</p>
    </div>
    {!snapshot?.enabled && <p className="development-notice">Cloud development is disabled. Follow the repository’s self-improvement setup guide to connect it.</p>}
    <form onSubmit={(event) => { event.preventDefault(); void perform(async () => {
      await api<Change>("/development", { id: draftId.current, text: text.trim() });
      setText(""); draftId.current = crypto.randomUUID();
    }); }}>
      <label htmlFor="development-feedback">What should change?</label>
      <textarea id="development-feedback" rows={4} value={text} maxLength={8000}
        disabled={busy || !snapshot?.enabled} onChange={(event) => setText(event.target.value)}
        placeholder="For example: add monitoring that alerts when room temperature drops 3°C within 10 minutes." />
      <div className="development-actions">
        {SpeechRecognition && <button type="button" disabled={busy || !snapshot?.enabled}
          aria-pressed={listening} onClick={() => listening ? recognition.current?.stop() : listen()}>
          {listening ? "Stop listening" : "Speak request"}</button>}
        <button type="submit" disabled={busy || listening || !text.trim() || !snapshot?.enabled}>Save for review</button>
      </div>
      <p className="development-caption">Only the text you explicitly send leaves this app. Remove names, credentials and private details. Cloud runs use API credits.</p>
    </form>
    {error && <p role="alert" className="development-error">{error}</p>}
    <div className="development-list">{snapshot?.requests.map((item) => <article key={item.id}>
      <div className="development-status">{labels[item.state] || item.state}</div>
      <p className="development-request">{item.text}</p>
      {item.error && <p className="development-error">{item.error}</p>}
      {["draft", "failed_to_dispatch"].includes(item.state) && <div>
        <p className="development-caption">Send this exact request to {snapshot.repository}? Claude can implement backend features, interface changes and bug fixes for review.</p>
        <p className="development-caption">Merging updates the repository after validation. Installing that revision on the demo Mac is a separate approval.</p>
        <button disabled={busy} onClick={() => void perform(() => api<Change>(`/development/${item.id}/send`, { fingerprint: item.fingerprint }))}>
          Implement and open a PR
        </button>
        <button disabled={busy} onClick={() => void perform(() => api<Change>(`/development/${item.id}/send`, { fingerprint: item.merge_fingerprint, auto_merge: true }))}>
          Implement and merge after checks
        </button>
      </div>}
      <div className="development-actions">
        {item.run_url && <a href={item.run_url} target="_blank" rel="noreferrer">View cloud run</a>}
        {item.pr_url && <a href={item.pr_url} target="_blank" rel="noreferrer">Review change #{item.pr_number}</a>}
        {!["draft", "failed_to_dispatch"].includes(item.state) && <button disabled={busy}
          onClick={() => void perform(() => api<Change>(`/development/${item.id}/refresh`, {}))}>Refresh status</button>}
      </div>
      {!["draft", "failed_to_dispatch"].includes(item.state) && <p className="development-caption">{item.auto_merge ? "Authorized to merge scoped code after checks pass. Local installation still needs approval." : "Review-only: the cloud workflow will leave a draft PR."}</p>}
      {item.state === "merged" && item.merge_sha && <div className="development-update">
        <p>Review the merged revision, then run this on the demo Mac to approve its local update:</p>
        <code>python3 scripts/local_demo.py update --pr {item.pr_number} --approve-sha {item.merge_sha}</code>
        <p>The local supervisor checks the app after restarting. Code rollback preserves office data.</p>
      </div>}
    </article>)}</div>
  </section>;
}
