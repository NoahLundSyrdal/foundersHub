#!/usr/bin/env python3
"""Local speech-to-text for the office assistant (laptop mic → Whisper → text).

Providers: ElevenLabs Scribe (cloud, best Norwegian; set ELEVENLABS_API_KEY in
the repo .env) or local faster-whisper (offline fallback). Handles Norwegian and
English (language auto-detect by default).

Library use:
    from stt import SpeechToText
    stt = SpeechToText()                 # loads the model once (~2 s)
    text = stt.listen()                  # records until silence, returns text
    text = stt.listen(stop="enter")      # push-to-talk: Enter stops recording

CLI:
    python stt.py                        # push-to-talk: Enter to start, Enter to stop
    python stt.py --station kitchen      # also POST the transcript to the API
                                         # (air/temperature/CO2 questions are answered
                                         #  from GET /api/air-quality instead; --speak reads aloud)
    python stt.py --file clip.wav        # transcribe an audio file
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import queue
import subprocess
import wave
import sys
import threading
import time
from urllib.request import Request, urlopen
from uuid import uuid4

import numpy as np

SAMPLE_RATE = 16_000
HALLUCINATIONS = {"thank you", "thanks for watching", "thank you for watching", "takk for at du så på",
                  "takk for meg", "teksting av nicolai winther", "you", "bye"}
STATIONS = ("kitchen", "bathroom", "printer")


def _load_env_key(name: str) -> str | None:
    """Read a key from the environment, else from the repo-root .env (never committed)."""
    if os.environ.get(name):
        return os.environ[name]
    env = Path(__file__).resolve().parents[2] / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip('"') or None
    return None


def _wav_bytes(audio: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


class SpeechToText:
    """provider="elevenlabs" (Scribe v2, cloud, needs ELEVENLABS_API_KEY) or "whisper" (local, offline).
    provider="auto" uses ElevenLabs when a key exists and falls back to Whisper on any error."""

    def __init__(self, model: str = "small", language: str | None = None, provider: str = "auto"):
        self.language = language  # None = auto-detect; "no" = Norwegian; "en" = English
        self.whisper_size = model
        self._whisper = None
        self.eleven_key = _load_env_key("ELEVENLABS_API_KEY")
        self.eleven_model = os.environ.get("ELEVENLABS_STT_MODEL", "scribe_v2")
        if provider == "auto":
            provider = "elevenlabs" if self.eleven_key else "whisper"
        if provider == "elevenlabs" and not self.eleven_key:
            raise RuntimeError("ELEVENLABS_API_KEY is not set (env or repo .env)")
        self.provider = provider
        if provider == "whisper":
            self._load_whisper()

    def _load_whisper(self):
        if self._whisper is None:
            from faster_whisper import WhisperModel
            self._whisper = WhisperModel(self.whisper_size, device="cpu", compute_type="int8")
        return self._whisper

    # ---- recording -------------------------------------------------------
    def record(self, stop: str = "silence", max_seconds: float = 15.0,
               silence_seconds: float = 1.2, threshold: float = 0.015,
               stop_event: threading.Event | None = None) -> np.ndarray:
        """Record mono 16 kHz float32 audio from the default mic.

        stop="silence": ends after `silence_seconds` of quiet once speech was heard.
        stop="enter":   ends when Enter is pressed (or at max_seconds).
        stop_event:     also ends when this is set (e.g. a second press on the board button).
        """
        import sounddevice as sd

        chunks: queue.Queue[np.ndarray] = queue.Queue()
        done = stop_event or threading.Event()
        if stop == "enter":
            threading.Thread(target=lambda: (sys.stdin.readline(), done.set()), daemon=True).start()

        def callback(indata, frames, t, status):
            chunks.put(indata[:, 0].copy())

        audio: list[np.ndarray] = []
        heard_speech, quiet_since, start = False, None, time.monotonic()
        noise: list[float] = []  # first ~0.3 s calibrates the room's noise floor
        loud_run = 0
        self.last_threshold = threshold
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                            blocksize=1600, callback=callback):
            while not done.is_set() and time.monotonic() - start < max_seconds:
                try:
                    chunk = chunks.get(timeout=0.2)
                except queue.Empty:
                    continue
                audio.append(chunk)
                if stop == "silence":
                    level = float(np.sqrt(np.mean(chunk ** 2)))
                    if len(noise) < 3:
                        noise.append(level)
                        threshold = min(max(threshold, float(np.median(noise)) * 2.0), 0.03)
                        self.last_threshold = threshold
                        continue
                    loud = level > threshold
                    loud_run = loud_run + 1 if loud else 0
                    if loud_run >= 3:  # ≥0.3 s of sound = speech, not a single noise blip
                        heard_speech, quiet_since = True, None
                    elif heard_speech and not loud:
                        quiet_since = quiet_since or time.monotonic()
                        if time.monotonic() - quiet_since >= silence_seconds:
                            break
        return np.concatenate(audio) if audio else np.zeros(0, dtype=np.float32)

    # ---- transcription ---------------------------------------------------
    def transcribe(self, audio: np.ndarray | str) -> str:
        """Transcribe a float32 16 kHz array or an audio file path."""
        if isinstance(audio, np.ndarray) and audio.size < SAMPLE_RATE * 0.3:
            return ""
        if self.provider == "elevenlabs":
            try:
                return self._transcribe_elevenlabs(audio)
            except Exception as exc:
                print(f"ElevenLabs failed ({exc}); falling back to local Whisper", file=sys.stderr)
        return self._transcribe_whisper(audio)

    def _transcribe_elevenlabs(self, audio: np.ndarray | str) -> str:
        if isinstance(audio, str):
            data, filename = Path(audio).read_bytes(), Path(audio).name
        else:
            data, filename = _wav_bytes(audio), "speech.wav"
        boundary = uuid4().hex
        fields = {"model_id": self.eleven_model, "tag_audio_events": "false"}
        if self.language:
            fields["language_code"] = {"no": "nor", "en": "eng"}.get(self.language, self.language)
        body = b"".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
            for k, v in fields.items())
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
                 f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        req = Request("https://api.elevenlabs.io/v1/speech-to-text", data=body, method="POST",
                      headers={"xi-api-key": self.eleven_key,
                               "Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urlopen(req, timeout=20) as resp:
            result = json.load(resp)
        code = str(result.get("language_code") or "").lower()
        self.last_language = "en" if code.startswith("en") else "no" if code[:2] in ("no", "nb", "nn", "da", "sv") else None
        return result.get("text", "").strip()

    def _transcribe_whisper(self, audio: np.ndarray | str) -> str:
        if isinstance(audio, str):  # decode via ffmpeg (avoids PyAV version issues)
            raw = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", audio, "-f", "s16le",
                                  "-ac", "1", "-ar", str(SAMPLE_RATE), "-"], capture_output=True, check=True).stdout
            audio = np.frombuffer(raw, "<i2").astype(np.float32) / 32768
        model = self._load_whisper()
        language = self.language
        if language is None:  # only choose between Norwegian and English; noise makes free detection wander
            try:
                _, _, probs = model.detect_language(audio, vad_filter=True)
                p = dict(probs)
                language = "en" if p.get("en", 0) > p.get("no", 0) + p.get("nn", 0) else "no"
            except Exception:
                language = "no"
        self.last_language = language
        segments, _info = model.transcribe(
            audio, language=language, beam_size=1, vad_filter=True,
            condition_on_previous_text=False)
        text = " ".join(s.text.strip() for s in segments).strip()
        # Whisper invents these on silence/noise; treat them as "nothing heard".
        if text.lower().strip(" .!") in HALLUCINATIONS:
            return ""
        return text

    def listen(self, stop: str = "silence", **record_kwargs) -> str:
        return self.transcribe(self.record(stop=stop, **record_kwargs))


AIR_WORDS = ("luft", "air", "temperat", "co2", "co 2", "varmt", "kaldt", "warm", "cold", "hot",
             "fukt", "humid", "stuffy", "tett", "innestengt", "ventil")


def is_air_question(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in AIR_WORDS)


def air_quality(api: str) -> dict:
    """GET /api/air-quality → {level, readings, issues, summary, stale}."""
    with urlopen(api.rstrip("/") + "/api/air-quality", timeout=5) as resp:
        return json.load(resp)


NORWEGIAN_HINTS = (" er ", " det ", " hvor", " luft", " varmt", " kaldt", " vi ", " jeg ", "dårlig", " ikke ", " og ")


def answer_language(text: str, detected: str | None) -> str:
    """'no' or 'en' for the reply: the STT's detected language, else a word heuristic."""
    if detected in ("no", "en"):
        return detected
    t = f" {text.lower()} "
    return "no" if any(h in t for h in NORWEGIAN_HINTS) else "en"


