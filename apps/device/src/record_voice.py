"""TEMPORARY: record voice-enrollment clips from the board's microphone.

Needs firmware that streams the microphone: apps/device/firmware/office_station or voice_record
(microphone on ports 5-6).
Press the board button to start, read the text, press again to stop. Each take is
written as a 16 kHz mono WAV in recordings/ (git-ignored). Ctrl-C to quit.

    apps/device/.venv/bin/python apps/device/src/record_voice.py leo
"""

from __future__ import annotations

import argparse
import array
import base64
import json
import math
import wave
from pathlib import Path

import serial  # pyserial
from serial.tools import list_ports

RATE = 16000
CHUNK_BYTES = 512  # firmware sends 256 int16 samples per line
ROOT = Path(__file__).resolve().parents[3]


def find_port() -> str:
    ports = [p.device for p in list_ports.comports() if "ACM" in p.device or "usbmodem" in p.device]
    if not ports:
        raise SystemExit("No board found on USB. Plug it in with a data cable (and stop `atech monitor`).")
    return ports[0]


def next_path(out: Path, name: str) -> Path:
    n = 1
    while (out / f"{name}-{n}.wav").exists():
        n += 1
    return out / f"{name}-{n}.wav"


def save(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)


def stats(pcm: bytes) -> tuple[float, float, float]:
    samples = array.array("h", pcm)
    if not samples:
        return 0.0, 0.0, 0.0
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    peak = max(abs(s) for s in samples)
    return len(samples) / RATE, rms, peak


def lines(ser: serial.Serial):
    """Yield lines, reading in large blocks. Byte-at-a-time readline() is too slow:
    the board's USB buffer then overflows and drops bytes mid-line."""
    buf = bytearray()
    while True:
        buf += ser.read(max(1, ser.in_waiting))
        *done, rest = buf.split(b"\n")
        buf = bytearray(rest)
        yield from done


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", help="speaker name, lower-case as in config/people.json (e.g. leo)")
    ap.add_argument("--port", help="serial port (default: first /dev/ttyACM* or usbmodem)")
    ap.add_argument("--out", type=Path, default=ROOT / "recordings", help="output directory")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    port = args.port or find_port()
    name = args.name.strip().lower()

    # exclusive: a second reader on the same port would silently steal half the audio.
    try:
        ser = serial.Serial(port, 115200, timeout=1, exclusive=True)
    except serial.SerialException as e:
        raise SystemExit(f"Cannot open {port}: another program is using the board "
                         f"(another record_voice.py, atech monitor, or the bridge). Close it first.\n({e})")
    with ser:
        print(f"[{name}] Connected on {port}. Press the board button to start recording.")
        pcm = bytearray()
        recording = False
        bad = short = 0
        for line in lines(ser):
            if not line.startswith(b'{"type":"'):
                continue
            try:
                msg = json.loads(line)
                if msg.get("type") == "audio":
                    chunk = base64.b64decode(msg["d"], validate=True)
            except ValueError:
                bad += recording
                continue
            kind = msg.get("type")
            if kind == "audio" and recording:
                pcm += chunk
                short += len(chunk) < CHUNK_BYTES
            elif kind == "rec" and msg.get("state") == "start":
                pcm.clear()
                bad = short = 0
                recording = True
                print("  ● recording... press the button again to stop")
            elif kind == "rec" and msg.get("state") == "stop" and recording:
                recording = False
                secs, rms, peak = stats(bytes(pcm))
                path = next_path(args.out, name)
                save(path, bytes(pcm))
                warn = []
                if msg.get("dropped"):
                    warn.append(f"{msg['dropped']} samples dropped on the board")
                if short > 1:  # only the final flush may be short
                    warn.append(f"{short} chunks arrived cut short, audio has gaps; record again")
                if bad:
                    warn.append(f"{bad} damaged chunks skipped, audio has gaps; record again")
                if peak >= 32000:
                    warn.append("clipping, move back from the mic")
                if rms < 100:
                    warn.append("very quiet, move closer")
                print(f"  saved {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}: "
                      f"{secs:.1f} s, rms {rms:.0f}, peak {peak:.0f}"
                      + (f"  WARNING: {'; '.join(warn)}" if warn else ""))
                print("  Press the button for another take, or Ctrl-C to finish.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
