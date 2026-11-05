import test from "node:test";
import assert from "node:assert/strict";
import { defaultTwinDevices, loadTwinDevices, saveTwinDevices } from "../src/twinDevices";

function storage(entries: Record<string, string>) {
  const data = new Map(Object.entries(entries));
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: {
    getItem: (key: string) => data.get(key) ?? null,
    setItem: (key: string, value: string) => data.set(key, value),
  } });
  return data;
}

test("valid v1 devices remain visible and original storage survives future v2 saves", () => {
  const custom = [{ ...defaultTwinDevices[0], name: "Saved local button", position: { x: 1, z: 2 } }];
  const raw = JSON.stringify(custom);
  const data = storage({ "office-twin-devices-v1": raw });
  assert.deepEqual(loadTwinDevices(), custom);
  assert.equal(data.get("office-twin-devices-v2"), undefined);
  assert.equal(saveTwinDevices(custom), true);
  assert.equal(data.get("office-twin-devices-v1"), raw);
  assert.deepEqual(JSON.parse(data.get("office-twin-devices-v2")!), custom);
});

test("an existing v2 configuration, including an intentionally empty layout, wins over v1", () => {
  storage({ "office-twin-devices-v1": JSON.stringify(defaultTwinDevices), "office-twin-devices-v2": "[]" });
  assert.deepEqual(loadTwinDevices(), []);
});

test("invalid legacy devices fall back without changing the saved value", () => {
  const raw = JSON.stringify([{ ...defaultTwinDevices[0], position: { x: 100, z: 0 } }]);
  const data = storage({ "office-twin-devices-v1": raw });
  assert.deepEqual(loadTwinDevices(), defaultTwinDevices);
  assert.equal(data.get("office-twin-devices-v1"), raw);
});

test("malformed v2 is not silently replaced by a legacy configuration", () => {
  const data = storage({ "office-twin-devices-v2": "not json", "office-twin-devices-v1": "[]" });
  assert.deepEqual(loadTwinDevices(), defaultTwinDevices);
  assert.equal(data.get("office-twin-devices-v2"), "not json");
});
