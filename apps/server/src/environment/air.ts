import type { FastifyInstance } from "fastify";

/**
 * Indoor-climate telemetry and a deterministic air-quality assessment.
 * The bridge forwards sensor readings (POST /api/telemetry); speech/dashboard ask
 * GET /api/air-quality. In-memory only: latest value per metric, no history.
 * CO2 is optional — the current kit has no SCD40, so CO2 is null until one is added.
 */
export type Metric = "temperature_c" | "humidity_pct" | "co2_ppm";
export type Reading = { value: number; received_at: string };
export type Level = "good" | "moderate" | "poor" | "unknown";
export type AirQuality = {
  level: Level;
  readings: Record<Metric, Reading | null>;
  issues: string[];
  summary: string;
  stale: boolean;
};

const STALE_MS = 2 * 60 * 1000;

/** Map firmware sensor keys (atech-serial-events contract) to metrics. */
export function metricFor(key: string): Metric | null {
  const k = key.toLowerCase();
  if (k === "th_temperature" || k === "temperature_c") return "temperature_c";
  if (k === "th_humidity" || k === "humidity_pct") return "humidity_pct";
  if (k.includes("co2")) return "co2_ppm";
  return null;
}

export class Telemetry {
  private latest = new Map<Metric, Reading>();
  record(key: string, value: number, at = new Date()): Metric | null {
    const metric = metricFor(key);
    if (!metric || !Number.isFinite(value)) return null;
    this.latest.set(metric, { value, received_at: at.toISOString() });
    return metric;
  }
  get(metric: Metric): Reading | null {
    return this.latest.get(metric) ?? null;
  }
}

const rank: Record<Level, number> = { unknown: -1, good: 0, moderate: 1, poor: 2 };
const worst = (a: Level, b: Level) => (rank[b] > rank[a] ? b : a);

/** Thresholds: office comfort ~19–24 °C, humidity 25–60 %, CO2 <1000 good / >1500 poor. */
export function assess(t: Telemetry, now = new Date()): AirQuality {
  const readings = {
    temperature_c: t.get("temperature_c"),
    humidity_pct: t.get("humidity_pct"),
    co2_ppm: t.get("co2_ppm"),
  };
  const present = Object.values(readings).filter((r): r is Reading => r !== null);
  if (present.length === 0)
    return { level: "unknown", readings, issues: [], stale: true,
      summary: "No sensor readings yet. Is the device bridge running?" };
  const stale = present.some((r) => now.getTime() - Date.parse(r.received_at) > STALE_MS);
  let level: Level = "good";
  const issues: string[] = [];
  const temp = readings.temperature_c?.value;
  if (temp !== undefined) {
    if (temp > 26 || temp < 17) { level = worst(level, "poor"); issues.push(temp > 26 ? `too warm (${temp.toFixed(1)} °C)` : `too cold (${temp.toFixed(1)} °C)`); }
    else if (temp > 24 || temp < 19) { level = worst(level, "moderate"); issues.push(temp > 24 ? `a bit warm (${temp.toFixed(1)} °C)` : `a bit cool (${temp.toFixed(1)} °C)`); }
  }
  const hum = readings.humidity_pct?.value;
  if (hum !== undefined) {
    if (hum > 70 || hum < 15) { level = worst(level, "poor"); issues.push(hum > 70 ? `very humid (${hum.toFixed(0)} %)` : `very dry (${hum.toFixed(0)} %)`); }
    else if (hum > 60 || hum < 25) { level = worst(level, "moderate"); issues.push(hum > 60 ? `humid (${hum.toFixed(0)} %)` : `dry (${hum.toFixed(0)} %)`); }
  }
  const co2 = readings.co2_ppm?.value;
  if (co2 !== undefined) {
    if (co2 > 1500) { level = worst(level, "poor"); issues.push(`stuffy, CO2 ${co2.toFixed(0)} ppm – open a window`); }
    else if (co2 > 1000) { level = worst(level, "moderate"); issues.push(`CO2 rising (${co2.toFixed(0)} ppm) – consider airing`); }
  }
  const parts = [
    temp !== undefined ? `${temp.toFixed(1)} °C` : null,
    hum !== undefined ? `${hum.toFixed(0)} % humidity` : null,
    co2 !== undefined ? `CO2 ${co2.toFixed(0)} ppm` : "no CO2 sensor",
  ].filter(Boolean).join(", ");
  const verdict = level === "good" ? "Air quality is good" : level === "moderate" ? "Air quality is OK but not ideal" : "Air quality is poor";
  const summary = `${verdict}: ${parts}.${issues.length ? " " + issues.join("; ") + "." : ""}${stale ? " (readings are over 2 minutes old)" : ""}`;
  return { level, readings, issues, summary, stale };
}

export function registerEnvironment(app: FastifyInstance, telemetry = new Telemetry()) {
  app.post("/api/telemetry", async (req, reply) => {
    const body = req.body as { key?: unknown; value?: unknown } | null;
    if (!body || typeof body.key !== "string" || body.key.length > 64 || typeof body.value !== "number")
      return reply.code(400).send({ error: "Expected { key: string, value: number }" });
    const metric = telemetry.record(body.key, body.value);
    return { accepted: metric !== null, metric };
  });
  app.get("/api/air-quality", () => assess(telemetry));
  return telemetry;
}