def air_summary(aq: dict, lang: str = "en") -> str:
    """Spoken air-quality answer in Norwegian or English, built from the API's readings."""
    if lang != "no":
        return aq["summary"]
    r = aq.get("readings") or {}
    if aq.get("level") == "unknown" or not any(r.values()):
        return "Jeg har ingen målinger ennå. Sjekk at sensoren er koblet til."
    verdict = {"good": "Luftkvaliteten er god", "moderate": "Luftkvaliteten er grei, men ikke helt optimal",
               "poor": "Luftkvaliteten er dårlig"}.get(aq["level"], "Luftkvaliteten er ukjent")
    fmt = lambda v, d: f"{v:.{d}f}".replace(".", ",")
    parts, tips = [], []
    if r.get("temperature_c"):
        t = r["temperature_c"]["value"]; parts.append(f"{fmt(t, 1)} grader")
        if t > 24: tips.append("det er for varmt, skru ned varmen eller luft ut")
        elif t < 19: tips.append("det er litt kaldt")
    if r.get("humidity_pct"):
        h = r["humidity_pct"]["value"]; parts.append(f"{fmt(h, 0)} prosent luftfuktighet")
        if h < 25: tips.append("luften er tørr")
        elif h > 60: tips.append("luften er fuktig")
    if r.get("co2_ppm"):
        c = r["co2_ppm"]["value"]; parts.append(f"CO2 på {fmt(c, 0)} ppm")
        if c > 1000: tips.append("CO2-nivået er høyt, åpne et vindu")
    text = f"{verdict}: {', '.join(parts)}."
    if tips:
        text += " " + "; ".join(tips).capitalize() + "."
    if aq.get("stale"):
        text += " Merk at målingene er mer enn to minutter gamle."
    return text


