#!/usr/bin/env python3
"""Real Atech board <-> office API, using the official SDK (`pip install atech`). Owner: hardware.

Board -> API:  button press  -> POST /api/device-events (button_pressed = push-to-talk for that station)
               sensor events -> POST /api/sensor (throttled)
API -> board:  latest command per station -> office_say (screen) + led_<station>_fill (light grid)

Firmware: apps/device/firmware/office_station/project.yaml. Event/action names come from its instances.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import unicodedata
from urllib.request import Request, urlopen

from bridge import make_event, post_event  # same event contract + retry-with-same-id as the simulator

STATIONS = ("kitchen", "bathroom", "printer")
COLORS = {"blue": (0, 0, 255), "amber": (255, 120, 0), "green": (0, 255, 0), "red": (255, 0, 0)}
SENSORS = {  # event key -> (sensor kind, min seconds between posts)
    "aht20_temperature": ("temperature", 60),
    "aht20_humidity": ("humidity", 60),
    "distance_sensor_min_distance": ("distance_mm", 5),
}
SENSOR_STATION = "kitchen"  # which station's board the sensors belong to; change if they move


NO_TARGET_MM = 32767  # distance sensor sentinel: nothing in range (or sensor not started)


def screen_text(text: str) -> str:
    """Firmware unescapes \\uXXXX badly: send plain ASCII, short enough for the 160x80 screen."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()[:180]


def post_json(api: str, path: str, body: dict) -> dict:
    req = Request(api.rstrip("/") + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=3) as r:  # short: this runs on the button thread
        return json.load(r)


def read_events(board, api: str) -> None:
    last_post: dict[str, float] = {}
    for ev in board.events():
        try:
            station = ev.key.removeprefix("btn_")
            if ev.key.startswith("btn_") and station in STATIONS and ev.value == 1:
                post_event(api, make_event(station, "button_pressed"))
                print(f"press {station}", flush=True)
            elif ev.key in SENSORS and not (ev.key == "distance_sensor_min_distance" and ev.value >= NO_TARGET_MM):
                kind, every = SENSORS[ev.key]
                if time.monotonic() - last_post.get(kind, 0) >= every:
                    last_post[kind] = time.monotonic()
                    post_json(api, "/api/sensor", {"board_id": SENSOR_STATION, "kind": kind, "value": float(ev.value)})
        except Exception as exc:  # keep reading; one bad event must not kill the bridge
            print(f"event {ev.key} failed: {exc}", file=sys.stderr)


def push_feedback(board, api: str) -> None:
    from atech import ActionError

    shown: dict[str, tuple] = {}
    while True:
        for station in STATIONS:
            try:
                with urlopen(f"{api.rstrip('/')}/api/devices/{station}/command", timeout=2) as r:
                    cmd = json.load(r)["command"]
                if not cmd or shown.get(station) == (cmd["text"], cmd["color"]):
                    continue
                r_, g, b = COLORS.get(cmd["color"], (255, 255, 255))
                board.call(f"led_{station}_fill", {"r": r_, "g": g, "b": b})
                board.call("office_say", screen_text(f"{station}: {cmd['text']}"))  # one shared screen: prefix the station
                shown[station] = (cmd["text"], cmd["color"])
            except ActionError as exc:  # board said no / didn't answer: retry next poll
                print(f"feedback {station}: {exc}", file=sys.stderr)
            except OSError as exc:
                print(f"feedback {station}: API or serial error: {exc}", file=sys.stderr)
            except Exception as exc:  # never let one bad command kill the bridge
                print(f"feedback {station}: {exc!r}", file=sys.stderr)
        time.sleep(1)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", help="serial port; default: first Atech board on USB (`atech ports`)")
    p.add_argument("--api", default="http://127.0.0.1:3001")
    p.add_argument("--diagnose", action="store_true", help="reboot the board first and print its boot report")
    args = p.parse_args()
    from atech import Board

    with Board.connect(port=args.port) as board:
        if args.diagnose:
            print(f"boot report: {board.diagnostics()}", file=sys.stderr)  # reboots; shows which modules answered
        threading.Thread(target=read_events, args=(board, args.api), daemon=True).start()
        try:
            push_feedback(board, args.api)
        except KeyboardInterrupt:
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
