import { DatabaseSync } from "node:sqlite";
import { mkdirSync } from "node:fs";
import { dirname } from "node:path";
import type { OfficeRequest } from "@office/contracts";
export class Store {
  db: DatabaseSync;
  constructor(path: string) {
    if (path !== ":memory:") mkdirSync(dirname(path), { recursive: true });
    this.db = new DatabaseSync(path);
    this.db.exec(`PRAGMA journal_mode=WAL;
      CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, body TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS device_events(event_id TEXT PRIMARY KEY, request_id TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS execution_attempts(request_id TEXT PRIMARY KEY, proposal TEXT NOT NULL, state TEXT NOT NULL, reference TEXT);
      PRAGMA user_version=1;`);
  }
  all(): OfficeRequest[] {
    return (
      this.db
        .prepare("SELECT body FROM requests ORDER BY rowid DESC")
        .all() as { body: string }[]
    ).map((r) => JSON.parse(r.body));
  }
  get(id: string): OfficeRequest | undefined {
    const row = this.db
      .prepare("SELECT body FROM requests WHERE id=?")
      .get(id) as { body: string } | undefined;
    return row ? JSON.parse(row.body) : undefined;
  }
  save(request: OfficeRequest) {
    this.db
      .prepare(
        "INSERT INTO requests(id,body) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
      )
      .run(request.id, JSON.stringify(request));
  }
  event(id: string): string | undefined {
    return (
      this.db
        .prepare("SELECT request_id FROM device_events WHERE event_id=?")
        .get(id) as { request_id: string } | undefined
    )?.request_id;
  }
  link(event: string, request: string) {
    this.db
      .prepare("INSERT INTO device_events VALUES(?,?)")
      .run(event, request);
  }
  close() {
    this.db.close();
  }
}
