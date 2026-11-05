"""Read-only temperature trend for one board, computed on request.

Uses only stored physical ``facility_events`` readings (metric ``temperature_c``,
``simulation`` strictly false). It writes nothing, sends nothing and does not
run in the background: no alerting is enabled by this module. When the stored
history cannot support a trend it reports ``available: false`` with a reason
instead of inventing a measurement.
"""
from __future__ import annotations

import json
import math
from datetime import UTC, datetime

from office.facility_rules import BOUNDS

METRIC = "temperature_c"
UNIT = "°C"
WINDOW_SECONDS = 600.0
STALE_SECONDS = 120.0
FUTURE_TOLERANCE_SECONDS = 5.0
DROP_THRESHOLD_C = 3.0
STEADY_BAND_C = 0.5
SOURCE = "physical sensor readings (facility_events, simulation=false)"
ALERTING = "none: trend is computed only when queried; no background alerting or notifications"


def _parse_time(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds")


def _valid_value(value) -> float | None:
    if type(value) not in (int, float):  # excludes bool, str, None
        return None
    value = float(value)
    low, high = BOUNDS[METRIC]
    if not math.isfinite(value) or not low <= value <= high:
        return None
    return value


def valid_samples(conn, board_id: str, now: datetime) -> list[tuple[datetime, float]]:
    """Distinct-timestamp physical samples within the window, oldest first."""
    rows = conn.execute(
        "SELECT body FROM facility_events WHERE "
        "CASE WHEN json_valid(body) THEN json_extract(body, '$.board_id') END = ? AND "
        "CASE WHEN json_valid(body) THEN json_extract(body, '$.metric') END = ?",
        (board_id, METRIC),
    ).fetchall()
    by_time: dict[datetime, set[float]] = {}
    for row in rows:
        try:
            body = json.loads(row[0])
        except (TypeError, ValueError):
            continue
        if not isinstance(body, dict):
            continue
        if body.get("board_id") != board_id or body.get("metric") != METRIC:
            continue
        if body.get("simulation") is not False:
            continue
        value = _valid_value(body.get("value"))
        at = _parse_time(body.get("timestamp"))
        if value is None or at is None:
            continue
        age = (now - at).total_seconds()
        if age < -FUTURE_TOLERANCE_SECONDS or age > WINDOW_SECONDS:
            continue
        by_time.setdefault(at, set()).add(value)
    # Conflicting values recorded for the same instant are ambiguous; skip them.
    return sorted((at, next(iter(values))) for at, values in by_time.items() if len(values) == 1)


def compute(conn, board_id: str, now: datetime) -> dict:
    now = now.astimezone(UTC)
    samples = valid_samples(conn, board_id, now)
    result = {
        "board_id": board_id, "metric": METRIC, "unit": UNIT, "source": SOURCE,
        "simulation": False, "alerting": ALERTING, "evaluated_at": _iso(now),
        "window_seconds": WINDOW_SECONDS, "stale_after_seconds": STALE_SECONDS,
        "drop_threshold_c": DROP_THRESHOLD_C, "sample_count": len(samples),
        "available": False, "status": "unknown", "reason": None,
        "delta_c": None, "drop_detected": None, "earliest": None, "latest": None,
        "latest_sample_at": _iso(samples[-1][0]) if samples else None,
    }
    if samples and (now - samples[-1][0]).total_seconds() > STALE_SECONDS:
        result["reason"] = "stale: latest physical temperature reading is older than 120 seconds"
        return result
    if len(samples) < 2:
        result["reason"] = "insufficient: fewer than two valid physical readings with distinct timestamps in the last 10 minutes"
        return result
    (first_at, first), (last_at, last) = samples[0], samples[-1]
    delta = round(last - first, 3)
    drop = delta <= -DROP_THRESHOLD_C
    status = ("drop_detected" if drop else "falling" if delta < -STEADY_BAND_C
              else "rising" if delta > STEADY_BAND_C else "stable")
    result.update(
        available=True, status=status, delta_c=delta, drop_detected=drop,
        earliest={"timestamp": _iso(first_at), "value_c": first},
        latest={"timestamp": _iso(last_at), "value_c": last},
        span_seconds=round((last_at - first_at).total_seconds(), 3),
    )
    return result
