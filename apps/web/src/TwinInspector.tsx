import { useState, type ReactNode } from "react";
import type { OfficeRequest, Station } from "@office/contracts";
import { byUrgency, inLoop, isMerged, isTicket, loopLabel, mostUrgent, requestTitle, stamp, statusTone } from "./format";
import { EnvironmentReadings, type EnvironmentState } from "./EnvironmentReadings";
import { TwinDeviceSettings } from "./TwinDeviceSettings";
import type { TwinDevice } from "./twinDevices";
import { thingById, thingForRequest, type StationId, type TwinThing } from "./twinThings";

export type TwinSelection = { type: "station" | "thing" | "device"; id: string } | null;
type Props = {
  environment: EnvironmentState;
  selection: TwinSelection;
  stations: Station[];
  requests: OfficeRequest[];
  devices: TwinDevice[];
  offline: boolean;
  disabled: boolean;
  placing: boolean;
  feedback: string;
  showStations: boolean;
  onSelect: (selection: TwinSelection) => void;
  onAdd: (kind: TwinDevice["kind"]) => void;
  onSaveDevice: (device: TwinDevice) => void;
  onPlace: () => void;
  onCancelPlace: () => void;
  onRemoveDevice: (id: string) => void;
  onSimulatePress: (station: Station) => void;
  onSimulateReport: (station: StationId, text: string) => void;
  onAsk: (station: StationId) => void;
  onRequest: (id: string) => void;
};
const newestFirst = (a: OfficeRequest, b: OfficeRequest) => b.updated_at.localeCompare(a.updated_at);
export const stationRequests = (requests: OfficeRequest[], station: StationId) =>
  requests.filter((r) => r.station_id === station && !isMerged(r) && (isTicket(r) || inLoop(r))).sort(newestFirst);
export const reading = (device: TwinDevice) => `${device.tracking.value.toLocaleString()}${device.tracking.unit}`;
export const isLow = (device: TwinDevice) => device.kind === "level" && device.tracking.value <= device.tracking.warningBelow;

export function TwinInspector(props: Props) {
  const { selection, stations, devices, feedback } = props;
  const thing = selection?.type === "thing" ? thingById(selection.id) : undefined;
  const device = selection?.type === "device" ? devices.find((d) => d.id === selection.id) : undefined;
  const stationId = selection?.type === "station" ? (selection.id as StationId) : device?.stationId ?? thing?.stationId ?? null;
  const station = stations.find((s) => s.id === stationId);
  const known = !!selection && (selection.type === "station" ? !!station : !!thing || !!device);
  return (
    <aside className="twin-inspector" aria-label="Details">
      {feedback && <div className="alert error" role="status">{feedback}</div>}
      {known ? (
        <Details {...props} thing={thing} device={device} station={station} title={thing?.name ?? device?.name ?? station?.name ?? ""} />
      ) : (
        <Overview {...props} />
      )}
    </aside>
  );
}

const foldKey = "office-twin-devices-open";
/** Closed by default to keep the status page calm. The choice is remembered per browser. */
function DevicesFold({ devices, children }: { devices: TwinDevice[]; children: ReactNode }) {
  const [open, setOpen] = useState(() => {
    try {
      return localStorage.getItem(foldKey) === "true";
    } catch {
      return false;
    }
  });
  const low = devices.filter(isLow).length;
  return (
    <details
      className="twin-fold"
      open={open}
      onToggle={(event) => {
        const next = event.currentTarget.open;
        setOpen(next);
        try {
          localStorage.setItem(foldKey, String(next));
        } catch {
          // The section still works; it just won't be remembered.
        }
      }}
    >
      <summary className="section-head">
        <h2>Buttons and sensors</h2>
        <span className={low ? "twin-low" : ""}>{low ? `${low} low · simulated` : "Simulated"}</span>
      </summary>
      {children}
    </details>
  );
}

function RequestRows({ requests, onRequest }: { requests: OfficeRequest[]; onRequest: (id: string) => void }) {
  return (
    <div className="queue">
      {requests.map((request) => (
        <button key={request.id} className="row" onClick={() => onRequest(request.id)}>
          <i className={`dot dot-${request.status}`} aria-hidden="true" />
          <span className="row-main">
            <strong>{requestTitle(request)}</strong>
            <small>{loopLabel(request)} · {stamp(request.updated_at)}</small>
          </span>
        </button>
      ))}
    </div>
  );
}

