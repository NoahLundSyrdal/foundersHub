import { useEffect, useRef, useState } from "react";
import type { OfficeRequest, Station } from "@office/contracts";
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { inLoop, isMoreUrgent, loopLabel, mostUrgent, statusLabels } from "./format";
import { createOfficeScene } from "./officeScene";
import { createTwinDevice, loadTwinDevices, saveTwinDevices, type TwinDevice } from "./twinDevices";
import { createDeviceMesh, disposeDeviceMeshes, isOfficeFloor } from "./twinDeviceScene";
import { isLow, reading, stationRequests, TwinInspector, type TwinSelection } from "./TwinInspector";
import { roomAt, thingById, thingForRequest, thingsAt, type StationId } from "./twinThings";
import "./office-twin.css";

type View = "perspective" | "plan";
type Props = {
  hidden: boolean;
  stations: Station[];
  requests: OfficeRequest[];
  offline: boolean;
  disabled: boolean;
  onSimulatePress: (station: Station) => void;
  onSimulateReport: (station: StationId, text: string) => void;
  onAsk: (station: StationId) => void;
  onRequest: (id: string) => void;
};
type Statuses = Record<string, OfficeRequest["status"] | undefined>;
const ids: StationId[] = ["kitchen", "bathroom", "printer"];
// The same status colours as the dashboard's dots and agent panel.
function statusColor(status: OfficeRequest["status"] | undefined): number | null {
  if (status === "processing") return 0xff7f24;
  if (status === "failed") return 0xff6b66;
  if (status === "ordered") return 0x46dc90;
  return status ? 0xffd233 : null;
}
function thingStatuses(requests: OfficeRequest[]): Statuses {
  const statuses: Statuses = {};
  for (const request of requests.filter(inLoop)) {
    const thing = thingForRequest(request);
    const current = thing && statuses[thing.id];
    if (thing && (!current || isMoreUrgent(request.status, current))) statuses[thing.id] = request.status;
  }
  return statuses;
}
function openStatuses(requests: OfficeRequest[]) {
  return Object.fromEntries(ids.map((id) => [id, mostUrgent(stationRequests(requests, id).filter(inLoop))?.status])) as Record<StationId, OfficeRequest["status"] | undefined>;
}

