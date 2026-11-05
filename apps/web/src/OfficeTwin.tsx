import { useEffect, useRef, useState } from "react";
import type { OfficeRequest, Station } from "@office/contracts";
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { createOfficeScene } from "./officeScene";
import { createTwinDevice, loadTwinDevices, saveTwinDevices, type TwinDevice } from "./twinDevices";
import { TwinDevicePanel } from "./TwinDevicePanel";
import { createDeviceMesh, disposeDeviceMeshes, isOfficeFloor } from "./twinDeviceScene";
import "./office-twin.css";

type StationId = Station["id"];
type View = "perspective" | "plan";
type Props = {
  stations: Station[];
  requests: OfficeRequest[];
  connected: boolean;
  offline: boolean;
  disabled: boolean;
  onPress: (station: Station) => void;
  onStockLow: () => void;
  onScenario: (station: StationId, text: string) => void;
  onRequest: (id: string) => void;
};
const ids: StationId[] = ["kitchen", "bathroom", "printer"];
const locations = {
  kitchen: { name: "Coffee point", zone: "CAFÉ & LOUNGE", icon: "coffee", description: "The oak counter, coffee machine and shared supplies at the heart of the office.", note: "Modelled from your office photos", equipment: "Coffee machine · Cups · Shared supplies" },
  bathroom: { name: "Bathroom", zone: "CLOAKROOM & WC", icon: "water", description: "Keep everyday essentials stocked and route facilities issues to the right team.", note: "WC interior inferred from the corridor", equipment: "Toilet paper · Soap · Plumbing" },
  printer: { name: "Print station", zone: "SHARED SERVICES", icon: "printer", description: "Paper, toner and printer maintenance, connected to the same office workflow.", note: "Illustrative placement · location unverified", equipment: "Printer · Paper · Toner" },
} as const;
const labels: Record<OfficeRequest["status"], string> = {
  processing: "Agent working", awaiting_input: "Waiting for details", awaiting_approval: "Approval needed", needs_clarification: "More details needed", maintenance_open: "Maintenance open", ordered: "Order simulated", resolved: "Resolved", failed: "Needs attention",
};
function currentRequest(requests: OfficeRequest[], station: StationId) {
  const matching = requests.filter((r) => r.station_id === station).sort((a, b) => b.updated_at.localeCompare(a.updated_at));
  return matching.find((r) => !["ordered", "resolved"].includes(r.status)) ?? matching[0];
}
function mood(request?: OfficeRequest) {
  if (!request || request.status === "resolved") return "quiet";
  if (request.status === "failed" || request.status === "maintenance_open") return "attention";
  if (request.status === "ordered") return "done";
  return "pending";
}
export function TwinIcon({ name }: { name: string }) {
  return <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {name === "coffee" ? <><path d="M4 9h12v7a4 4 0 0 1-4 4H8a4 4 0 0 1-4-4Z"/><path d="M16 10h2a3 3 0 0 1 0 6h-2M8 3v3m4-3v3M3 23h15"/></> : name === "water" ? <><path d="M12 3s-6 7-6 11a6 6 0 0 0 12 0c0-4-6-11-6-11Z"/><path d="M9 14a3 3 0 0 0 3 3"/></> : name === "printer" ? <><path d="M6 8V3h12v5M6 17H3V8h18v9h-3M6 14h12v7H6Z"/><path d="M17 11h1"/></> : name === "cube" ? <><path d="m12 2 9 5v10l-9 5-9-5V7Z"/><path d="m3 7 9 5 9-5M12 12v10m-5-17 10 5"/></> : name === "reset" ? <><path d="M3 11a9 9 0 1 1 3 8M3 4v7h7"/></> : name === "expand" ? <><path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/></> : <><path d="M4 4h16v16H4Zm6 0v16m0-9h10"/></>}
  </svg>;
}