function Overview({ stations, requests, devices, placing, showStations, onSelect, onAdd, onRequest }: Props) {
  const active = requests.filter((r) => inLoop(r) && !isMerged(r)).sort(newestFirst);
  return (
    <>
      <section aria-label="In progress">
        <div className="section-head">
          <h2>In progress</h2>
          {active.length > 0 && <span className="tally">{active.length}</span>}
        </div>
        {active.length === 0 ? <p className="empty">All clear.</p> : <RequestRows requests={byUrgency(active)} onRequest={onRequest} />}
      </section>
      {showStations && (
        <section aria-label="Stations">
          <div className="section-head">
            <h2>Stations</h2>
          </div>
          <div className="queue">
            {stations.map((station) => (
              <button key={station.id} className="row" onClick={() => onSelect({ type: "station", id: station.id })}>
                <span className="row-main"><strong>{station.name}</strong></span>
              </button>
            ))}
          </div>
        </section>
      )}
      <DevicesFold devices={devices}>
        <div className="queue">
          {devices.map((device) => (
            <button key={device.id} className="row" onClick={() => onSelect({ type: "device", id: device.id })}>
              <span className="row-main"><strong>{device.name}</strong></span>
              <span className={`row-status ${isLow(device) ? "twin-low" : ""}`}>{device.kind === "level" ? reading(device) : "Button"}</span>
            </button>
          ))}
        </div>
        <div className="form-actions">
          <button className="secondary twin-small" disabled={devices.length >= 20 || placing} onClick={() => onAdd("button")}>Add button</button>
          <button className="secondary twin-small" disabled={devices.length >= 20 || placing} onClick={() => onAdd("level")}>Add sensor</button>
        </div>
        <p className="hint">The office layout is approximate, based on photos.</p>
      </DevicesFold>
    </>
  );
}

function Details(props: Props & { thing?: TwinThing; device?: TwinDevice; station?: Station; title: string }) {
  const { thing, device, station, title, requests, stations, offline, disabled, placing, onSelect, onAsk, onRequest } = props;
  const all = station ? stationRequests(requests, station.id) : [];
  // An object shows the reports about it; a station or a device shows the whole station.
  const related = thing ? requests.filter((r) => thingForRequest(r)?.id === thing.id) : all;
  const current = mostUrgent(related.filter(inLoop));
  const place = station ? (title === station.name ? station.responsible_team : `${station.name} · ${station.responsible_team}`) : "";
  return (
    <>
      <button className="text-button twin-back" onClick={() => onSelect(null)}>Whole office</button>
      <section aria-label={title}>
        <div className="section-head">
          <h2>{title}</h2>
          {place && <span>{place}</span>}
        </div>
      </section>
      {current && !offline && (
        <div className={`twin-status twin-status-${statusTone(current.status)}`}>
          <strong>{loopLabel(current)}</strong>
          <span>Updated {stamp(current.updated_at)}</span>
        </div>
      )}
      {device?.kind === "level" && (
        <div className={`twin-level ${isLow(device) ? "low" : ""}`}>
          <strong>{reading(device)}</strong>
          <span>{device.tracking.label} · simulated</span>
          <meter min={device.tracking.min} max={device.tracking.max} value={device.tracking.value} aria-label={`${device.tracking.label}, simulated`} />
        </div>
      )}
      {device?.kind === "level" && <EnvironmentReadings boardId={device.stationId} location={station?.name ?? device.stationId} state={props.environment} />}
      {station || thing ? (
        <section aria-label="Reports">
          {related.length === 0 ? (
            <p className="empty">{offline ? "Reconnect to load reports." : "All clear. No reports."}</p>
          ) : (
            <RequestRows requests={byUrgency(related)} onRequest={onRequest} />
          )}
          {station && <button className="text-button" onClick={() => onAsk(station.id)}>Report something here</button>}
        </section>
      ) : (
        <p className="empty">Not connected to the office system.</p>
      )}
      {device && station && (
        <TwinDeviceSettings
          device={device}
          stations={stations}
          placing={placing}
          disabled={disabled || offline}
          onTest={() => (device.kind === "button" ? props.onSimulatePress(station) : props.onSimulateReport(station.id, `${device.tracking.label} is running low`))}
          onSave={props.onSaveDevice}
          onPlace={props.onPlace}
          onCancelPlace={props.onCancelPlace}
          onRemove={props.onRemoveDevice}
        />
      )}
    </>
  );
}
