import * as THREE from "three";
import type { TwinDevice } from "./twinDevices";

/** Device markers are deliberately enlarged so their physical locations remain legible. */
export function createDeviceMesh(device: TwinDevice, selected: boolean, statusColor: number | null) {
  const group = new THREE.Group();
  group.position.set(device.position.x, 0.17, device.position.z);
  group.userData.deviceId = device.id;
  const low = device.kind === "level" && device.tracking.value <= device.tracking.warningBelow;
  const color = low ? 0xff6b66 : statusColor ?? 0xc4d2ff;
  const white = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.5 });
  const dark = new THREE.MeshStandardMaterial({ color: 0x15130f, roughness: 0.55 });
  const accent = new THREE.MeshStandardMaterial({ color, roughness: 0.35, emissive: color, emissiveIntensity: selected ? 0.32 : 0.08 });
  function box(w: number, h: number, d: number, y: number, material: THREE.Material) {
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), material);
    mesh.position.y = y;
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    group.add(mesh);
    return mesh;
  }
  if (device.kind === "button") {
    // A small demo plinth keeps the button visible above the stage edge.
    box(0.65, 0.68, 0.55, 0.34, dark);
    box(0.86, 0.12, 0.72, 0.74, white);
    const bezel = new THREE.Mesh(new THREE.CylinderGeometry(0.3, 0.3, 0.06, 32), dark);
    bezel.position.y = 0.83;
    group.add(bezel);
    const button = new THREE.Mesh(new THREE.CylinderGeometry(0.25, 0.27, 0.13, 32), accent);
    button.position.y = 0.9;
    button.castShadow = true;
    group.add(button);
  } else {
    box(0.63, 0.9, 0.42, 0.45, white);
    const face = box(0.47, 0.6, 0.03, 0.51, dark);
    face.position.z = 0.23;
    const ratio = (device.tracking.value - device.tracking.min) / (device.tracking.max - device.tracking.min);
    const height = Math.max(0.025, ratio * 0.5);
    const fill = box(0.33, height, 0.04, 0.25 + height / 2, accent);
    fill.position.z = 0.25;
  }
  if (selected) {
    const ring = new THREE.Mesh(new THREE.RingGeometry(0.67, 0.76, 48), new THREE.MeshBasicMaterial({ color: 0x15130f, side: THREE.DoubleSide, depthTest: false }));
    ring.rotation.x = -Math.PI / 2;
    ring.position.y = 0.015;
    ring.renderOrder = 3;
    group.add(ring);
  }
  return group;
}
export function disposeDeviceMeshes(group: THREE.Group) {
  const materials = new Set<THREE.Material>();
  group.traverse((object) => {
    if (object instanceof THREE.Mesh) {
      object.geometry.dispose();
      for (const material of Array.isArray(object.material) ? object.material : [object.material]) materials.add(material);
    }
  });
  materials.forEach((material) => material.dispose());
  group.clear();
}
export function isOfficeFloor(x: number, z: number) {
  return Number.isFinite(x) && Number.isFinite(z) && ((x >= -9.7 && x <= 0.85 && z >= -5.9 && z <= 6.95) || (x >= 1.45 && x <= 9.4 && z >= -6.8 && z <= 6.95));
}
