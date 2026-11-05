import test from "node:test";
import assert from "node:assert/strict";
import { buildApp } from "./app.js";
import { mockInterpret } from "./agent/interpret.js";
import { Telemetry, assess } from "./environment/air.js";

test("air quality is unknown without readings, then reflects telemetry", async () => {
  const app = buildApp({ databasePath: ":memory:", interpreter: mockInterpret });
  try {
    let res = await app.inject({ method: "GET", url: "/api/air-quality" });
    assert.equal(res.json().level, "unknown");
    for (const [key, value] of [["th_temperature", 22.1], ["th_humidity", 40], ["imu_tilt", 3]] as const) {
      res = await app.inject({ method: "POST", url: "/api/telemetry", payload: { key, value } });
      assert.equal(res.statusCode, 200);
    }
    const aq = (await app.inject({ method: "GET", url: "/api/air-quality" })).json();
    assert.equal(aq.level, "good");
    assert.equal(aq.readings.co2_ppm, null);
    assert.match(aq.summary, /22\.1 °C/);
    res = await app.inject({ method: "POST", url: "/api/telemetry", payload: { key: "x" } });
    assert.equal(res.statusCode, 400);
  } finally {
    await app.close();
  }
});

test("worst metric decides the level", () => {
  const t = new Telemetry();
  t.record("th_temperature", 23);
  t.record("th_humidity", 45);
  t.record("scd40_co2", 1700);
  const aq = assess(t);
  assert.equal(aq.level, "poor");
  assert.ok(aq.issues.some((i) => i.includes("CO2")));
  t.record("scd40_co2", 1200);
  assert.equal(assess(t).level, "moderate");
});