export function OfficeTwin({ hidden, stations, requests, offline, disabled, onSimulatePress, onSimulateReport, onAsk, onRequest }: Props) {
  const [selection, setSelection] = useState<TwinSelection>(null);
  const [view, setView] = useState<View>("perspective");
  const [renderError, setRenderError] = useState("");
  const [ready, setReady] = useState(false);
  const [devices, setDevices] = useState<TwinDevice[]>(loadTwinDevices);
  const [placing, setPlacing] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [update, setUpdate] = useState<string | null>(null);
  const container = useRef<HTMLDivElement>(null);
  const tip = useRef<HTMLDivElement>(null);
  const markerElements = useRef<Partial<Record<StationId, HTMLButtonElement | null>>>({});
  const deviceElements = useRef<Record<string, HTMLButtonElement | null>>({});
  const deviceState = useRef({ devices, selection, placing });
  deviceState.current = { devices, selection, placing };
  const viewer = useRef<{
    view: (mode: View) => void;
    status: (statuses: Record<StationId, OfficeRequest["status"] | undefined>) => void;
    select: (thingIds: string[]) => void;
    reports: (statuses: Statuses) => void;
    devices: (items: TwinDevice[], selectedId: string | null, statuses: Record<StationId, OfficeRequest["status"] | undefined>) => void;
  } | null>(null);
  const statuses = openStatuses(requests);
  const reported = thingStatuses(requests);
  // Details follow new reports until someone chooses what to look at themselves.
  const following = useRef(true);
  const seen = useRef<Map<string, string> | null>(null);

  useEffect(() => {
    const host = container.current;
    if (!host) return;
    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    } catch {
      setRenderError("3D isn’t available in this browser. You can still use the list beside this.");
      return;
    }
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFShadowMap;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.25;
    renderer.domElement.setAttribute("aria-label", "3D model of the Founders Hub office. The layout is approximate. Drag to turn it, scroll to zoom and click something to see its details.");
    host.prepend(renderer.domElement);
    const scene = new THREE.Scene();
    const { group, hotspots, things } = createOfficeScene();
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
    const ambient = new THREE.HemisphereLight(0xffffff, 0x8a8880, 2.5);
    scene.add(ambient);
    const sun = new THREE.DirectionalLight(0xffefd9, 4);
    sun.position.set(-12, 24, 14);
    sun.castShadow = true;
    sun.shadow.mapSize.set(2048, 2048);
    Object.assign(sun.shadow.camera, { left: -23, right: 23, top: 23, bottom: -23, near: 1, far: 70 });
    sun.shadow.normalBias = 0.04;
    sun.shadow.bias = -0.0002;
    sun.target.position.copy(center);
    scene.add(sun, sun.target);
    const fill = new THREE.DirectionalLight(0xffffff, 1.4);
    fill.position.set(12, 10, -18);
    scene.add(fill);
    const ground = new THREE.Mesh(new THREE.PlaneGeometry(200, 200), new THREE.ShadowMaterial({ color: 0x15130f, opacity: 0.12 }));
    ground.rotation.x = -Math.PI / 2;
    ground.position.y = box.min.y - 0.04;
    ground.receiveShadow = true;
    scene.add(ground);
    const rings = {} as Record<StationId, THREE.Mesh<THREE.RingGeometry, THREE.MeshBasicMaterial>>;
    for (const id of ids) {
      const ring = new THREE.Mesh(new THREE.RingGeometry(0.62, 0.74, 48), new THREE.MeshBasicMaterial({ side: THREE.DoubleSide, depthTest: false }));
      ring.rotation.x = -Math.PI / 2;
      ring.position.set(hotspots[id].x, 0.05, hotspots[id].z);
      ring.renderOrder = 2;
      ring.visible = false;
      rings[id] = ring;
      scene.add(ring);
    }
    // Lighting up a thing swaps in tinted copies of its shared materials.
    const baseMaterials = new Map<THREE.Mesh, THREE.Material>();
    for (const meshes of things.values()) for (const mesh of meshes) baseMaterials.set(mesh, mesh.material as THREE.Material);
    const tints = new Map<string, THREE.Material>();
    const tinted = (material: THREE.Material, color: number, strength: number) => {
      const key = `${material.uuid}-${color}-${strength}`;
      let copy = tints.get(key);
      if (!copy) {
        const standard = (material as THREE.MeshStandardMaterial).clone();
        standard.emissive.set(color);
        standard.emissiveIntensity = strength;
        copy = standard;
        tints.set(key, copy);
      }
      return copy;
    };
    let selectedThings = new Set<string>();
    let hoveredThing: string | null = null;
    let reportedThings: Statuses = {};
    // A reported thing keeps its status colour. Selecting and hovering use the panel's blue.
    const paint = () => {
      for (const [id, meshes] of things) {
        const status = statusColor(reportedThings[id]);
        const color = status ?? 0x5b7cff;
        const strength = status !== null ? (selectedThings.has(id) ? 0.7 : 0.45) : selectedThings.has(id) ? 0.5 : hoveredThing === id ? 0.22 : 0;
        for (const mesh of meshes) {
          const base = baseMaterials.get(mesh)!;
          mesh.material = strength ? tinted(base, color, strength) : base;
        }
      }
    };
    let mode: View = "perspective";
    const setCameraView = (next: View) => {
      mode = next;
      controls.enableRotate = next !== "plan";
      camera.position.copy(center).add(next === "plan" ? new THREE.Vector3(0, 38, 0.01) : new THREE.Vector3(24, 27, 30));
      camera.zoom = 1;
      camera.lookAt(center);
      controls.update();
      resize();
    };
    setCameraView("perspective");
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
      const span = Math.max(halfHeight * 2, halfWidth * 2 / aspect) * 1.08;
      camera.left = -span * aspect / 2;
      camera.right = span * aspect / 2;
      camera.top = span / 2;
      camera.bottom = -span / 2;
      camera.updateProjectionMatrix();
      renderer.setSize(width, height);
      // setSize clears the canvas, so draw straight away instead of flashing blank.
      renderer.render(scene, camera);
    }
    const observer = new ResizeObserver(resize);
    observer.observe(host);
    resize();
    viewer.current = {
      view: setCameraView,
      status: (next) => {
        for (const id of ids) {
          const color = statusColor(next[id]);
          rings[id].visible = color !== null;
          if (color !== null) rings[id].material.color.set(color);
        }
      },
      select: (thingIds) => {
        selectedThings = new Set(thingIds);
        paint();
      },
      reports: (next) => {
        reportedThings = next;
        paint();
      },
      devices: (items, selectedId, next) => {
        disposeDeviceMeshes(deviceGroup);
        for (const item of items) deviceGroup.add(createDeviceMesh(item, item.id === selectedId, statusColor(next[item.stationId])));
      },
    };
    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();
    const floor = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);
    const intersection = new THREE.Vector3();
    const aim = (event: PointerEvent) => {
      const rect = renderer.domElement.getBoundingClientRect();
      pointer.set((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1);
      raycaster.setFromCamera(pointer, camera);
    };
    // What the pointer is really on. Glass is see-through, so clicks pass through it.
    const pick = (): { deviceId?: string; thingId?: string } => {
      const hits = raycaster.intersectObjects([deviceGroup, group], true);
      const hit = hits.find((candidate) => !(candidate.object as THREE.Mesh<THREE.BufferGeometry, THREE.Material>).material.transparent);
      let node: THREE.Object3D | null = hit?.object ?? null;
      while (node && !node.userData.deviceId && !node.userData.thingId) node = node.parent;
      const thingId = node?.userData.thingId ?? (hit && !node?.userData.deviceId ? roomAt(hit.point.x, hit.point.z) : null);
      return { deviceId: node?.userData.deviceId, thingId: thingId ?? undefined };
    };
    let pointerStart = { x: 0, y: 0 };
    const pointerDown = (event: PointerEvent) => {
      pointerStart = { x: event.clientX, y: event.clientY };
    };
    const pointerUp = (event: PointerEvent) => {
      if (Math.hypot(event.clientX - pointerStart.x, event.clientY - pointerStart.y) > 7) return;
      aim(event);
      const { selection: current, placing: isPlacing } = deviceState.current;
      if (isPlacing && current?.type === "device") {
        if (!raycaster.ray.intersectPlane(floor, intersection) || !isOfficeFloor(intersection.x, intersection.z)) {
          setFeedback("Choose a point inside the office floor.");
          return;
        }
        const position = { x: Math.round(intersection.x * 100) / 100, z: Math.round(intersection.z * 100) / 100 };
        setDevices((items) => items.map((device) => (device.id === current.id ? { ...device, position } : device)));
        setPlacing(false);
        setFeedback("");
        return;
      }
      const { deviceId, thingId } = pick();
      following.current = !deviceId && !thingId;
      setSelection(deviceId ? { type: "device", id: deviceId } : thingId ? { type: "thing", id: thingId } : null);
    };
    let hoverEvent: PointerEvent | null = null;
    const pointerMove = (event: PointerEvent) => {
      if (event.buttons === 0) hoverEvent = event;
    };
    const pointerLeave = () => {
      hoverEvent = null;
      hoveredThing = null;
      paint();
      if (tip.current) tip.current.hidden = true;
    };
    const hover = (event: PointerEvent) => {
      if (deviceState.current.placing) {
        renderer.domElement.style.cursor = "";
        pointerLeave();
        return;
      }
      aim(event);
      const { deviceId, thingId } = pick();
      if (thingId !== hoveredThing) {
        hoveredThing = thingId ?? null;
        paint();
      }
      const name = deviceId ? deviceState.current.devices.find((device) => device.id === deviceId)?.name : thingId ? thingById(thingId)?.name : undefined;
      renderer.domElement.style.cursor = name ? "pointer" : "";
      if (tip.current) {
        tip.current.hidden = !name;
        if (name) {
          tip.current.textContent = name;
          const rect = host.getBoundingClientRect();
          tip.current.style.transform = `translate(${event.clientX - rect.left + 14}px, ${event.clientY - rect.top + 14}px)`;
        }
      }
    };
    renderer.domElement.addEventListener("pointerdown", pointerDown);
    renderer.domElement.addEventListener("pointerup", pointerUp);
    renderer.domElement.addEventListener("pointermove", pointerMove);
    renderer.domElement.addEventListener("pointerleave", pointerLeave);
    const projected = new THREE.Vector3();
    let frame = 0;
    let visible = true;
    const visibility = new IntersectionObserver(([entry]) => {
      visible = entry.isIntersecting;
    });
    visibility.observe(host);
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const place = (element: HTMLElement) => {
      const x = THREE.MathUtils.clamp((projected.x * 0.5 + 0.5) * host.clientWidth, element.offsetWidth / 2 + 8, host.clientWidth - element.offsetWidth / 2 - 8);
      const y = THREE.MathUtils.clamp((-projected.y * 0.5 + 0.5) * host.clientHeight, element.offsetHeight + 8, host.clientHeight - 8);
      element.style.transform = `translate(${x}px, ${y}px) translate(-50%, -100%)`;
      element.style.visibility = projected.z < 1 ? "visible" : "hidden";
    };
    function draw() {
      frame = requestAnimationFrame(draw);
      if (!visible) return;
      controls.update();
      const pulse = still ? 1 : 1 + 0.16 * Math.sin(performance.now() / 320);
      for (const id of ids) rings[id].scale.setScalar(pulse);
      if (hoverEvent) {
        hover(hoverEvent);
        hoverEvent = null;
      }
      for (const id of ids) {
        const element = markerElements.current[id];
        if (!element) continue;
        projected.copy(hotspots[id]);
        projected.y += 0.3;
        projected.project(camera);
        place(element);
      }
      for (const device of deviceState.current.devices) {
        const element = deviceElements.current[device.id];
        if (!element) continue;
        projected.set(device.position.x, mode === "plan" ? 0.4 : 1.25, device.position.z).project(camera);
        place(element);
      }
      renderer.render(scene, camera);
    }
    const contextLost = (event: Event) => {
      event.preventDefault();
      setRenderError("The 3D view stopped working. Reload the page to bring it back.");
    };
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
      renderer.domElement.removeEventListener("pointermove", pointerMove);
      renderer.domElement.removeEventListener("pointerleave", pointerLeave);
      renderer.domElement.removeEventListener("webglcontextlost", contextLost);
      const geometries = new Set<THREE.BufferGeometry>(), materials = new Set<THREE.Material>(tints.values());
      for (const material of baseMaterials.values()) materials.add(material);
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
  const statusKey = JSON.stringify(statuses);
  useEffect(() => {
    viewer.current?.status(statuses);
  }, [statusKey, ready]);
  const reportedKey = JSON.stringify(reported);
  useEffect(() => {
    viewer.current?.reports(reported);
  }, [reportedKey, ready]);
  useEffect(() => {
    if (stations.length === 0) return;
    const before = seen.current;
    seen.current = new Map(requests.map((request) => [request.id, request.status]));
    if (!before) return;
    const changed = [...requests].sort((a, b) => b.updated_at.localeCompare(a.updated_at)).find((request) => before.get(request.id) !== request.status);
    if (!changed) return;
    setUpdate(changed.id);
    if (following.current && !deviceState.current.placing) {
      // Show what just happened, then go back to the whole office once it is finished.
      const thing = thingForRequest(changed);
      setSelection(!inLoop(changed) ? null : thing ? { type: "thing", id: thing.id } : { type: "station", id: changed.station_id });
    }
  }, [requests, stations.length]);
  useEffect(() => {
    if (!update) return;
    const timer = setTimeout(() => setUpdate(null), 12000);
    return () => clearTimeout(timer);
  }, [update]);
  useEffect(() => {
    viewer.current?.devices(devices, selection?.type === "device" ? selection.id : null, statuses);
  }, [devices, selection, statusKey, ready]);
  useEffect(() => {
    viewer.current?.select(selection?.type === "thing" ? [selection.id] : selection?.type === "station" ? thingsAt(selection.id as StationId).map((thing) => thing.id) : []);
  }, [selection, ready]);
  useEffect(() => {
    if (!saveTwinDevices(devices)) setFeedback("Browser storage is unavailable. Changes will last for this visit only.");
  }, [devices]);
  function changeView(next: View) {
    setView(next);
    viewer.current?.view(next);
  }
  function addDevice(kind: TwinDevice["kind"]) {
    const device = createTwinDevice(kind, devices.length + 1);
    following.current = false;
    setDevices((current) => [...current, device]);
    setSelection({ type: "device", id: device.id });
    setPlacing(true);
    changeView("plan");
  }
  function select(next: TwinSelection) {
    following.current = next === null;
    setSelection(next);
    setPlacing(false);
    setFeedback("");
  }

  const latestUpdate = requests.find((request) => request.id === update);
  return (
    <section className="office-twin" aria-label="Office status" hidden={hidden}>
      <div className={`twin-canvas ${placing ? "placing-device" : ""}`} ref={container}>
        {renderError ? (
          <div className="twin-fallback" role="status">{renderError}</div>
        ) : (
          <div className="twin-pins">
            {ids.map((id) => {
              const status = statuses[id];
              const name = stations.find((station) => station.id === id)?.name ?? id;
              return (
                <button
                  key={id}
                  ref={(element) => { markerElements.current[id] = element; }}
                  className={`twin-pin ${selection?.type === "station" && selection.id === id ? "selected" : ""}`}
                  aria-pressed={selection?.type === "station" && selection.id === id}
                  aria-label={status && !offline ? `${name}: ${statusLabels[status]}` : name}
                  onClick={() => select({ type: "station", id })}
                >
                  <i className={`dot ${status ? `dot-${status}` : ""}`} aria-hidden="true" />
                  <span>
                    {name}
                    {status && !offline && <small> {statusLabels[status]}</small>}
                  </span>
                </button>
              );
            })}
            {devices.filter((device) => device.kind === "level").map((device) => {
              const selected = selection?.type === "device" && selection.id === device.id;
              return (
                <button
                  key={device.id}
                  ref={(element) => { deviceElements.current[device.id] = element; }}
                  className={`twin-pin twin-pin-device ${selected ? "selected" : ""} ${isLow(device) ? "low" : ""}`}
                  aria-pressed={selected}
                  aria-label={`${device.name}: ${reading(device)}, simulated`}
                  onClick={() => select({ type: "device", id: device.id })}
                >
                  {reading(device)}
                </button>
              );
            })}
          </div>
        )}
        <div className="twin-tip" ref={tip} hidden />
        <nav className="views twin-views" aria-label="Camera view">
          <button aria-pressed={view === "perspective"} onClick={() => changeView("perspective")}>3D</button>
          <button aria-pressed={view === "plan"} onClick={() => changeView("plan")}>Floor plan</button>
        </nav>
        {placing && (
          <div className="new-request twin-placing" role="status">
            <span>Click the floor to place it</span>
            <button className="secondary" onClick={() => setPlacing(false)}>Cancel</button>
          </div>
        )}
        {!placing && !following.current && latestUpdate && (
          <button
            key={`${latestUpdate.id}-${latestUpdate.status}`}
            className="twin-update"
            onClick={() => {
              const thing = thingForRequest(latestUpdate);
              select(thing ? { type: "thing", id: thing.id } : { type: "station", id: latestUpdate.station_id });
            }}
          >
            <i className={`dot dot-${latestUpdate.status}`} aria-hidden="true" />
            <span>
              <strong>{thingForRequest(latestUpdate)?.name ?? stations.find((s) => s.id === latestUpdate.station_id)?.name}</strong>
              <small>{loopLabel(latestUpdate)}</small>
            </span>
          </button>
        )}
      </div>
      <TwinInspector
        selection={selection}
        stations={stations}
        requests={requests}
        devices={devices}
        offline={offline}
        disabled={disabled}
        placing={placing}
        feedback={feedback}
        showStations={!!renderError}
        onSelect={select}
        onAdd={addDevice}
        onSaveDevice={(device) => setDevices((current) => current.map((item) => (item.id === device.id ? device : item)))}
        onPlace={() => {
          setPlacing(true);
          changeView("plan");
        }}
        onCancelPlace={() => setPlacing(false)}
        onRemoveDevice={(id) => {
          setDevices((current) => current.filter((item) => item.id !== id));
          select(null);
        }}
        onSimulatePress={onSimulatePress}
        onSimulateReport={onSimulateReport}
        onAsk={onAsk}
        onRequest={onRequest}
      />
    </section>
  );
}
