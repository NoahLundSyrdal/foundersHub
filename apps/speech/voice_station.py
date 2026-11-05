#!/usr/bin/env python3
"""Voice station on the physical Atech board (office_station firmware).

Press the board button → speak into the laptop mic → press again (or go quiet)
→ ElevenLabs/Whisper transcribes → the answer is shown on the board's screen and
spoken through the board's speaker.

- Air/temperature/CO2 questions are answered from the board's LIVE readings
  (co2_co2, aht20_temperature, aht20_humidity), in the speaker's language.
- Anything else is posted to the office API as a speech_transcript event.

Holds the board's serial port while running (stop the bridge first).

    apps/speech/.venv/bin/python apps/speech/voice_station.py [--port /dev/cu.usbmodem…] [--api http://127.0.0.1:3001]
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stt import SpeechToText, air_summary, answer_language, is_air_question, post_transcript  # noqa: E402

GREEN, AMBER, RED, BLUE, OFF = ({"r": 0, "g": 180, "b": 0}, {"r": 255, "g": 120, "b": 0},
                                {"r": 255, "g": 0, "b": 0}, {"r": 0, "g": 0, "b": 255}, {"r": 0, "g": 0, "b": 0})
PCM_CHUNK = 2250  # bytes per speaker_play_pcm action (~3000 base64 chars, the firmware's limit)


def loud(pcm: bytes, thr_db: float = -38.0, ratio: float = 12.0, peak: float = 0.97, drive: float = 1.6) -> bytes:
    """Make TTS clearly audible on the small board speaker (tested by ear, setting "C").

    Speech is ~12 dB quieter than the firmware's tones at the same volume. So: cut bass
    (<400 Hz) and boost 1.5–4 kHz where the speaker is efficient, compress hard
    (≈ -7.5 dBFS RMS), gate silence, and soft-limit just below full scale.
    """
    import numpy as np
    fs = 16000
    x = np.frombuffer(pcm, "<i2").astype(np.float32) / 32768
    if x.size == 0 or np.abs(x).max() == 0:
        return pcm
    X = np.fft.rfft(x); f = np.fft.rfftfreq(len(x), 1 / fs)
    X[f < 400] *= (f[f < 400] / 400) ** 3
    X *= 10 ** ((8.0 * np.exp(-((f - 2500) / 1800) ** 2)) / 20)
    x = np.fft.irfft(X, len(x)); x /= np.abs(x).max()
    a, r, e = np.exp(-1 / (0.002 * fs)), np.exp(-1 / (0.05 * fs)), 1e-4
    env = np.empty_like(x)
    for i, v in enumerate(np.abs(x)):
        e = a * e + (1 - a) * v if v > e else r * e + (1 - r) * v
        env[i] = e
    lvl = 20 * np.log10(np.maximum(env, 1e-4))
    y = x * 10 ** (np.where(lvl > thr_db, (thr_db - lvl) * (1 - 1 / ratio), 0.0) / 20)
    y[lvl < -48] *= 0.15
    y /= np.percentile(np.abs(y), 99.5)
    y = np.clip(np.tanh(drive * y) / np.tanh(drive), -1, 1) * peak
    return (y * 32767).astype("<i2").tobytes()


def screen_text(text: str) -> str:
    """The built-in screen font is ASCII; transliterate Norwegian letters."""
    for a, b in (("æ", "ae"), ("ø", "o"), ("å", "aa"), ("Æ", "Ae"), ("Ø", "O"), ("Å", "Aa"), ("°", " ")):
        text = text.replace(a, b)
    return text.encode("ascii", "ignore").decode()[:180]


class Station:
    def __init__(self, board, stt: SpeechToText, api: str):
        self.board, self.stt, self.api = board, stt, api
        self.lock = threading.Lock()  # one action at a time on the serial link
        self.press = threading.Event()
        self.pending: tuple[str, float] | None = None  # (request id, when) of an open clarification question
        self.volume = 0.85

    def call(self, key, value="", timeout=3.0):
        with self.lock:
            try:
                return self.board.call(key, value, timeout=timeout)
            except Exception as exc:  # keep the station alive if one action fails
                print(f"  ! {key}: {exc}", file=sys.stderr)

    def show(self, text: str, led=None):
        self.call("office_say", screen_text(text))
        if led:
            self.call("led_kitchen_fill", led)

    def beep(self, tune="b:d=16,o=6,b=200:c"):
        self.call("speaker_play_rtttl", tune)

    def say(self, text: str, lang: str):
        """TTS on the laptop (macOS say), streamed as 16 kHz PCM to the board speaker."""
        with tempfile.TemporaryDirectory() as d:
            wav = Path(d) / "tts.wav"
            voice = ["-v", "Nora", "-r", "165"] if lang == "no" else ["-v", "Samantha", "-r", "165"]
            subprocess.run(["say", *voice, "--data-format=LEI16@16000", "-o", str(wav), text], check=True)
            with wave.open(str(wav)) as w:
                pcm = w.readframes(w.getnframes())
        pcm = loud(pcm)
        for i in range(0, len(pcm), PCM_CHUNK):
            self.call("speaker_play_pcm", base64.b64encode(pcm[i:i + PCM_CHUNK]).decode(), timeout=5)

    def live_air(self) -> dict:
        """Same shape as GET /api/air-quality, built from the board's latest events."""
        now = datetime.now(timezone.utc).isoformat()
        def reading(key):
            ev = self.board.latest(key)
            return {"value": float(ev.value), "received_at": now} if ev is not None else None
        r = {"temperature_c": reading("aht20_temperature") or reading("co2_temperature"),
             "humidity_pct": reading("aht20_humidity") or reading("co2_humidity"),
             "co2_ppm": reading("co2_co2")}
        level, en = "good", []
        t, h, c = (r[k]["value"] if r[k] else None for k in ("temperature_c", "humidity_pct", "co2_ppm"))
        if c is not None and c > 1500 or t is not None and (t > 26 or t < 17): level = "poor"
        elif (c is not None and c > 1000) or (t is not None and (t > 24 or t < 19)) or (h is not None and (h < 25 or h > 60)):
            level = "moderate"
        if not any(r.values()):
            level = "unknown"
        parts = [f"{t:.1f} °C" if t is not None else None, f"{h:.0f} % humidity" if h is not None else None,
                 f"CO2 {c:.0f} ppm" if c is not None else None]
        verdict = {"good": "Air quality is good", "moderate": "Air quality is OK but not ideal",
                   "poor": "Air quality is poor", "unknown": "No readings yet"}[level]
        if c is not None and c > 1000: en.append("open a window")
        if t is not None and t > 24: en.append("it is too warm")
        summary = f"{verdict}: {', '.join(p for p in parts if p)}." + (f" Tip: {' and '.join(en)}." if en else "")
        return {"level": level, "readings": r, "summary": summary, "stale": False}

    TELEMETRY = ("co2_co2", "aht20_temperature", "aht20_humidity")

    def watch_button(self):
        """Button presses → push-to-talk; climate samples → POST /api/telemetry (physical, throttled)."""
        last: dict[str, float] = {}
        for ev in self.board.events(keys=["btn_kitchen", *self.TELEMETRY]):
            if ev.key == "btn_kitchen":
                if ev.value in (1, True):
                    self.press.set()
                continue
            now = time.time()
            if now - last.get(ev.key, 0) < 4:  # one sample per key per 4 s; the stream can repeat lines
                continue
            last[ev.key] = now
            stamp = datetime.now(timezone.utc)
            body = {"key": ev.key, "value": float(ev.value), "board_id": "kitchen",
                    "event_id": f"kitchen-{ev.key}-{int(stamp.timestamp() * 1000)}",
                    "timestamp": stamp.isoformat().replace("+00:00", "Z"), "simulation": False}
            threading.Thread(target=self._post, args=("/api/telemetry", body), daemon=True).start()

    def _post(self, path: str, body: dict):
        import json
        from urllib.request import Request, urlopen
        try:
            req = Request(self.api.rstrip("/") + path, data=json.dumps(body).encode(), method="POST",
                          headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=5) as r:
                return json.load(r)
        except Exception as exc:
            print(f"  ! POST {path}: {exc}", file=sys.stderr)
            return None

    def deliver_actions(self):
        """Claim backend station actions (e.g. CO₂ alarm sound) and play them; ack only after the board confirms."""
        while True:
            res = self._post("/api/devices/kitchen/actions/claim", {"consumer_id": "office-station-bridge"})
            for a in (res or {}).get("actions", []):
                print(f"🔔 action {a.get('kind')}/{a.get('pattern')}: {a.get('text', '')}")
                try:
                    if a.get("kind") != "sound":
                        raise ValueError(f"unsupported kind {a.get('kind')}")
                    with self.lock:
                        self.board.call("led_kitchen_fill", RED, timeout=3)
                        self.board.call("office_say", screen_text(a.get("text") or "Varsel"), timeout=3)
                        self.board.call("speaker_play_rtttl", "attention:d=8,o=6,b=180:c,p,c,p,c", timeout=3)
                    self._post(f"/api/devices/kitchen/actions/{a['id']}/acknowledge", {"status": "delivered"})
                except Exception as exc:
                    self._post(f"/api/devices/kitchen/actions/{a['id']}/acknowledge",
                               {"status": "failed", "detail": str(exc)[:400]})
            time.sleep(1.5)

    def handle_one(self):
        self.press.clear()
        self.show("Lytter... trykk igjen naar du er ferdig", RED)
        self.beep()
        audio = self.stt.record(stop="silence", max_seconds=10, silence_seconds=1.5, stop_event=self.press)
        self.press.clear()
        self.show("Tenker...", AMBER)
        text = self.stt.transcribe(audio)
        lang = answer_language(text, getattr(self.stt, "last_language", None)) if text else "no"
        print(f"📝 [{lang}] {text or '(nothing heard)'}")
        if not text:
            answer = "Jeg hørte ingenting. Prøv igjen." if lang == "no" else "I didn't hear anything. Try again."
        elif is_air_question(text):
            aq = self.live_air()
            answer = air_summary(aq, lang)
        else:
            try:
                pending = self.pending if self.pending and time.time() - self.pending[1] < 180 else None
                if pending:  # the agent asked a question: this utterance is the answer, not a new case
                    res = self._post(f"/api/requests/{pending[0]}/transcript", {"text": text[:2000]}) or {}
                else:
                    res = post_transcript(self.api, "kitchen", text)
                print("  API:", str(res)[:300])
                req = res.get("request") or res
                status, summary = req.get("status"), req.get("summary", "")
                self.pending = (req["id"], time.time()) if status == "needs_clarification" and req.get("id") else None
                if lang == "no":
                    answer = {"awaiting_approval": f"Jeg har laget et forslag: {summary}. Det venter på godkjenning.",
                              "needs_clarification": f"Jeg trenger litt mer informasjon. {req.get('question') or ''}",
                              }.get(status, f"Registrert: {summary}")
                else:
                    answer = {"awaiting_approval": f"I made a proposal: {summary}. It is waiting for approval.",
                              "needs_clarification": f"I need a bit more information. {req.get('question') or ''}",
                              }.get(status, f"Registered: {summary}")
            except Exception as exc:
                print(f"  ! API: {exc}", file=sys.stderr)
                answer = "Jeg fikk ikke kontakt med systemet." if lang == "no" else "I could not reach the system."
        print(f"💬 {answer}")
        self.show(answer, GREEN)
        self.say(answer, lang)

    def run(self):
        threading.Thread(target=self.watch_button, daemon=True).start()
        threading.Thread(target=self.deliver_actions, daemon=True).start()
        self.call("speaker_set_volume", self.volume)
        self.show("Trykk paa knappen og snakk", BLUE)
        print("Ready: press the board button to talk (Ctrl+C to quit).")
        while True:
            self.press.wait()
            self.handle_one()
            self.press.clear()
            self.call("led_kitchen_fill", BLUE)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", help="Board serial port (default: first Atech board found)")
    p.add_argument("--api", default="http://127.0.0.1:3001")
    p.add_argument("--provider", choices=("auto", "elevenlabs", "whisper"), default="auto")
    p.add_argument("--volume", type=float, default=0.85, help="Board speaker volume 0.0–1.0 (1.0 may distort)")
    args = p.parse_args()
    from atech import Board

    stt = SpeechToText(provider=args.provider)
    print(f"STT provider: {stt.provider}", file=sys.stderr)
    with Board.connect(port=args.port) if args.port else Board.connect() as board:
        try:
            station = Station(board, stt, args.api)
            station.volume = args.volume
            station.run()
        except KeyboardInterrupt:
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
