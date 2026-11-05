import test from "node:test";
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { buildApp as createApp } from "./app.js";
import { mockInterpret } from "./agent/interpret.js";
// Tests must never inherit live API execution from a developer's .env.
const buildApp = (options: Parameters<typeof createApp>[0] = {}) =>
  createApp({ ...options, interpreter: mockInterpret });
import { proposalFingerprint, type EventResponse } from "@office/contracts";
const event = (station = "kitchen", text = "We need coffee") => ({
  event_id: randomUUID(),
  station_id: station,
  type: "speech_transcript",
  timestamp: new Date().toISOString(),
  payload: { text },
});
test("same words use station context; kitchen paper needs clarification", async () => {
  const app = buildApp({ databasePath: ":memory:" });
  try {
    for (const [station, item] of [
      ["bathroom", "toilet-paper"],
      ["printer", "printer-paper"],
      ["kitchen", null],
    ] as const) {
      const res = await app.inject({
        method: "POST",
        url: "/api/device-events",
        payload: event(station, "We need paper"),
      });
      assert.equal(res.statusCode, 200);
      const body = res.json<EventResponse>();
      assert.equal(body.request.proposal?.product.id ?? null, item);
    }
  } finally {
    await app.close();
  }
});
test("event retries and equivalent requests deduplicate; approvals execute exactly once", async () => {
  let calls = 0;
  const app = buildApp({
    databasePath: ":memory:",
    supplier: {
      placeOrder() {
        calls++;
        return { reference: "SIM-test", simulation: true };
      },
    },
  });
  try {
    const input = event();
    const submit = () =>
      app.inject({ method: "POST", url: "/api/device-events", payload: input });
    const results = await Promise.all([submit(), submit()]);
    assert.equal(results[0].json().request.id, results[1].json().request.id);
    const first = results[0].json<EventResponse>().request;
    assert.ok(first.proposal);
    const equivalent = await app.inject({
      method: "POST",
      url: "/api/device-events",
      payload: event(),
    });
    assert.equal(equivalent.json().request.id, first.id);
    assert.equal(equivalent.json().duplicate, true);
    const wrong = await app.inject({
      method: "POST",
      url: `/api/requests/${first.id}/approve`,
      payload: { proposal_fingerprint: "different" },
    });
    assert.equal(wrong.statusCode, 409);
    assert.equal(calls, 0);
    const approve = () =>
      app.inject({
        method: "POST",
        url: `/api/requests/${first.id}/approve`,
        payload: { proposal_fingerprint: proposalFingerprint(first.proposal!) },
      });
    const approvals = await Promise.all([approve(), approve()]);
    assert.ok(approvals.every((r) => r.statusCode === 200));
    assert.equal(calls, 1);
    assert.equal(approvals[0].json().status, "ordered");
    const command = await app.inject({ url: "/api/devices/kitchen/command" });
    assert.equal(command.json().command.color, "green");
    await app.inject({
      method: "POST",
      url: `/api/requests/${first.id}/resolve`,
      payload: {},
    });
    const fresh = await app.inject({
      method: "POST",
      url: "/api/device-events",
      payload: event(),
    });
    assert.notEqual(fresh.json().request.id, first.id);
  } finally {
    await app.close();
  }
});
test("maintenance routes to responsible team and can resolve", async () => {
  const app = buildApp({ databasePath: ":memory:" });
  try {
    const res = await app.inject({
      method: "POST",
      url: "/api/device-events",
      payload: event("bathroom", "The sink is leaking"),
    });
    const r = res.json().request;
    assert.equal(r.status, "maintenance_open");
    assert.equal(r.assigned_to, "Cleaning team");
    assert.equal(r.proposal, null);
    const approved = await app.inject({
      method: "POST",
      url: `/api/requests/${r.id}/approve`,
      payload: { proposal_fingerprint: "anything" },
    });
    assert.equal(approved.statusCode, 409);
    const resolved = await app.inject({
      method: "POST",
      url: `/api/requests/${r.id}/resolve`,
      payload: {},
    });
    assert.equal(resolved.json().status, "resolved");
  } finally {
    await app.close();
  }
});
test("invalid input, unsupported sensor items and spending cap cannot place orders", async () => {
  const app = buildApp({ databasePath: ":memory:" });
  try {
    const invalid = await app.inject({
      method: "POST",
      url: "/api/device-events",
      payload: {},
    });
    assert.equal(invalid.statusCode, 400);
    const cap = await app.inject({
      method: "POST",
      url: "/api/device-events",
      payload: event("kitchen", "20 coffee"),
    });
    assert.equal(cap.json().request.status, "needs_clarification");
    assert.equal(cap.json().request.proposal, null);
    const sensor = await app.inject({
      method: "POST",
      url: "/api/device-events",
      payload: {
        ...event(),
        type: "stock_low",
        payload: { item_id: "toner", level: 5 },
      },
    });
    assert.equal(sensor.json().request.status, "needs_clarification");
    const missingText = await app.inject({
      method: "POST",
      url: "/api/device-events",
      payload: { ...event(), payload: {} },
    });
    assert.equal(missingText.statusCode, 400);
  } finally {
    await app.close();
  }
});
test("uncertain supplier outcome persists and cannot be retried or edited", async () => {
  let calls = 0;
  const app = buildApp({
    databasePath: ":memory:",
    supplier: {
      placeOrder() {
        calls++;
        throw new Error("timeout");
      },
    },
  });
  try {
    const res = await app.inject({
      method: "POST",
      url: "/api/device-events",
      payload: event(),
    });
    const r = res.json().request;
    const approve = () =>
      app.inject({
        method: "POST",
        url: `/api/requests/${r.id}/approve`,
        payload: { proposal_fingerprint: proposalFingerprint(r.proposal) },
      });
    assert.equal((await approve()).statusCode, 409);
    assert.equal((await approve()).statusCode, 409);
    assert.equal(calls, 1);
    const edit = await app.inject({
      method: "POST",
      url: `/api/requests/${r.id}/transcript`,
      payload: { text: "We need cups" },
    });
    assert.equal(edit.statusCode, 409);
  } finally {
    await app.close();
  }
});
test("requests and event dedup survive server restart", async () => {
  const dir = mkdtempSync(join(tmpdir(), "office-test-"));
  const path = join(dir, "db.sqlite");
  const input = event();
  let app = buildApp({ databasePath: path });
  try {
    const first = await app.inject({
      method: "POST",
      url: "/api/device-events",
      payload: input,
    });
    await app.close();
    app = buildApp({ databasePath: path });
    const next = await app.inject({
      method: "POST",
      url: "/api/device-events",
      payload: input,
    });
    assert.equal(next.json().request.id, first.json().request.id);
    assert.equal(next.json().duplicate, true);
  } finally {
    await app.close();
    rmSync(dir, { recursive: true, force: true });
  }
});

test("SSE broadcasts durable request updates and disconnects cleanly", async () => {
  const app = buildApp({ databasePath: ":memory:" });
  const address = await app.listen({ port: 0, host: "127.0.0.1" });
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);
  try {
    const stream = await fetch(address + "/api/events", {
      signal: controller.signal,
    });
    assert.match(
      stream.headers.get("content-type") ?? "",
      /text\/event-stream/,
    );
    const reader = stream.body!.getReader();
    await reader.read();
    const submission = fetch(address + "/api/device-events", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(event()),
    });
    let received = "";
    while (!received.includes("requests.updated")) {
      const chunk = await reader.read();
      if (chunk.done) break;
      received += new TextDecoder().decode(chunk.value);
    }
    assert.match(received, /event: requests.updated/);
    assert.match(received, /request_id/);
    assert.equal((await submission).status, 200);
    await reader.cancel();
  } finally {
    clearTimeout(timeout);
    controller.abort();
    await app.close();
  }
});
