"""Opt-in, idempotent local demo history; never dispatches actions or physical telemetry."""
import argparse
from datetime import datetime, timedelta, timezone

from office import db, procurement_workflow
from office.facility_rules import FacilityReading, FacilityEngine


def seed(conn, now=None):
    now = now or datetime.now(timezone.utc)
    FacilityEngine(conn)
    procurement_workflow.ensure_schema(conn)
    # Two earlier sessions, three environmental measurements each. Fixed IDs make reruns safe.
    for session, hours, co2, temperature, humidity in [(1, 4, 1650., 27.2, 64.), (2, 2, 1820., 28.1, 67.)]:
        at = (now - timedelta(hours=hours)).isoformat()
        for metric, value in [("co2_ppm", co2), ("temperature_c", temperature), ("humidity_pct", humidity)]:
            event_id = f"demo-stage-history-{session}-{metric}"
            reading = FacilityReading(event_id=event_id, board_id="kitchen", metric=metric,
                                      value=value, timestamp=at, simulation=True)
            conn.execute("INSERT OR IGNORE INTO facility_events VALUES (?,?)", (event_id, reading.model_dump_json()))
            # Display the latest simulated sample without overwriting any physical stream.
            body = conn.execute("SELECT body FROM facility_events WHERE id=?", (event_id,)).fetchone()[0]
            stream = f"kitchen:{metric}:1"
            conn.execute("INSERT INTO facility_readings VALUES (?,?) ON CONFLICT(stream) DO UPDATE SET body=excluded.body "
                         "WHERE json_extract(excluded.body, '$.timestamp') > json_extract(facility_readings.body, '$.timestamp')", (stream, body))
        task_id = f"demo-stage-environment-{session}"
        conn.execute("INSERT OR IGNORE INTO tasks (id,board_id,person,prompt,status,reply,created_at,updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?)", (task_id, "kitchen", "Demo fixture",
                     "[Simulated history] Environmental check at the stage button", "done",
                     f"Simulated stage readings: CO₂ {co2:g} ppm, temperature {temperature:g} °C, humidity {humidity:g}%. Historical sample; current conditions unknown.", at, at))
        procurement_workflow.store_context(conn, task_id, {"interpretation": {"intent": "conversation"}})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, help="Explicit local demo SQLite path")
    args = parser.parse_args()
    conn = db.connect(args.database)
    seed(conn)
    conn.close()
    print("Seeded two simulated stage environmental checks (six readings); no actions executed.")
