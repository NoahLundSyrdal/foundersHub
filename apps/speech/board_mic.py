"""Push-to-talk audio from the board microphone (office_station firmware), next to the atech protocol.

The firmware streams each recording as JSON lines on the same USB port the atech SDK uses:
    {"type":"rec","state":"start"}  {"type":"audio","d":"<base64 int16 LE>"}  {"type":"rec","state":"stop","dropped":N}
The SDK ignores those lines, so `connect()` routes them here and passes everything else on. The SDK's
serial transport already opens the port exclusively (a second reader would silently steal half the audio)
and reads in blocks (byte-at-a-time reads let the board's USB buffer overflow and drop bytes mid-line).

    board, mic = connect()          # instead of atech.Board.connect()
    take = mic.next_take()          # blocks until the button press -> press again (or auto-stop)
    take.wav(), take.warnings()
"""
from __future__ import annotations

import array
import base64
import io
import json
import math
import queue
import threading
import wave
from dataclasses import dataclass

RATE = 16_000
CHUNK_BYTES = 512  # the firmware sends 256 int16 samples per line; only the final flush may be shorter


@dataclass
class Take:
    pcm: bytes
    dropped: int = 0  # samples lost on the board (full ring buffer)
    short: int = 0  # chunks that arrived cut short
    bad: int = 0  # damaged lines skipped

    @property
    def seconds(self) -> float:
        return len(self.pcm) / 2 / RATE

    def levels(self) -> tuple[float, int]:
        samples = array.array("h", self.pcm)
        if not samples:
            return 0.0, 0
        return math.sqrt(sum(s * s for s in samples) / len(samples)), max(abs(s) for s in samples)

    def warnings(self) -> list[str]:
        rms, peak = self.levels()
        warn = []
        if self.dropped:
            warn.append(f"{self.dropped} samples dropped on the board")
        if self.short > 1:
            warn.append(f"{self.short} chunks arrived cut short; audio has gaps")
        if self.bad:
            warn.append(f"{self.bad} damaged chunks skipped; audio has gaps")
        if peak >= 32000:
            warn.append("clipping; move back from the mic")
        if self.pcm and rms < 100:
            warn.append("very quiet; move closer")
        return warn

    def wav(self) -> bytes:
        out = io.BytesIO()
        with wave.open(out, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(self.pcm)
        return out.getvalue()


class BoardMic:
    """Reassembles recordings from the firmware's lines. `feed` runs on the serial reader thread."""

    def __init__(self):
        self.takes: queue.Queue[Take] = queue.Queue()
        self.started = threading.Event()  # set on "rec start"; cleared by the consumer
        self._pcm = bytearray()
        self._short = self._bad = 0
        self._recording = False

    @staticmethod
    def wants(line: bytes) -> bool:
        return line.startswith((b'{"type":"audio"', b'{"type":"rec"'))

    def feed(self, line: bytes) -> None:
        try:
            msg = json.loads(line)
            chunk = base64.b64decode(msg["d"], validate=True) if msg.get("type") == "audio" else b""
        except (ValueError, KeyError, TypeError):
            self._bad += self._recording
            return
        kind = msg.get("type")
        if kind == "audio" and self._recording:
            self._pcm += chunk
            self._short += len(chunk) < CHUNK_BYTES
        elif kind == "rec" and msg.get("state") == "start":
            self._pcm.clear()
            self._short = self._bad = 0
            self._recording = True
            self.started.set()
        elif kind == "rec" and msg.get("state") == "stop" and self._recording:
            self._recording = False
            self.takes.put(Take(bytes(self._pcm), int(msg.get("dropped") or 0), self._short, self._bad))

    def next_take(self, timeout: float | None = None) -> Take | None:
        try:
            return self.takes.get(timeout=timeout)
        except queue.Empty:
            return None


def connect(port: str | None = None):
    """Open the board like `atech.Board.connect`, with the microphone lines routed to a `BoardMic`."""
    from atech import Board, SerialTransport, discover_ports

    mic = BoardMic()

    class MicTransport(SerialTransport):
        def _handle_line(self, raw: bytes) -> None:
            if BoardMic.wants(raw):
                mic.feed(raw)
            else:
                super()._handle_line(raw)

    if port is None:
        found = discover_ports()
        if not found:
            raise SystemExit("No Atech board found on USB. Plug it in with a data cable (and stop other programs using it).")
        port = found[0].device
    return Board(MicTransport(port=port)), mic
