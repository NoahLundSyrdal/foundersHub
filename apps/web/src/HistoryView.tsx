import type { OfficeRequest, Station } from "@office/contracts";
import { isMerged, money, requestTitle, stamp } from "./format";

const dayLabel = (value: string) => {
  const date = new Date(value);
  return date.toDateString() === new Date().toDateString()
    ? "Today"
    : date.toLocaleDateString([], {
        weekday: "long",
        day: "numeric",
        month: "long",
      });
};

export function HistoryView({
  requests,
  stations,
  statusLabels,
  disabled,
  onOpen,
}: {
  requests: OfficeRequest[];
  stations: Station[];
  statusLabels: Record<string, string>;
  disabled: boolean;
  onOpen: (id: string) => void;
}) {
  const entries = [...requests]
    .sort((a, b) => b.created_at.localeCompare(a.created_at));
  const days = new Map<string, OfficeRequest[]>();
  for (const entry of entries) {
    const label = dayLabel(entry.created_at);
    days.set(label, [...(days.get(label) ?? []), entry]);
  }
  return (
    <section className="history" aria-label="History">
      <div className="section-head">
        <h2>History</h2>
        <span>Latest requests, with their current status</span>
      </div>
      {entries.length === 0 && (
        <p className="empty">Nothing has been said or ordered yet.</p>
      )}
      {[...days].map(([day, items]) => (
        <div className="history-day" key={day}>
          <h3>{day}</h3>
          {items.map((r) => (
            <button
              className="history-row"
              key={r.id}
              disabled={disabled}
              onClick={() => onOpen(r.id)}
            >
              <time dateTime={r.created_at}>{stamp(r.created_at)}</time>
              <i className={`dot dot-${r.status}`} aria-hidden="true" />
              <span className="history-main">
                <strong>
                  {r.original_text ? `“${r.original_text}”` : requestTitle(r)}
                </strong>
                <small>
                  {stations.find((s) => s.id === r.station_id)?.name ??
                    r.station_id}
                  {" · "}
                  {r.proposal
                    ? `${r.proposal.product.name}, ${money(r.proposal.total_nok)} · `
                    : ""}
                  {isMerged(r) ? requestTitle(r) : (statusLabels[r.status] ?? r.status)}
                </small>
              </span>
            </button>
          ))}
        </div>
      ))}
    </section>
  );
}
