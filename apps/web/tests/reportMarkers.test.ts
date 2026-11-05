import test from "node:test";
import assert from "node:assert/strict";
import type { OfficeRequest } from "@office/contracts";
import { reportMarkers, thingForRequest } from "../src/twinThings";
const report = (text: string, changes: Partial<OfficeRequest> = {}) => ({ id: text, original_text: text, station_id: "kitchen", intent: "maintenance", status: "maintenance_open", item_id: null, ...changes }) as OfficeRequest;
test("saved reports locate rooms independently of input station and group by room", () => {
  const markers = reportMarkers([report("A worktable is broken"), report("The desks are damaged"), report("Lights broken in the event hall")]);
  assert.equal(markers.find(m => m.thing.id === "workspace")?.reports.length, 2);
  assert.equal(markers.find(m => m.thing.id === "event-hall")?.reports.length, 1);
});
test("chat, drafts, resolved reports and ambiguous locations do not create pins", () => {
  assert.deepEqual(reportMarkers([
    report("Explain the event hall", { intent: "conversation", status: "resolved" }),
    report("Worktable broken", { status: "processing" }),
    report("Worktable broken", { status: "needs_clarification" }),
    report("Worktable repaired", { status: "resolved" }),
    report("Worktables and event hall need repairs"),
  ]), []);
});
test("existing catalogue locations still resolve", () => {
  assert.equal(thingForRequest(report("We need coffee", { intent: "purchase", item_id: "coffee" }))?.id, "coffee-machine");
});
