import * as THREE from "three";

/** Photo-informed architectural model. Dimensions and hidden WC interiors are illustrative. */
export function createOfficeScene(): {
  group: THREE.Group;
  hotspots: Record<"kitchen" | "bathroom" | "printer", THREE.Vector3>;
  things: Map<string, THREE.Mesh[]>;
} {
  const group = new THREE.Group();
  group.name = "Founders Hub · approximate architectural cutaway";
  const materials = {
    slab: new THREE.MeshStandardMaterial({ color: "#a5a096", roughness: 0.93 }),
    floor: new THREE.MeshStandardMaterial({ color: "#bcb7ab", roughness: 0.88 }),
    wall: new THREE.MeshStandardMaterial({ color: "#e4decb", roughness: 0.95 }),
    trim: new THREE.MeshStandardMaterial({ color: "#f7efdc", roughness: 0.8 }),
    concrete: new THREE.MeshStandardMaterial({ color: "#c3bfb2", roughness: 0.98 }),
    oak: new THREE.MeshStandardMaterial({ color: "#ba8550", roughness: 0.72 }),
    oakLight: new THREE.MeshStandardMaterial({ color: "#d4ad72", roughness: 0.7 }),
    oakDark: new THREE.MeshStandardMaterial({ color: "#91653e", roughness: 0.8 }),
    burgundy: new THREE.MeshStandardMaterial({ color: "#862e45", roughness: 0.95 }),
    dark: new THREE.MeshStandardMaterial({ color: "#242a2b", roughness: 0.76 }),
    black: new THREE.MeshStandardMaterial({ color: "#131b20", roughness: 0.58 }),
    steel: new THREE.MeshStandardMaterial({ color: "#adb7b9", roughness: 0.38, metalness: 0.68 }),
    white: new THREE.MeshStandardMaterial({ color: "#eeeadd", roughness: 0.5 }),
    fabric: new THREE.MeshStandardMaterial({ color: "#d2c9ad", roughness: 1 }),
    rug: new THREE.MeshStandardMaterial({ color: "#8c8c7c", roughness: 1 }),
    leaf: new THREE.MeshStandardMaterial({ color: "#426c49", roughness: 0.9 }),
    leafLight: new THREE.MeshStandardMaterial({ color: "#678850", roughness: 0.9 }),
    pot: new THREE.MeshStandardMaterial({ color: "#c2ac8b", roughness: 0.95 }),
    blue: new THREE.MeshStandardMaterial({ color: "#84b4bd", roughness: 0.42 }),
    screen: new THREE.MeshStandardMaterial({ color: "#506c7a", roughness: 0.4, emissive: "#355565", emissiveIntensity: 0.24 }),
    glass: new THREE.MeshStandardMaterial({ color: "#a4c9d0", transparent: true, opacity: 0.28, roughness: 0.18, side: THREE.DoubleSide, depthWrite: false }),
    glow: new THREE.MeshStandardMaterial({ color: "#fff3d6", emissive: "#ffd690", emissiveIntensity: 0.6, roughness: 0.5 }),
    curtain: new THREE.MeshStandardMaterial({ color: "#bcb099", roughness: 1 }),
  };
  type Material = keyof typeof materials;
  const boxGeometry = new THREE.BoxGeometry(1, 1, 1);
  const cylinderGeometry = new THREE.CylinderGeometry(1, 1, 1, 16);
  const sphereGeometry = new THREE.SphereGeometry(1, 12, 8);
  function box(x: number, y: number, z: number, w: number, h: number, d: number, material: Material, parent: THREE.Object3D = group) {
    const mesh = new THREE.Mesh(boxGeometry, materials[material]);
    mesh.position.set(x, y, z);
    mesh.scale.set(w, h, d);
    mesh.castShadow = material !== "glass";
    mesh.receiveShadow = true;
    parent.add(mesh);
    return mesh;
  }
  function cylinder(x: number, y: number, z: number, radius: number, h: number, material: Material, parent: THREE.Object3D = group) {
    const mesh = new THREE.Mesh(cylinderGeometry, materials[material]);
    mesh.position.set(x, y, z);
    mesh.scale.set(radius, h, radius);
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    parent.add(mesh);
    return mesh;
  }
  function sphere(x: number, y: number, z: number, radius: number, material: Material, parent: THREE.Object3D = group) {
    const mesh = new THREE.Mesh(sphereGeometry, materials[material]);
    mesh.position.set(x, y, z);
    mesh.scale.setScalar(radius);
    mesh.castShadow = true;
    parent.add(mesh);
    return mesh;
  }
  function furniture(x: number, z: number, rotation = 0) {
    const item = new THREE.Group();
    item.position.set(x, 0, z);
    item.rotation.y = rotation;
    group.add(item);
    return item;
  }
  function chair(x: number, z: number, rotation = 0, cafe = false) {
    const item = furniture(x, z, rotation);
    const upholstery: Material = cafe ? "dark" : "burgundy";
    box(0, 0.48, 0, 0.52, 0.12, 0.52, upholstery, item);
    const back = box(0, 0.85, 0.23, 0.54, 0.63, 0.085, upholstery, item);
    back.rotation.x = -0.08;
    for (const xx of [-0.21, 0.21]) {
      for (const zz of [-0.2, 0.2]) cylinder(xx, 0.23, zz, 0.018, 0.46, cafe ? "black" : "steel", item);
    }
  }
  function cup(x: number, y: number, z: number, parent: THREE.Object3D = group) {
    cylinder(x, y + 0.07, z, 0.065, 0.14, "white", parent);
    cylinder(x, y + 0.144, z, 0.047, 0.004, "oakDark", parent);
  }
  function laptop(x: number, z: number, rotation = 0) {
    const item = furniture(x, z, rotation);
    box(0, 0.87, 0, 0.48, 0.025, 0.34, "steel", item);
    const screen = box(0, 1.04, -0.16, 0.46, 0.32, 0.025, "dark", item);
    screen.rotation.x = -0.16;
    const display = box(0, 1.04, -0.14, 0.41, 0.27, 0.008, "screen", item);
    display.rotation.x = -0.16;
  }
  function plant(x: number, z: number, size = 1) {
    const item = furniture(x, z);
    cylinder(0, 0.25 * size, 0, 0.28 * size, 0.5 * size, "pot", item);
    cylinder(0, 0.52 * size, 0, 0.23 * size, 0.025, "oakDark", item);
    cylinder(0, 1.1 * size, 0, 0.025 * size, 1.2 * size, "oakDark", item);
    for (let index = 0; index < 10; index++) {
      const angle = index * 2.4;
      const leaf = sphere(Math.cos(angle) * 0.29 * size, (0.92 + index * 0.078) * size, Math.sin(angle) * 0.29 * size, 0.2 * size, index % 2 ? "leaf" : "leafLight", item);
      leaf.scale.set(0.17 * size, 0.1 * size, 0.46 * size);
      leaf.rotation.set(0.3, -angle, 0.4);
    }
  }
  function table(x: number, z: number) {
    box(x, 0.82, z, 2.2, 0.11, 1.15, "oakLight");
    for (const xx of [-0.93, 0.93]) for (const zz of [-0.44, 0.44]) box(x + xx, 0.4, z + zz, 0.055, 0.78, 0.055, "dark");
    chair(x - 0.5, z + 0.95);
    chair(x + 0.5, z + 0.95);
    chair(x - 0.5, z - 0.95, Math.PI);
    chair(x + 0.5, z - 0.95, Math.PI);
    laptop(x - 0.45, z + 0.2);
    laptop(x + 0.4, z - 0.23, Math.PI);
    cup(x + 0.65, 0.88, z + 0.3);
  }
  function roundTable(x: number, z: number) {
    cylinder(x, 0.8, z, 0.85, 0.09, "oakLight");
    cylinder(x, 0.4, z, 0.07, 0.8, "dark");
    cylinder(x, 0.04, z, 0.4, 0.06, "dark");
    for (let index = 0; index < 3; index++) {
      const angle = index * Math.PI * 2 / 3;
      chair(x + Math.sin(angle) * 1.18, z + Math.cos(angle) * 1.18, angle, true);
    }
    cup(x + 0.32, 0.845, z + 0.16);
    cup(x - 0.3, 0.845, z - 0.12);
    cylinder(x, 0.93, z, 0.085, 0.16, "pot");
    sphere(x, 1.06, z, 0.12, "leaf");
  }

  // Everything built inside a section belongs to one clickable thing in the twin.
  const things = new Map<string, THREE.Mesh[]>();
  function thing(id: string, build: () => void) {
    const start = group.children.length;
    build();
    const meshes = things.get(id) ?? [];
    for (const child of group.children.slice(start)) {
      child.traverse((object) => {
        if (!(object instanceof THREE.Mesh)) return;
        object.userData.thingId = id;
        meshes.push(object);
      });
    }
    things.set(id, meshes);
  }

  // Two connected public rooms, with a narrow rear cloakroom and WC extension.
  box(-4.5, -0.22, 0.5, 11.4, 0.44, 13.5, "slab");
  box(5.5, -0.22, 2.1, 8.6, 0.44, 10.3, "slab");
  box(5.45, -0.22, -5.05, 8.7, 0.44, 4.3, "slab");
  box(-4.5, 0.012, 0.5, 11.15, 0.025, 13.2, "floor");
  box(5.45, 0.015, 2.1, 8.4, 0.03, 10.15, "floor");
  box(5.45, 0.015, -5.05, 8.4, 0.03, 4.05, "floor");
  // Quiet expansion joints add scale without creating visual noise.
  for (const z of [-4, -1, 2, 5]) box(-4.5, 0.032, z, 11, 0.006, 0.012, "slab");
  for (const x of [-7.5, -4.5, -1.5]) box(x, 0.032, 0.5, 0.012, 0.006, 13, "slab");
  // Tall rear walls; front walls are deliberately cut low.
  box(-4.5, 1.35, -6.15, 11.4, 2.7, 0.18, "wall");
  box(5.4, 1.35, -7.1, 8.9, 2.7, 0.18, "wall");
  box(-10.1, 0.28, 0.5, 0.18, 0.56, 13.5, "wall");
  box(-4.5, 0.16, 7.15, 11.4, 0.32, 0.18, "wall");
  box(9.75, 0.28, 2.0, 0.18, 0.56, 10.4, "wall");
  box(5.4, 0.16, 7.15, 8.7, 0.32, 0.18, "wall");
  // Long window wall mirrors the main hall and the café's daylight frontage.
  for (const z of [-4.9, -2.5, -0.1, 2.3, 4.7]) {
    box(-10.06, 1.6, z, 0.045, 2.0, 2.15, "glass");
    box(-10.03, 0.61, z, 0.23, 0.08, 2.23, "trim");
    box(-10.03, 2.63, z, 0.12, 0.07, 2.24, "dark");
    for (const dz of [-1.1, 1.1]) box(-10.03, 1.61, z + dz, 0.1, 2.13, 0.07, "trim");
  }
  for (const z of [-0.8, 1.6, 4.0, 6.15]) {
    box(9.72, 1.6, z, 0.045, 2, 2.06, "glass");
    for (const dz of [-1.07, 1.07]) box(9.7, 1.6, z + dz, 0.09, 2.1, 0.06, "trim");
    box(9.7, 0.6, z, 0.2, 0.07, 2.1, "trim");
  }
  // Rear curtain has shallow folds instead of a flat rectangle.
  for (let index = 0; index < 28; index++) {
    cylinder(-9.6 + index * 0.24, 1.36, -6.0, 0.13, 2.65, "curtain");
  }
  box(-4.6, 2.71, -5.8, 10.6, 0.12, 0.13, "dark");
  for (const [x, z] of [[-6.8, 0.2], [-0.25, 1.2], [7.3, -1.7]]) {
    cylinder(x, 1.48, z, 0.28, 2.96, "concrete");
    cylinder(x, 0.045, z, 0.3, 0.09, "slab");
  }
  // Rear exposed structure is retained, while the ceiling is removed for the cutaway.
  const duct = cylinder(-4.6, 3.05, -5.35, 0.2, 10.8, "dark");
  duct.rotation.z = Math.PI / 2;
  for (const x of [-8.8, -5.2, -1.6]) {
    cylinder(x, 2.9, -5.35, 0.23, 0.22, "dark");
    box(x, 3.18, -4.1, 0.045, 0.045, 2.6, "dark");
    box(x, 2.87, -3.85, 1.6, 0.055, 0.11, "glow");
  }
  // Event hall: six rows of burgundy chairs with a central circulation aisle.
  thing("event-hall", () => {
    for (let row = 0; row < 5; row++) {
      for (const x of [-8.7, -7.85, -7.0, -5.55, -4.7, -3.85]) chair(x, 1.5 + row * 1.03, Math.PI);
    }
  });
  // The worktables visible behind the audience seating.
  thing("workspace", () => {
    table(-7.7, -3.25);
    table(-3.8, -3.25);
    table(-2.4, -0.2);
  });
  // Small presentation wall and low stage at the front of the main hall.
  thing("event-hall", () => {
    box(-5.65, 0.07, 6.7, 5.7, 0.12, 0.55, "dark");
  });
  // Glass divider and open passage between main hall and café.
  box(1.15, 1.2, 4.9, 0.045, 2.4, 3.8, "glass");
  for (const z of [3, 6.8]) box(1.15, 1.22, z, 0.055, 2.44, 0.055, "dark");
  box(1.15, 2.43, 4.9, 0.055, 0.055, 3.85, "dark");
  // Café: four sociable tables, a soft perimeter bench and hanging globe lighting.
  thing("cafe", () => {
    box(5.4, 0.045, 3.7, 7.7, 0.025, 6.3, "rug");
    roundTable(3.25, 1.0);
    roundTable(7.05, 1.0);
    roundTable(3.25, 4.8);
    roundTable(7.05, 4.8);
    box(5.5, 0.42, 6.55, 6.2, 0.7, 0.75, "oakDark");
    box(5.5, 0.83, 6.8, 6.2, 0.85, 0.2, "fabric");
    for (const x of [2.85, 3.9, 4.95, 6, 7.05, 8.1]) box(x, 0.82, 6.45, 1, 0.16, 0.65, "fabric");
    for (const [x, z] of [[3.25, 1.0], [7.05, 1.0], [3.25, 4.8], [7.05, 4.8]]) {
      cylinder(x, 2.7, z, 0.015, 0.7, "dark");
      sphere(x, 2.33, z, 0.19, "glow");
    }
  });
  plant(1.8, 6.4, 1.2);
  plant(8.85, 6.2, 1.1);
  plant(8.85, -0.65, 0.9);
  plant(-9.3, 6.5, 0.85);

  // Oak service counter, shelf wall, coffee machine and sink from the reference.
  thing("counter", () => {
    box(6.0, 1.38, -2.6, 7.2, 2.75, 0.18, "wall");
    box(5.85, 0.55, -2.05, 6.4, 1.1, 0.92, "oak");
    box(5.85, 1.13, -2.04, 6.6, 0.12, 1.03, "oakDark");
    for (const x of [3.2, 4.45, 5.7, 7, 8.35]) {
      box(x, 0.56, -1.565, 0.025, 0.94, 0.018, "oakDark");
      box(x + 0.12, 0.73, -1.54, 0.025, 0.2, 0.04, "dark");
    }
    for (const x of [3.7, 6.5, 8.5]) {
      box(x, 1.98, -2.4, 0.075, 1.6, 0.42, "oak");
    }
    for (const y of [1.53, 2.15, 2.74]) box(7.5, y, -2.38, 2.1, 0.065, 0.43, "oakLight");
    for (const y of [1.57, 2.19]) for (const x of [6.85, 7.15, 7.45, 7.75, 8.05]) {
      cup(x, y, -2.3);
      if (y > 2) cup(x, y + 0.14, -2.3);
    }
  });
  thing("coffee-machine", () => {
    box(5.25, 1.7, -1.98, 1.05, 1.05, 0.69, "steel");
    box(5.25, 1.76, -1.615, 0.86, 0.83, 0.05, "black");
    box(5.25, 1.94, -1.579, 0.34, 0.2, 0.018, "screen");
    for (const y of [1.77, 1.65, 1.53]) sphere(5.5, y, -1.575, 0.027, "white");
    box(5.14, 1.49, -1.52, 0.22, 0.08, 0.16, "steel");
    box(5.25, 1.21, -1.52, 0.94, 0.07, 0.5, "dark");
    cup(5.15, 1.25, -1.42);
    cylinder(5.0, 2.26, -1.98, 0.23, 0.16, "dark");
    cylinder(5.5, 2.26, -1.98, 0.23, 0.16, "dark");
  });
  thing("counter", () => {
    box(7.44, 1.202, -1.92, 0.92, 0.04, 0.62, "steel");
    box(7.44, 1.225, -1.92, 0.73, 0.018, 0.46, "dark");
    cylinder(7.65, 1.44, -2.15, 0.025, 0.45, "steel");
    const spout = cylinder(7.65, 1.66, -2.01, 0.025, 0.28, "steel");
    spout.rotation.x = Math.PI / 2;
  });
  // Small drinks fridge at the end of the counter.
  thing("fridge", () => {
    box(2.1, 0.65, -2.1, 0.8, 1.3, 0.8, "dark");
    box(2.1, 0.7, -1.69, 0.67, 1.04, 0.03, "glass");
    for (const y of [0.35, 0.7, 1.05]) {
      box(2.1, y - 0.09, -2.05, 0.68, 0.025, 0.6, "steel");
      for (const x of [1.9, 2.1, 2.3]) cylinder(x, y, -1.93, 0.056, 0.18, "burgundy");
    }
  });
  // Cloakroom and two inferred WC cubicles. Open doors keep the circulation legible.
  thing("bathroom", () => {
    box(4.55, 0.62, -5.1, 0.15, 1.24, 3.9, "wall");
    box(7.1, 0.62, -5.7, 0.12, 1.24, 2.7, "wall");
    box(9.7, 0.62, -5.1, 0.16, 1.24, 3.9, "wall");
    box(7.1, 0.62, -3.15, 5.3, 1.24, 0.15, "wall");
    for (const x of [5.75, 8.45]) {
      box(x, 0.041, -5.2, 2.4, 0.05, 3.55, "trim");
      // Cistern, bowl, seat and compact wall basin.
      box(x, 0.72, -6.65, 0.55, 0.65, 0.23, "white");
      const bowl = sphere(x, 0.36, -6.15, 0.34, "white");
      bowl.scale.set(0.29, 0.26, 0.43);
      cylinder(x, 0.15, -6.25, 0.16, 0.3, "white");
      const seat = cylinder(x, 0.56, -6.1, 0.29, 0.055, "white");
      seat.scale.z = 0.38;
      const opening = cylinder(x, 0.591, -6.1, 0.18, 0.008, "dark");
      opening.scale.z = 0.26;
      box(x + 0.65, 0.83, -4.45, 0.6, 0.15, 0.46, "white");
      cylinder(x + 0.65, 1.02, -4.56, 0.022, 0.24, "steel");
      box(x + 0.65, 1.45, -4.7, 0.58, 0.65, 0.035, "steel");
      const door = furniture(x - 0.7, -3.2, -Math.PI / 3);
      box(0, 0.57, -0.46, 0.06, 1.14, 0.92, "dark", door);
      sphere(0.065, 0.63, -0.75, 0.035, "steel", door);
    }
  });
  // Coat rails with a few anonymous hanging garments.
  thing("cloakroom", () => {
    for (const z of [-5.65, -3.85]) {
      for (const x of [1.6, 3.6]) {
        cylinder(x, 1.05, z, 0.022, 2.1, "steel");
        box(x, 0.06, z, 0.32, 0.06, 0.5, "steel");
      }
      const rail = cylinder(2.6, 2.1, z, 0.026, 2.05, "steel");
      rail.rotation.z = Math.PI / 2;
      for (let index = 0; index < 7; index++) {
        const x = 1.85 + index * 0.23;
        cylinder(x, 1.99, z, 0.012, 0.18, "steel");
        box(x, 1.47, z, 0.16, 0.94, 0.38, index % 3 ? "dark" : "black");
        sphere(x, 1.93, z, 0.12, "dark");
      }
    }
  });
  // Printer is a deliberately illustrative placement in the connecting passage.
  thing("printer", () => {
    box(0.55, 0.39, -2.4, 0.9, 0.78, 0.75, "oak");
    box(0.55, 0.99, -2.4, 0.83, 0.43, 0.67, "white");
    box(0.55, 1.23, -2.47, 0.85, 0.08, 0.7, "dark");
    box(0.55, 1.28, -2.49, 0.65, 0.025, 0.48, "white");
    box(0.55, 0.95, -2.05, 0.58, 0.09, 0.03, "dark");
    box(0.77, 1.15, -2.04, 0.18, 0.1, 0.025, "screen");
  });
  plant(-0.5, -5.3, 0.9);

  return {
    group,
    things,
    hotspots: {
      kitchen: new THREE.Vector3(5.25, 2.9, -1.85),
      bathroom: new THREE.Vector3(7.15, 2.0, -5.35),
      printer: new THREE.Vector3(0.55, 1.9, -2.35),
    },
  };
}
