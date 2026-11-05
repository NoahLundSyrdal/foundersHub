import type { OfficeRequest, Station } from "@office/contracts";

export type StationId = Station["id"];
/** A clickable part of the office model. Ids match the sections in officeScene.ts. */
export type TwinThing = {
  id: string;
  name: string;
  stationId: StationId | null;
  supplies: string[];
  /** Words in a report that point at this thing. Used only to show it in the right place. */
  mentions?: RegExp;
};

export const twinThings: TwinThing[] = [
  {
    id: "counter",
    name: "Kitchen counter",
    stationId: "kitchen",
    supplies: ["cups"],
    mentions: /cup|kopp|sink|vask|counter|shelf|shelves|dishwasher|oppvask/i,
  },
  {
    id: "fridge",
    name: "Drinks fridge",
    stationId: "kitchen",
    supplies: [],
    mentions: /fridge|drinks|kjøleskap/i,
  },
  {
    id: "coffee-machine",
    name: "Coffee machine",
    stationId: "kitchen",
    supplies: ["coffee"],
    mentions: /coffee|espresso|machine|kaffe/i,
  },
  {
    id: "bathroom",
    name: "Bathroom",
    stationId: "bathroom",
    supplies: ["toilet-paper", "soap"],
  },
  {
    id: "printer",
    name: "Printer",
    stationId: "printer",
    supplies: ["printer-paper", "toner"],
  },
  { id: "cloakroom", name: "Cloakroom", stationId: null, supplies: [], mentions: /\bcloakroom\b|garderobe/i },
  { id: "event-hall", name: "Event hall", stationId: null, supplies: [], mentions: /\bevent[ -]?hall\b|\bauditorium\b|arrangementssal/i },
  { id: "workspace", name: "Worktables", stationId: null, supplies: [], mentions: /\bwork[ -]?tables?\b|\bworkspace\b|\bdesks?\b|arbeidsbord/i },
  { id: "cafe", name: "Café", stationId: null, supplies: [], mentions: /\bcaf[eé]\b|kafé/i },
];

/** Floors and walls belong to the room around them, so clicking anywhere in a room works. */
export function roomAt(x: number, z: number): string | null {
  if (x < -10.2 || x > 9.9 || z < -7.2 || z > 7.3) return null;
  if (x < 1.15) return z > 0.5 ? "event-hall" : "workspace";
  if (z > -1.5) return "cafe";
  return x < 4.55 ? "cloakroom" : "bathroom";
}
/**
 * The object a report is about, or null when only its station is known. Only the person's
 * own words count, because the agent's reply can mention other things ("unplug the machine").
 * Objects are checked in list order, so the broad coffee machine pattern comes last.
 */
export function thingForRequest(request: OfficeRequest): TwinThing | null {
  // Explicit room references override the input device's location. Do not guess
  // when a report names multiple rooms; it needs a more precise location.
  const rooms = twinThings.filter((thing) => !thing.stationId && thing.mentions?.test(request.original_text));
  if (rooms.length) return rooms.length === 1 ? rooms[0] : null;
  const candidates = twinThings.filter((thing) => thing.stationId === request.station_id);
  if (candidates.length === 1) return candidates[0];
  return (
    candidates.find((thing) => request.item_id !== null && thing.supplies.includes(request.item_id)) ??
    candidates.find((thing) => thing.mentions?.test(request.original_text)) ??
    null
  );
}
export const thingById = (id: string) => twinThings.find((thing) => thing.id === id);
export const thingsAt = (station: StationId) => twinThings.filter((thing) => thing.stationId === station);

/** Only saved actionable reports create room markers; chat and drafts do not. */
export function reportMarkers(requests: OfficeRequest[]) {
  return twinThings.filter((thing) => !thing.stationId).flatMap((thing) => {
    const reports = requests.filter((r) =>
      ["maintenance", "purchase", "grocery"].includes(r.intent ?? "") &&
      ["maintenance_open", "awaiting_approval", "ordered", "checkout_ready", "failed"].includes(r.status) &&
      thingForRequest(r)?.id === thing.id);
    return reports.length ? [{ thing, reports }] : [];
  });
}
