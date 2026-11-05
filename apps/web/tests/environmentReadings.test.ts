import test from "node:test";
import assert from "node:assert/strict";
import type { FacilityReading } from "@office/contracts";
import { environmentReadings } from "../src/environmentTelemetry";
const now = Date.parse("2026-11-05T12:00:00Z");
const sample = (changes: Partial<FacilityReading> = {}): FacilityReading => ({ event_id: "sample", board_id: "kitchen", metric: "temperature_c", value: 22, timestamp: new Date(now).toISOString(), simulation: false, ...changes });
test("all three metrics appear without inventing readings or borrowing another location", () => {
  const readings = environmentReadings([sample()], "bathroom", now);
  assert.equal(readings.length, 3);
  assert.ok(readings.every(r => r.reading === undefined));
});
test("fresh physical readings, zero values and missing metrics are distinguished", () => {
  const readings = environmentReadings([sample({ value: 0 }), sample({ metric: "co2_ppm", value: 650 })], "kitchen", now);
  assert.equal(readings[0].reading?.value, 0);
  assert.equal(readings[0].stale, false);
  assert.equal(readings[1].reading?.value, 650);
  assert.equal(readings[2].reading, undefined);
});
test("physical data wins over simulation and expires without new samples", () => {
  const readings = [sample(), sample({ simulation: true, value: 99, timestamp: new Date(now + 1000).toISOString() })];
  assert.equal(environmentReadings(readings, "kitchen", now + 30000)[0].reading?.value, 22);
  assert.equal(environmentReadings(readings, "kitchen", now + 30000)[0].stale, true);
  assert.equal(environmentReadings([readings[1]], "kitchen", now + 1000)[0].reading?.simulation, true);
});
