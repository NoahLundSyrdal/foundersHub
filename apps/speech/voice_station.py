#!/usr/bin/env python3
"""Voice station on the physical Atech board (office_station firmware).

Press the board button → speak into the BOARD microphone → press again (auto-stop after 15 s)
→ the clip goes to the office agent (POST /api/input with audio): ElevenLabs transcription and
speaker identification run in parallel on the server, then the agent answers → the agent's reply is
shown on the board's screen and spoken through the board's speaker.

- A follow-up within 3 minutes continues the same conversation (conversation_id); the server refuses
  that when the request already has an action, and the station then starts a new one.
- `--mic laptop` records from the laptop microphone instead (e.g. firmware without a microphone).
- Physical `co2_co2` / `aht20_*` samples are forwarded to POST /api/telemetry; sound actions from
  /api/devices/kitchen/actions are played and acknowledged.

Holds the board's serial port while running (stop the bridge and record_voice.py first).

    apps/speech/.venv/bin/python apps/speech/voice_station.py [--port /dev/ttyACM0] [--api http://127.0.0.1:3001]
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stt import _load_env_key, _wav_bytes, answer_language  # noqa: E402

GREEN, AMBER, RED, BLUE, OFF = ({"r": 0, "g": 180, "b": 0}, {"r": 255, "g": 120, "b": 0},
                                {"r": 255, "g": 0, "b": 0}, {"r": 0, "g": 0, "b": 255}, {"r": 0, "g": 0, "b": 0})
PCM_CHUNK = 2250  # bytes per speaker_play_pcm action (~3000 base64 chars, the firmware's limit)
LEDS = {"green": GREEN, "amber": AMBER, "red": RED, "blue": BLUE}
CONTINUE_SECONDS = 180  # a follow-up within this window continues the conversation
MIN_SECONDS = 0.4  # shorter takes are treated as an accidental double press


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


def compress(wav: bytes) -> tuple[bytes, str]:
    """Opus in Ogg (~10x smaller than WAV) when ffmpeg exists: the server forwards the clip to the
    transcription and speaker-ID services, so a slow uplink pays for every byte twice."""
    if not shutil.which("ffmpeg"):
        return wav, "wav"
    out = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", "pipe:0", "-c:a", "libopus",
                          "-b:a", "32k", "-f", "ogg", "pipe:1"], input=wav, capture_output=True)
    return (out.stdout, "ogg") if out.returncode == 0 and out.stdout else (wav, "wav")


class Station:
    def __init__(self, board, api: str, mic=None, stt=None):
        self.board, self.api = board, api
        self.mic, self.stt = mic, stt  # board microphone (board_mic.BoardMic), else the laptop mic via stt
        self.lock = threading.Lock()  # one action at a time on the serial link
        self.press = threading.Event()
        self.conversation: tuple[str, float] | None = None  # (request id, when) to continue
        self.volume = 0.85
        self.eleven_key = _load_env_key("ELEVENLABS_API_KEY")

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
        """Speak on the board speaker: ElevenLabs TTS as 16 kHz PCM, else macOS `say`."""
        try:
            pcm = self._tts_elevenlabs(text) if self.eleven_key else self._tts_say(text, lang)
        except Exception as exc:
            print(f"  ! TTS: {exc}", file=sys.stderr)
            return
        pcm = loud(pcm)
        for i in range(0, len(pcm), PCM_CHUNK):
            self.call("speaker_play_pcm", base64.b64encode(pcm[i:i + PCM_CHUNK]).decode(), timeout=5)

    def _tts_elevenlabs(self, text: str) -> bytes:
        voice = os.environ.get("ELEVENLABS_TTS_VOICE", "EXAVITQu4vr4xnSDxMaL")  # multilingual premade voice
        model = os.environ.get("ELEVENLABS_TTS_MODEL", "eleven_flash_v2_5")  # low latency, includes Norwegian
        req = Request(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=pcm_16000",
                      data=json.dumps({"text": text, "model_id": model}).encode(), method="POST",
                      headers={"xi-api-key": self.eleven_key, "Content-Type": "application/json"})
        with urlopen(req, timeout=30) as r:
            return r.read()  # raw 16 kHz mono int16 LE

    def _tts_say(self, text: str, lang: str) -> bytes:
        if not shutil.which("say"):
            raise RuntimeError("no TTS: set ELEVENLABS_API_KEY (macOS `say` is not available here)")
        with tempfile.TemporaryDirectory() as d:
            wav = Path(d) / "tts.wav"
            voice = ["-v", "Nora", "-r", "165"] if lang == "no" else ["-v", "Samantha", "-r", "165"]
            subprocess.run(["say", *voice, "--data-format=LEI16@16000", "-o", str(wav), text], check=True)
            with wave.open(str(wav)) as w:
                return w.readframes(w.getnframes())

    TELEMETRY = ("co2_co2", "aht20_temperature", "aht20_humidity")

    def watch_button(self):
        """Button presses → push-to-talk; climate samples → POST /api/telemetry (physical, throttled)."""
        last: dict[str, float] = {}
        for ev in self.board.events(keys=["btn_kitchen", *self.TELEMETRY]):
            if ev.key == "btn_kitchen":
                if ev.value in (1, True) and self.mic is None:  # the board mic handles its own presses
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

    def _post(self, path: str, body: dict, timeout: float = 5):
        return self._request(path, body, timeout)[1]

    def _request(self, path: str, body: dict, timeout: float = 5) -> tuple[int, dict | None]:
        req = Request(self.api.rstrip("/") + path, data=json.dumps(body).encode(), method="POST",
                      headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=timeout) as r:
                return r.status, json.load(r)
        except HTTPError as exc:
            print(f"  ! POST {path}: HTTP {exc.code} {exc.read()[:200]!r}", file=sys.stderr)
            return exc.code, None
        except Exception as exc:
            print(f"  ! POST {path}: {exc}", file=sys.stderr)
            return 0, None

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

    def listen(self) -> bytes | None:
        """One push-to-talk take as WAV bytes (None if nothing usable was recorded)."""
        if self.mic is None:  # laptop microphone
            self.press.wait()
            self.press.clear()
            self.show("Lytter... trykk igjen naar du er ferdig", RED)
            self.beep()
            audio = self.stt.record(stop="silence", max_seconds=10, silence_seconds=1.5, stop_event=self.press)
            self.press.clear()
            return _wav_bytes(audio) if len(audio) / 16000 >= MIN_SECONDS else None
        self.mic.started.wait()  # the firmware lights red itself; no beep, it would be recorded
        self.mic.started.clear()
        self.call("office_say", screen_text("Lytter... trykk igjen naar du er ferdig"))
        take = self.mic.next_take(timeout=20)
        if take is None:  # stop line lost: end the recording on the board, then wait for its flush
            self.call("office_mic", "stop")
            take = self.mic.next_take(timeout=3)
        if take is None:
            return None
        rms, peak = take.levels()
        warn = take.warnings()
        print(f"🎙  {take.seconds:.1f} s, rms {rms:.0f}, peak {peak}" + (f"  WARNING: {'; '.join(warn)}" if warn else ""))
        return take.wav() if take.seconds >= MIN_SECONDS else None

    def ask(self, wav: bytes) -> dict | None:
        """Send the clip to the agent; continue the recent conversation when the server allows it."""
        audio, kind = compress(wav)
        body = {"board_id": "kitchen", "audio_b64": base64.b64encode(audio).decode()}
        recent = self.conversation if self.conversation and time.time() - self.conversation[1] < CONTINUE_SECONDS else None
        print(f"  → agent: {len(audio) // 1024} KB {kind}" + (f", continuing {recent[0]}" if recent else ""))
        status, res = self._request("/api/input", body | {"event_id": uuid.uuid4().hex}
                                    | ({"conversation_id": recent[0]} if recent else {}), timeout=90)
        if status == 409 and recent:  # that request already has an action: start a new conversation
            status, res = self._request("/api/input", body | {"event_id": uuid.uuid4().hex}, timeout=90)
        return res

    def handle_one(self):
        wav = self.listen()
        if wav is None:
            answer, color, lang = "Jeg hørte ingenting. Prøv igjen.", "amber", "no"
        else:
            self.show("Tenker...", AMBER)
            res = self.ask(wav)
            if res is None:
                answer, color, lang = "Jeg fikk ikke kontakt med systemet.", "red", "no"
            else:
                command = res.get("command") or {}
                answer = res.get("reply") or command.get("text") or "Registrert."
                color = command.get("color", "green")
                lang = answer_language(answer, None)
                who = (res.get("speaker") or {}).get("display_name")
                print(f"📝 {res.get('prompt', '')!r}  (speaker: {who or 'unknown'}, status: {res.get('status')})")
                done = res.get("status") in {"awaiting_approval", "failed"}
                self.conversation = None if done or not res.get("id") else (res["id"], time.time())
        print(f"💬 {answer}")
        self.show(answer, LEDS.get(color, GREEN))
        self.say(answer, lang)

    def run(self):
        threading.Thread(target=self.watch_button, daemon=True).start()
        threading.Thread(target=self.deliver_actions, daemon=True).start()
        self.call("speaker_set_volume", self.volume)
        self.show("Trykk paa knappen og snakk", BLUE)
        print(f"Ready ({'board' if self.mic else 'laptop'} microphone): press the board button to talk (Ctrl+C to quit).")
        while True:
            self.handle_one()
            self.call("led_kitchen_fill", BLUE)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", help="Board serial port (default: first Atech board found)")
    p.add_argument("--api", default="http://127.0.0.1:3001")
    p.add_argument("--mic", choices=("board", "laptop"), default="board",
                   help="board: the office_station firmware's microphone (default); laptop: the laptop mic")
    p.add_argument("--volume", type=float, default=0.85, help="Board speaker volume 0.0–1.0 (1.0 may distort)")
    args = p.parse_args()
    import board_mic

    if args.mic == "board":
        board, mic, stt = *board_mic.connect(args.port), None
    else:
        from atech import Board
        from stt import SpeechToText
        board, mic, stt = (Board.connect(port=args.port) if args.port else Board.connect()), None, SpeechToText()
    with board:
        try:
            station = Station(board, args.api, mic=mic, stt=stt)
            station.volume = args.volume
            station.run()
        except KeyboardInterrupt:
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