def speak(text: str, lang: str = "en") -> None:
    """Read text aloud with a matching macOS voice (Nora for Norwegian)."""
    subprocess.run(["say", "-v", "Nora", text] if lang == "no" else ["say", text], check=False)


def post_transcript(api: str, station: str, text: str) -> dict:
    """Send the transcript as a canonical speech_transcript DeviceEvent."""
    event = {
        "event_id": str(uuid4()), "station_id": station, "type": "speech_transcript",
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "payload": {"text": text[:2000]},
    }
    req = Request(api.rstrip("/") + "/api/device-events", data=json.dumps(event).encode(),
                  headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=30) as resp:
        return json.load(resp)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--provider", choices=("auto", "elevenlabs", "whisper"), default="auto",
                   help="auto = ElevenLabs if ELEVENLABS_API_KEY is set, else local Whisper")
    p.add_argument("--model", default="small", help="Whisper size: tiny/base/small/medium/large-v3")
    p.add_argument("--language", default=None, help="no / en; default auto-detect")
    p.add_argument("--stop", choices=("silence", "enter"), default="enter",
                   help="enter = push-to-talk (robust in noisy rooms); silence = auto-stop")
    p.add_argument("--file", help="Transcribe an audio file instead of the mic")
    p.add_argument("--station", choices=STATIONS, help="POST each transcript to the API for this station")
    p.add_argument("--api", default="http://127.0.0.1:3001")
    p.add_argument("--air", action="store_true", help="Answer air/temperature/CO2 questions even without --station")
    p.add_argument("--speak", action="store_true", help="Read air-quality answers aloud (macOS say)")
    p.add_argument("--identify", action="store_true",
                   help="Show who is speaking (enroll first: python speaker_id.py enroll NAME). Informational only")
    args = p.parse_args()

    t0 = time.monotonic()
    stt = SpeechToText(args.model, args.language, args.provider)
    print(f"STT provider: {stt.provider} (ready in {time.monotonic() - t0:.1f}s)", file=sys.stderr)
    sid = None
    if args.identify:
        from speaker_id import SpeakerID
        sid = SpeakerID()
        print(f"Speaker ID: {len(sid.people)} enrolled ({', '.join(sid.people) or 'none'})", file=sys.stderr)

    if args.file:
        print(stt.transcribe(args.file))
        return 0

    while True:
        try:
            input("\n⏎  Press Enter and speak (Ctrl+C to quit) ")
        except (EOFError, KeyboardInterrupt):
            return 0
        print("🎙  Listening…" + (" press Enter to stop" if args.stop == "enter" else " (stops on silence)"))
        audio = stt.record(stop=args.stop)
        t1 = time.monotonic()
        text = stt.transcribe(audio)
        print(f"📝 {text or '(nothing heard)'}   [{len(audio) / SAMPLE_RATE:.1f}s audio, "
              f"transcribed in {time.monotonic() - t1:.1f}s]")
        if sid and text:
            who = sid.identify(audio)
            print(f"👤 {who.name or 'unknown speaker'} (score {who.score:.2f}"
                  + (f", closest {who.best}" if not who.name and who.best else "") + ")")
        if text and is_air_question(text) and (args.station or args.air):
            try:
                aq = air_quality(args.api)
                lang = answer_language(text, getattr(stt, "last_language", None))
                answer = air_summary(aq, lang)
                print(f"🌡  [{aq['level']}] {answer}")
                if args.speak:
                    speak(answer, lang)
            except Exception as exc:
                print(f"→ Air-quality lookup failed: {exc}", file=sys.stderr)
        elif text and args.station:
            try:
                result = post_transcript(args.api, args.station, text)
                print("→ API:", json.dumps(result.get("command"), ensure_ascii=False))
            except Exception as exc:  # demo tool: report and keep going
                print(f"→ API failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
