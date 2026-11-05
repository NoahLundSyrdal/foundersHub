#!/usr/bin/env python3
"""Translate Atech JSON lines to office events. Simulation needs only Python."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

STATIONS = ("kitchen", "bathroom", "printer")
EVENT_TYPES = ("button_pressed", "speech_transcript", "stock_low")
DEFAULT_MAPPING = {station: station for station in STATIONS}


def make_event(station: str, kind: str, payload: dict | None = None) -> dict:
    return validate_event({
        "event_id": str(uuid4()), "station_id": station, "type": kind,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "payload": payload or {},
    })


def validate_event(event: dict) -> dict:
    """Fail early on the bridge; the shared server schema remains authoritative."""
    if not isinstance(event, dict):
        raise ValueError("Event must be a JSON object")
    if event.get("station_id") not in STATIONS:
        raise ValueError("Unknown station_id")
    if event.get("type") not in EVENT_TYPES:
        raise ValueError("Unsupported event type")
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not 1 <= len(event_id) <= 128:
        raise ValueError("event_id must be a non-empty string of at most 128 characters")
    timestamp = event.get("timestamp")
    if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
        raise ValueError("timestamp must be an ISO UTC string ending in Z")
    try:
        datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Invalid timestamp") from exc
    payload = event.get("payload", {})
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    text, item_id, level = payload.get("text"), payload.get("item_id"), payload.get("level")
    if text is not None and (not isinstance(text, str) or not 1 <= len(text) <= 2000):
        raise ValueError("text must contain 1–2000 characters")
    if item_id is not None and (not isinstance(item_id, str) or len(item_id) > 128):
        raise ValueError("item_id must be a string of at most 128 characters")
    if level is not None and (type(level) not in (int, float) or not math.isfinite(level) or not 0 <= level <= 100):
        raise ValueError("level must be between 0 and 100")
    if event["type"] == "speech_transcript" and not text:
        raise ValueError("Speech events require text")
    if event["type"] == "stock_low" and not item_id:
        raise ValueError("Stock events require item_id")
    return {key: event[key] for key in ("event_id", "station_id", "type", "timestamp")} | {"payload": payload}


def normalize_message(message: dict, mapping: dict[str, str] | None = None) -> dict | None:
    """Accept canonical events or project-specific Atech button/stock events."""
    if not isinstance(message, dict):
        raise ValueError("Serial line must contain a JSON object")
    if message.get("type") == "event":
        message = message.get("payload")
        if not isinstance(message, dict):
            raise ValueError("Atech event wrapper requires an object payload")
    if "event_id" in message:
        return validate_event(message)
    event_type = message.get("event_type")
    if event_type not in ("button", "stock_low"):
        return None  # sensor telemetry and log lines are not requests
    if event_type == "button" and message.get("value") is not True:
        return None  # ignore releases; firmware must emit a press edge, not a held level
    station = (mapping or DEFAULT_MAPPING).get(str(message.get("key", "")))
    if station not in STATIONS:
        raise ValueError("Unknown firmware key; configure --mapping before using this button")
    if event_type == "button":
        return make_event(station, "button_pressed")
    value = message.get("value")
    if not isinstance(value, dict):
        raise ValueError("stock_low value must contain item_id and optional level")
    return make_event(station, "stock_low", value)


def post_event(base_url: str, event: dict, retries: int = 2) -> dict:
    """Retry transient failures with the same event ID and exact request body."""
    body = json.dumps(validate_event(event)).encode("utf-8")
    for attempt in range(retries + 1):
        request = Request(base_url.rstrip("/") + "/api/device-events", data=body,
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=15) as response:
                return json.load(response)
        except HTTPError as exc:
            detail = exc.read(2000).decode("utf-8", errors="replace")
            if exc.code not in (429, 502, 503, 504) or attempt == retries:
                raise RuntimeError(f"API returned HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, ConnectionError) as exc:
            if attempt == retries:
                raise RuntimeError(f"Cannot reach API at {base_url}: {exc}") from exc
        time.sleep(0.25 * (2 ** attempt))
    raise RuntimeError("Event delivery exhausted retries")


def feedback_action(result: dict) -> dict:
    command = result.get("command")
    if not isinstance(command, dict) or command.get("station_id") not in STATIONS:
        raise ValueError("API response has no valid device command")
    if command.get("color") not in ("blue", "amber", "green", "red") or not isinstance(command.get("text"), str):
        raise ValueError("Invalid device feedback")
    return {"action": "office_feedback", "value": command}


def send_feedback(port, result: dict, last_sent: dict[str, dict]) -> bool:
    """Send a valid command only when its station's visible state has changed."""
    action = feedback_action(result)
    command = action["value"]
    station = command["station_id"]
    if last_sent.get(station) == command:
        return False
    port.write((json.dumps(action) + "\n").encode("utf-8"))
    last_sent[station] = command.copy()
    return True