export function OfficeTwin(props: Props) {
  const { stations, requests, connected, offline, disabled, onPress, onStockLow, onScenario, onRequest } = props;
  const [selected, setSelected] = useState<StationId>("kitchen");
  const [view, setView] = useState<View>("perspective");
  const [expanded, setExpanded] = useState(false);
  const [renderError, setRenderError] = useState("");
  const [ready, setReady] = useState(false);
  const [devices, setDevices] = useState<TwinDevice[]>(loadTwinDevices);
  const [selectedDeviceId, setSelectedDeviceId] = useState<string | null>(() => loadTwinDevices()[0]?.id ?? null);
  const [inspector, setInspector] = useState<"devices" | "stations">("devices");
  const [placing, setPlacing] = useState(false);
  const [deviceFeedback, setDeviceFeedback] = useState("");
  const container = useRef<HTMLDivElement>(null);
  const sidePanel = useRef<HTMLDivElement>(null);
  const markerElements = useRef<Partial<Record<StationId, HTMLButtonElement | null>>>({});
  const deviceElements = useRef<Record<string, HTMLButtonElement | null>>({});
  const deviceState = useRef({ devices, selectedDeviceId, placing });
  deviceState.current = { devices, selectedDeviceId, placing };
  const viewer = useRef<{ view: (mode: View) => void; zoom: (delta: number) => void; highlight: (station: StationId, states: Record<StationId, string>) => void; devices: (items: TwinDevice[], selectedId: string | null, states: Record<StationId, string>) => void } | null>(null);
  const meta = locations[selected];
  const station = stations.find((s) => s.id === selected);
  const request = currentRequest(requests, selected);
  const state = mood(request);
  const open = requests.filter((r) => !["ordered", "resolved"].includes(r.status));

  useEffect(() => {
    const host = container.current;
    if (!host) return;
    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    } catch {
      setRenderError("3D isn’t available in this browser. You can still select a station below and use the office controls.");
      return;
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFShadowMap;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.25;
    renderer.domElement.setAttribute("aria-label", "3D cutaway of the Founders Hub office. Drag to orbit and scroll to zoom. Use the station buttons to explore.");
    host.prepend(renderer.domElement);
    const scene = new THREE.Scene();
    const { group, hotspots } = createOfficeScene();
    scene.add(group);
    const deviceGroup = new THREE.Group();
    scene.add(deviceGroup);
    const box = new THREE.Box3().setFromObject(group);
    const center = box.getCenter(new THREE.Vector3());
    center.y = 0;
    const camera = new THREE.OrthographicCamera(-20, 20, 20, -20, 0.1, 200);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.target.copy(center);
    controls.enableDamping = true;
    controls.dampingFactor = 0.09;
    controls.enablePan = false;
    controls.minPolarAngle = 0.02;
    controls.maxPolarAngle = Math.PI / 2.5;
    controls.minZoom = 0.65;
    controls.maxZoom = 2.4;
    const ambient = new THREE.HemisphereLight(0xf5f8ff, 0x7e8171, 2.5);
    scene.add(ambient);
    const sun = new THREE.DirectionalLight(0xffe5c3, 4);
    sun.position.set(-12, 24, 14);
    sun.castShadow = true;
    sun.shadow.mapSize.set(2048, 2048);
    Object.assign(sun.shadow.camera, { left: -23, right: 23, top: 23, bottom: -23, near: 1, far: 70 });
    sun.shadow.normalBias = 0.04;
    sun.shadow.bias = -0.0002;
    sun.target.position.copy(center);
    scene.add(sun, sun.target);
    const fill = new THREE.DirectionalLight(0xd4e7ff, 1.4);
    fill.position.set(12, 10, -18);
    scene.add(fill);
    const ground = new THREE.Mesh(new THREE.PlaneGeometry(200, 200), new THREE.ShadowMaterial({ color: 0x4d5a55, opacity: 0.16 }));
    ground.rotation.x = -Math.PI / 2;
    ground.position.y = box.min.y - 0.04;
    ground.receiveShadow = true;
    scene.add(ground);
    const grid = new THREE.GridHelper(100, 100, 0xa6b2ad, 0xa6b2ad);
    grid.position.y = box.min.y - 0.03;
    (grid.material as THREE.Material).transparent = true;
    (grid.material as THREE.Material).opacity = 0.18;
    scene.add(grid);
    const rings = {} as Record<StationId, THREE.Mesh<THREE.RingGeometry, THREE.MeshBasicMaterial>>;
    for (const id of ids) {
      const ring = new THREE.Mesh(new THREE.RingGeometry(0.45, 0.53, 48), new THREE.MeshBasicMaterial({ color: 0xe08b52, side: THREE.DoubleSide, transparent: true, opacity: 0.85, depthTest: false }));
      ring.rotation.x = -Math.PI / 2;
      ring.position.set(hotspots[id].x, 0.05, hotspots[id].z);
      ring.renderOrder = 2;
      rings[id] = ring;
      scene.add(ring);
    }
    let mode: View = "perspective";
    const setView = (next: View) => {
      mode = next;
      controls.enableRotate = next !== "plan";
      camera.position.copy(center).add(next === "plan" ? new THREE.Vector3(0, 38, 0.01) : new THREE.Vector3(24, 27, 30));
      camera.zoom = 1;
      camera.lookAt(center);
      controls.update();
      resize();
    };
    setView("perspective");
    function resize() {
      const width = host!.clientWidth, height = host!.clientHeight;
      if (!width || !height) return;
      const aspect = width / height;
      camera.updateMatrixWorld();
      let halfWidth = 0, halfHeight = 0;
      for (const x of [box.min.x, box.max.x]) for (const y of [box.min.y, box.max.y]) for (const z of [box.min.z, box.max.z]) {
        const corner = new THREE.Vector3(x, y, z).applyMatrix4(camera.matrixWorldInverse);
        halfWidth = Math.max(halfWidth, Math.abs(corner.x));
        halfHeight = Math.max(halfHeight, Math.abs(corner.y));
      }
      const span = Math.max(halfHeight * 2, halfWidth * 2 / aspect) * 1.22;
      camera.left = -span * aspect / 2;
      camera.right = span * aspect / 2;
      camera.top = span / 2;
      camera.bottom = -span / 2;
      camera.updateProjectionMatrix();
      renderer.setSize(width, height);
    }
    const observer = new ResizeObserver(resize);
    observer.observe(host);
    resize();
    viewer.current = {
      view: setView,
      zoom: (delta) => { camera.zoom = THREE.MathUtils.clamp(camera.zoom + delta, controls.minZoom, controls.maxZoom); camera.updateProjectionMatrix(); },
      highlight: (id, states) => {
        for (const key of ids) {
          rings[key].scale.setScalar(key === id ? 1.5 : 1);
          rings[key].material.color.set(states[key] === "attention" ? 0xc85c4d : states[key] === "pending" ? 0xe69a45 : 0x5c9c87);
          rings[key].material.opacity = key === id ? 1 : 0.5;
        }
      },
      devices: (items, selectedId, states) => {
        disposeDeviceMeshes(deviceGroup);
        for (const item of items) deviceGroup.add(createDeviceMesh(item, item.id === selectedId, states[item.stationId]));
      },
    };
    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();
    const floor = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);
    const intersection = new THREE.Vector3();
    let pointerStart = { x: 0, y: 0 };
    const pointerDown = (event: PointerEvent) => { pointerStart = { x: event.clientX, y: event.clientY }; };
    const pointerUp = (event: PointerEvent) => {
      if (Math.hypot(event.clientX - pointerStart.x, event.clientY - pointerStart.y) > 7) return;
      const rect = renderer.domElement.getBoundingClientRect();
      pointer.set((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1);
      raycaster.setFromCamera(pointer, camera);
      if (deviceState.current.placing && deviceState.current.selectedDeviceId) {
        if (!raycaster.ray.intersectPlane(floor, intersection) || !isOfficeFloor(intersection.x, intersection.z)) {
          setDeviceFeedback("Choose a point inside the office floor.");
          return;
        }
        const position = { x: Math.round(intersection.x * 100) / 100, z: Math.round(intersection.z * 100) / 100 };
        setDevices((current) => current.map((device) => device.id === deviceState.current.selectedDeviceId ? { ...device, position } : device));
        setPlacing(false);
        setDeviceFeedback("Device position saved in this browser.");
        return;
      }
      const hit = raycaster.intersectObjects(deviceGroup.children, true).find((candidate) => candidate.object instanceof THREE.Mesh);
      if (hit) {
        let node: THREE.Object3D | null = hit.object;
        while (node && !node.userData.deviceId) node = node.parent;
        if (node?.userData.deviceId) {
          setSelectedDeviceId(node.userData.deviceId as string);
          setInspector("devices");
          setPlacing(false);
        }
      }
    };
    renderer.domElement.addEventListener("pointerdown", pointerDown);
    renderer.domElement.addEventListener("pointerup", pointerUp);
    const projected = new THREE.Vector3();
    let frame = 0;
    let visible = true;
    const visibility = new IntersectionObserver(([entry]) => { visible = entry.isIntersecting; });
    visibility.observe(host);
    function draw() {
      frame = requestAnimationFrame(draw);
      if (!visible) return;
      controls.update();
      for (const id of ids) {
        const element = markerElements.current[id];
        if (!element) continue;
        projected.copy(hotspots[id]);
        projected.y += mode === "plan" ? 0.3 : 1.4;
        projected.project(camera);
        const x = THREE.MathUtils.clamp((projected.x * 0.5 + 0.5) * host!.clientWidth, element.offsetWidth / 2 + 12, host!.clientWidth - element.offsetWidth / 2 - 12);
        const offset = id === "bathroom" ? -43 : id === "printer" ? -18 : 12;
        const y = THREE.MathUtils.clamp((-projected.y * 0.5 + 0.5) * host!.clientHeight + offset, 105, host!.clientHeight - 85);
        element.style.transform = `translate(${x}px, ${y}px) translate(-50%, -100%)`;
        element.style.visibility = projected.z < 1 && x > 0 && x < host!.clientWidth && y > 0 && y < host!.clientHeight ? "visible" : "hidden";
      }
      for (const device of deviceState.current.devices) {
        const element = deviceElements.current[device.id];
        if (!element) continue;
        projected.set(device.position.x, mode === "plan" ? 0.4 : 1.8, device.position.z).project(camera);
        const x = THREE.MathUtils.clamp((projected.x * 0.5 + 0.5) * host!.clientWidth, element.offsetWidth / 2 + 10, host!.clientWidth - element.offsetWidth / 2 - 10);
        const y = THREE.MathUtils.clamp((-projected.y * 0.5 + 0.5) * host!.clientHeight, 98, host!.clientHeight - 65);
        element.style.transform = `translate(${x}px, ${y}px) translate(-50%, -100%)`;
        element.style.visibility = projected.z < 1 && projected.x > -1.2 && projected.x < 1.2 && projected.y > -1.2 && projected.y < 1.2 ? "visible" : "hidden";
      }
      renderer.render(scene, camera);
    }
    const contextLost = (event: Event) => { event.preventDefault(); setRenderError("The 3D view was interrupted. Reload to restore it, or continue with the station controls below."); };
    renderer.domElement.addEventListener("webglcontextlost", contextLost);
    draw();
    setReady(true);
    return () => {
      cancelAnimationFrame(frame);
      observer.disconnect();
      visibility.disconnect();
      controls.dispose();
      renderer.domElement.removeEventListener("pointerdown", pointerDown);
      renderer.domElement.removeEventListener("pointerup", pointerUp);
      renderer.domElement.removeEventListener("webglcontextlost", contextLost);
      const geometries = new Set<THREE.BufferGeometry>(), materials = new Set<THREE.Material>();
      scene.traverse((object) => {
        if (object instanceof THREE.Mesh || object instanceof THREE.LineSegments) {
          geometries.add(object.geometry);
          for (const material of Array.isArray(object.material) ? object.material : [object.material]) materials.add(material);
        }
      });
      geometries.forEach((geometry) => geometry.dispose());
      materials.forEach((material) => material.dispose());
      renderer.dispose();
      renderer.domElement.remove();
      viewer.current = null;
    };
  }, []);
  useEffect(() => {
    viewer.current?.highlight(selected, Object.fromEntries(ids.map((id) => [id, mood(currentRequest(requests, id))])) as Record<StationId, string>);
  }, [selected, requests, ready]);
  useEffect(() => {
    viewer.current?.devices(devices, selectedDeviceId, Object.fromEntries(ids.map((id) => [id, mood(currentRequest(requests, id))])) as Record<StationId, string>);
  }, [devices, selectedDeviceId, requests, ready]);
  useEffect(() => {
    if (!saveTwinDevices(devices)) setDeviceFeedback("Browser storage is unavailable. Changes will last for this visit only.");
  }, [devices]);
  useEffect(() => {
    if (sidePanel.current) sidePanel.current.scrollTop = 0;
  }, [selectedDeviceId, inspector]);
  useEffect(() => {
    if (!expanded) return;
    const close = (event: KeyboardEvent) => { if (event.key === "Escape") setExpanded(false); };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [expanded]);
  function changeView(next: View) { setView(next); viewer.current?.view(next); }
  function addDevice(kind: "button" | "level") {
    if (devices.length >= 20) { setDeviceFeedback("You can place up to 20 devices in this office."); return; }
    const device = createTwinDevice(kind, devices.length + 1);
    setDevices((current) => [...current, device]);
    setSelectedDeviceId(device.id);
    setInspector("devices");
    setPlacing(true);
    changeView("plan");
    setDeviceFeedback("Click the office floor to place your new device.");
  }
  function selectDevice(id: string) {
    setSelectedDeviceId(id);
    const device = devices.find((item) => item.id === id);
    if (device) setSelected(device.stationId);
    setInspector("devices");
    setPlacing(false);
  }

  return <section className={`office-twin ${expanded ? "twin-expanded" : ""}`} aria-label="Office digital twin">
    <div className="twin-toolbar">
      <div className="twin-floor"><TwinIcon name="cube"/><div>Founders Hub <span>/</span> <strong>Floor 01</strong><small>OFFICE DIGITAL TWIN</small></div></div>
      <div className="twin-view-controls" aria-label="Camera view">
        <button className={view === "perspective" ? "active" : ""} aria-pressed={view === "perspective"} onClick={() => changeView("perspective")}><TwinIcon name="cube"/>3D view</button>
        <button className={view === "plan" ? "active" : ""} aria-pressed={view === "plan"} onClick={() => changeView("plan")}><TwinIcon name="plan"/>Floor plan</button>
      </div>
      <button className="twin-icon-button expand-control" aria-label={expanded ? "Exit expanded view" : "Expand office view"} aria-pressed={expanded} onClick={() => setExpanded(!expanded)}><TwinIcon name="expand"/></button>
    </div>
    <div className="twin-body">
      <div className="twin-map-wrap">
        <div className="twin-map-caption"><span className="twin-map-dot"/> YOUR SPACE, CONNECTED<small>Photo-informed model · approximate layout</small></div>
        <div className={`twin-canvas ${placing ? "placing-device" : ""}`} ref={container}>
          {renderError ? <div className="twin-fallback" role="status"><TwinIcon name="cube"/><p>{renderError}</p></div> : <div className="twin-hotspots">
            {inspector === "stations" && ids.map((id) => { const activeRequest = currentRequest(requests, id); return <button key={id} ref={(element) => { markerElements.current[id] = element; }} className={`twin-hotspot ${selected === id ? "selected" : ""} mood-${mood(activeRequest)}`} aria-pressed={selected === id} aria-label={`${locations[id].name}: ${offline ? "Status unavailable" : activeRequest ? labels[activeRequest.status] : "No open requests"}`} onClick={() => setSelected(id)}><span className="hotspot-pin"><TwinIcon name={locations[id].icon}/></span><span className="hotspot-name">{locations[id].name}<small>{offline ? "Status unavailable" : activeRequest ? labels[activeRequest.status] : "No open requests"}</small></span><span className="hotspot-tail"/></button>; })}
            {inspector === "devices" && devices.map((device) => <button key={device.id} ref={(element) => { deviceElements.current[device.id] = element; }} className={`twin-hotspot twin-device-marker ${selectedDeviceId === device.id ? "selected" : ""} mood-${mood(currentRequest(requests, device.stationId))}`} aria-pressed={selectedDeviceId === device.id} aria-label={`Configure ${device.name}`} onClick={() => selectDevice(device.id)}><span className="hotspot-pin">{device.kind === "button" ? "◉" : "⌁"}</span><span className="hotspot-name">{device.name}<small>{device.kind === "level" ? `${device.tracking.value}${device.tracking.unit} · simulated` : `${currentRequest(requests, device.stationId) ? labels[currentRequest(requests, device.stationId)!.status] : "Mapped"} · ${device.stationId}`}</small></span><span className="hotspot-tail"/></button>)}
          </div>}
          {placing && <div className="twin-placement-banner" role="status">Click the floor to place the device <button onClick={() => setPlacing(false)}>Cancel</button></div>}
        </div>
        <div className="twin-map-bottom"><span className="twin-compass">N<i>↑</i></span><span className="twin-orbit-hint">Drag to orbit <b>·</b> Scroll to zoom</span><div className="twin-zoom"><button aria-label="Zoom out" disabled={!!renderError} onClick={() => viewer.current?.zoom(-0.15)}>−</button><button aria-label="Reset camera" disabled={!!renderError} onClick={() => changeView(view)}><TwinIcon name="reset"/></button><button aria-label="Zoom in" disabled={!!renderError} onClick={() => viewer.current?.zoom(0.15)}>+</button></div></div>
        <div className="twin-map-legend"><span><i className="legend-quiet"/>No open requests</span><span><i className="legend-pending"/>In progress</span><span><i className="legend-attention"/>Needs attention</span></div>
      </div>
      <div className="twin-side" ref={sidePanel}><div className="twin-inspector-switch"><button aria-pressed={inspector === "devices"} className={inspector === "devices" ? "active" : ""} onClick={() => { setInspector("devices"); setPlacing(false); }}>Devices <span>{devices.length}</span></button><button aria-pressed={inspector === "stations"} className={inspector === "stations" ? "active" : ""} onClick={() => { setInspector("stations"); setPlacing(false); }}>Stations <span>{stations.length}</span></button></div>
      {inspector === "devices" ? <TwinDevicePanel devices={devices} selectedId={selectedDeviceId} stations={stations} requests={requests} disabled={disabled} offline={offline} placing={placing} onSelect={selectDevice} onAdd={addDevice} onSave={(device) => { setDevices((current) => current.map((item) => item.id === device.id ? device : item)); setSelected(device.stationId); setDeviceFeedback("Configuration saved in this browser."); }} onPlace={() => { setPlacing(true); changeView("plan"); setDeviceFeedback("Click the office floor to place this device."); }} onCancelPlace={() => setPlacing(false)} onSimulate={(device) => { const mapped = stations.find((item) => item.id === device.stationId); if (!mapped) return; if (device.kind === "button") onPress(mapped); else onScenario(device.stationId, `${device.tracking.label} is running low`); }} onRequest={onRequest} onRemove={(id) => { setDevices((current) => current.filter((item) => item.id !== id)); setSelectedDeviceId(devices.find((item) => item.id !== id)?.id ?? null); setPlacing(false); setDeviceFeedback("Device removed from this office layout."); }} /> : <aside className="twin-inspector" aria-label="Selected station">
        <div className="twin-inspector-top"><span>STATION DETAILS</span><span className={`twin-sync ${offline ? "offline" : ""}`}><i/>{offline ? "Offline" : connected ? "API live" : "Syncing"}</span></div>
        <div className="twin-station-tabs" aria-label="Select a station">{ids.map((id) => <button key={id} className={selected === id ? "selected" : ""} aria-pressed={selected === id} aria-label={`Select ${locations[id].name}`} onClick={() => setSelected(id)}><TwinIcon name={locations[id].icon}/></button>)}</div>
        <div className="twin-zone">{meta.zone}</div><h2>{meta.name}<span>.</span></h2><p className="twin-description">{meta.description}</p>
        <div className="twin-station-state"><span className={`twin-state-dot mood-${state}`}/><strong>{offline ? "Status unavailable" : request ? labels[request.status] : "No open requests"}</strong><span>{open.filter((r) => r.station_id === selected).length.toString().padStart(2, "0")}</span></div>
        <dl className="twin-facts"><div><dt>Responsible team</dt><dd>{station?.responsible_team ?? "Connecting…"}</dd></div><div><dt>Equipment & supplies</dt><dd>{meta.equipment}</dd></div></dl>
        <p className="twin-location-note"><TwinIcon name="plan"/>{meta.note}</p>
        {request && <button className="twin-linked-request" onClick={() => onRequest(request.id)}><span>LATEST REQUEST</span><strong>{request.summary || "New station request"}</strong><small>{labels[request.status]} <b>↗</b></small></button>}
        <button className="primary twin-report" disabled={disabled || !station || offline} onClick={() => station && onPress(station)}>Report an issue <span>↗</span></button>
        <p className="twin-virtual-note">Virtual station · no sensor telemetry connected</p>
      </aside>}
      </div>
    </div>
    {deviceFeedback && <div className="twin-device-feedback" role="status">{deviceFeedback}</div>}
    <div className="twin-demo"><div className="twin-demo-intro"><span className="twin-demo-spark">✳</span><div><strong>See the office respond</strong><p>Trigger a simulated event. Follow the real workflow.</p></div></div><div className="twin-scenarios"><button disabled={disabled || offline || stations.length === 0} onClick={() => { setSelected("kitchen"); onScenario("kitchen", "We need coffee"); }}><TwinIcon name="coffee"/>Out of coffee<span>↗</span></button><button disabled={disabled || offline || stations.length === 0} onClick={() => { setSelected("kitchen"); onScenario("kitchen", "The coffee machine is leaking"); }}><TwinIcon name="water"/>Machine leaking<span>↗</span></button><button disabled={disabled || offline || stations.length === 0} onClick={() => { setSelected("bathroom"); onStockLow(); }}><span className="scenario-sensor">⌁</span>Paper running low<span>↗</span></button></div></div>
    <div className="twin-boundary"><span>SIMULATED INPUTS & FULFILMENT</span> Actual request state from the local API. No real orders or bookings.</div>
  </section>;
}