def poll_feedback(base_url: str, stations, port, last_sent: dict[str, dict]) -> None:
    """Refresh configured stations, allowing approval/resolve actions to reach USB."""
    for station in sorted(set(stations)):
        try:
            request = Request(base_url.rstrip("/") + f"/api/devices/{station}/command")
            with urlopen(request, timeout=1) as response:
                result = json.load(response)
            command = result.get("command")
            if command is None:
                continue
            if not isinstance(command, dict) or command.get("station_id") != station:
                raise ValueError("Command station does not match requested station")
            send_feedback(port, result, last_sent)
        except (HTTPError, URLError, TimeoutError, ConnectionError, ValueError) as exc:
            print(f"Feedback polling failed for {station}: {exc}", file=sys.stderr)


def load_mapping(path: str | None) -> dict[str, str]:
    if not path:
        return DEFAULT_MAPPING.copy()
    with open(path, encoding="utf-8") as source:
        mapping = json.load(source)
    if not isinstance(mapping, dict) or not mapping or any(not isinstance(key, str) or value not in STATIONS for key, value in mapping.items()):
        raise ValueError("Mapping must be an object from firmware key to kitchen/bathroom/printer")
    return mapping


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--simulate", action="store_true")
    source.add_argument("--port", help="Actual serial port, e.g. /dev/cu.usbmodem...")
    parser.add_argument("--station", choices=STATIONS, default="kitchen")
    input_mode = parser.add_mutually_exclusive_group()
    input_mode.add_argument("--text", help="Simulated speech transcript; no audio is recorded")
    input_mode.add_argument("--item", help="Simulated low-stock catalogue item ID")
    parser.add_argument("--level", type=float, default=15)
    parser.add_argument("--mapping", help="JSON firmware key → station mapping file")
    parser.add_argument("--api", default="http://127.0.0.1:3001")
    parser.add_argument("--baud", type=int, default=115200)
    args = parser.parse_args()
    parsed_url = urlparse(args.api)
    if parsed_url.scheme not in ("http", "https") or not parsed_url.hostname:
        parser.error("--api must be an http(s) base URL")
    try:
        if args.simulate:
            if args.text is not None:
                event = make_event(args.station, "speech_transcript", {"text": args.text})
            elif args.item is not None:
                event = make_event(args.station, "stock_low", {"item_id": args.item, "level": args.level})
            else:
                event = make_event(args.station, "button_pressed")
            print(json.dumps({"sending": event}), flush=True)
            result = post_event(args.api, event)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            print(json.dumps(feedback_action(result), ensure_ascii=False))
            return 0
        if args.text is not None or args.item is not None:
            parser.error("--text and --item apply only to --simulate")
        mapping = load_mapping(args.mapping)
        try:
            import serial
        except ImportError as exc:
            raise RuntimeError('USB mode requires pyserial: python3 -m pip install "./apps/device[usb]"') from exc
        with serial.Serial(args.port, args.baud, timeout=1) as port:
            print(f"Listening on {args.port} at {args.baud} baud", file=sys.stderr)
            last_sent: dict[str, dict] = {}
            next_poll = 0.0
            while True:
                if time.monotonic() >= next_poll:
                    poll_feedback(args.api, mapping.values(), port, last_sent)
                    next_poll = time.monotonic() + 1.0
                raw = port.readline(65537)
                if not raw:
                    continue
                if len(raw) > 65536 or not raw.endswith(b"\n"):
                    # Drain an overlong line instead of parsing a partial JSON message.
                    while raw and not raw.endswith(b"\n"):
                        raw = port.readline(65537)
                    print("Ignored incomplete or overlong serial line", file=sys.stderr)
                    continue
                try:
                    event = normalize_message(json.loads(raw), mapping)
                    if event is None:
                        continue
                    result = post_event(args.api, event)
                    send_feedback(port, result, last_sent)
                    print(json.dumps({"event_id": event["event_id"], "request_id": result["request"]["id"], "duplicate": result["duplicate"]}), flush=True)
                except (ValueError, RuntimeError, UnicodeDecodeError) as exc:
                    print(f"Event failed: {exc}", file=sys.stderr)
    except KeyboardInterrupt:
        return 0
    except (ValueError, RuntimeError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
