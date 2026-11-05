#!/usr/bin/env python3
"""Speaker identification for the office assistant (who said this command?).

Enrolled people get a voiceprint: an embedding computed locally by a small ONNX
speaker model (sherpa-onnx, WeSpeaker ResNet34 by default). A new utterance is embedded the
same way and matched by cosine similarity. Below the threshold → unknown.

Identity is informational only (e.g. "requested by"). It must never grant
purchasing authority; approval stays in the normal approval flow. Voiceprints
are biometric personal data: enroll only people who consent, keep them in the
ignored .data/ directory, and delete on request (`remove`).

Library use:
    from speaker_id import SpeakerID
    sid = SpeakerID()
    sid.enroll("Anders", audio)                # float32 16 kHz mono array(s)
    match = sid.identify(audio)                # Match(name|None, score, best)

CLI:
    python speaker_id.py enroll Anders         # read 3 sentences aloud at the mic
    python speaker_id.py enroll Anders --file a.wav --file b.wav
    python speaker_id.py identify              # speak, see who it thinks you are
    python speaker_id.py identify --file x.wav
    python speaker_id.py enroll Anders --seconds 20   # timed, no key presses
    python speaker_id.py identify --seconds 4
    python speaker_id.py list | remove Anders
    python speaker_id.py team                  # guided session: Leo, Noah, Jørgen, Anders + test
    python speaker_id.py room                  # re-record room background when the noise changes
    python speaker_id.py calibrate             # re-learn threshold from stored samples
    python speaker_id.py selftest              # macOS `say` voices as fake people
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import numpy as np

SAMPLE_RATE = 16_000
ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = ROOT / ".data" / "models"
DB_PATH = ROOT / ".data" / "voiceprints.json"
MODEL_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
DEFAULT_MODEL = "wespeaker_en_voxceleb_resnet34_LM.onnx"  # beat CAM++ clearly in our tests
DEFAULT_THRESHOLD = 0.7   # cosine; calibrate on real people: raise → fewer false matches, lower → fewer "unknown"
MIN_MARGIN = 0.05          # best must beat runner-up by this much, else ambiguous → unknown
MIN_SECONDS = 1.0
NORMALIZE_FROM = 3         # with ≥3 voiceprints (people + room), subtract their mean (cancels mic/room sound)
ROOM = "(room)"            # background-noise pseudo-speaker: never a name, always "unknown"
TEAM = ("Leo", "Noah", "Jørgen", "Anders")
TEAM_SCRIPT = (
    "Hei, jeg heter {name}. Jeg jobber i Founders Hub, og i dag tester vi kontorassistenten. "
    "Vi trenger mer kaffe på kjøkkenet, skriveren er tom for papir, og det er litt for varmt i møterommet. "
    "Hello, this is {name}. Could you check the air quality, order some milk, "
    "and tell me if the coffee machine is working? Én, to, tre, fire, fem, seks, sju, åtte, ni, ti."
)

ENROLL_PROMPTS = (
    "Hei, jeg heter {name}, og jeg jobber på kontoret her i Founders Hub.",
    "Vi trenger mer kaffe på kjøkkenet, og skriveren er tom for papir.",
    "Hello, this is {name}. Could you check the air quality in the meeting room?",
)


@dataclass
class Match:
    name: str | None      # None = unknown speaker
    score: float          # similarity to `best`
    best: str | None      # closest enrolled person, even when below threshold

    def as_dict(self) -> dict:
        return {"speaker": self.name, "confidence": round(self.score, 3)}


def _load_audio(path: str) -> np.ndarray:
    raw = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", path, "-f", "s16le",
                          "-ac", "1", "-ar", str(SAMPLE_RATE), "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, "<i2").astype(np.float32) / 32768


def _trim_silence(audio: np.ndarray, frame: int = 480) -> np.ndarray:
    """Drop quiet 30 ms frames so embeddings reflect voice, not room noise."""
    n = len(audio) // frame
    if n < 4:
        return audio
    frames = audio[: n * frame].reshape(n, frame)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    # noise floor from the quietest frames, but never above 10 % of the loud speech level
    level = max(0.004, min(np.percentile(rms, 10) * 2.5, np.percentile(rms, 95) * 0.1))
    keep = rms > level
    return frames[keep].reshape(-1) if keep.sum() >= 10 else audio


def _unit(v: np.ndarray) -> np.ndarray:
    return v / (np.linalg.norm(v) + 1e-9)


class SpeakerID:
    def __init__(self, model: str = DEFAULT_MODEL, db_path: Path = DB_PATH,
                 threshold: float | None = None):
        import sherpa_onnx

        path = MODEL_DIR / model
        if not path.exists():
            MODEL_DIR.mkdir(parents=True, exist_ok=True)
            print(f"Downloading speaker model {model} …", file=sys.stderr)
            subprocess.run(["curl", "-sSfL", "-o", str(path), MODEL_URL + model], check=True)
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(path), num_threads=2, provider="cpu")
        if not cfg.validate():
            raise RuntimeError(f"Invalid speaker model config: {path}")
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        self.model = model
        self._threshold = threshold            # explicit override; else calibrated; else default
        self.db_path = Path(db_path)
        self.people: dict[str, list[list[float]]] = {}
        self.room: list[list[float]] = []      # embeddings of the room with nobody talking to the laptop
        self.calibrated: float | None = None   # threshold learned by calibrate()
        self.normalize = True                  # calibrate() picks whichever mode separates the team best
        if self.db_path.exists():
            data = json.loads(self.db_path.read_text())
            if data.get("model") == model:
                self.people = data.get("people", {})
                self.calibrated = data.get("threshold")
                self.normalize = data.get("normalize", True)
                self.room = data.get("room", [])
            else:
                print(f"Voiceprints were made with {data.get('model')}; re-enroll for {model}.", file=sys.stderr)

    # ---- embeddings -------------------------------------------------------
    def embed(self, audio: np.ndarray) -> np.ndarray | None:
        audio = _trim_silence(np.asarray(audio, dtype=np.float32))
        if len(audio) < SAMPLE_RATE * MIN_SECONDS:
            return None
        stream = self._extractor.create_stream()
        stream.accept_waveform(SAMPLE_RATE, audio)
        stream.input_finished()
        if not self._extractor.is_ready(stream):
            return None
        return _unit(np.array(self._extractor.compute(stream), dtype=np.float32))

    @property
    def threshold(self) -> float:
        return self._threshold if self._threshold is not None else (self.calibrated or DEFAULT_THRESHOLD)

    @threshold.setter
    def threshold(self, value: float | None) -> None:
        self._threshold = value

    def _groups(self, people: dict) -> dict[str, list]:
        return {**people, ROOM: self.room} if self.room else dict(people)

    def _mean(self, people: dict) -> np.ndarray | None:
        """Mean of everyone's voiceprint plus the room (each weighted equally). All were recorded
        on the same mic in the same room, so this direction is 'laptop + room', not voice.
        In a noisy hackathon room the raw embeddings are dominated by it; removing it is key."""
        groups = [e for e in self._groups(people).values() if e]
        if not self.normalize or len(groups) < NORMALIZE_FROM:
            return None
        return np.mean([np.mean(np.array(e), axis=0) for e in groups], axis=0)

    def _norm(self, emb: np.ndarray, mean: np.ndarray | None) -> np.ndarray:
        return emb if mean is None else _unit(emb - mean)

    def _centroids(self, people: dict | None = None) -> tuple[dict[str, np.ndarray], np.ndarray | None]:
        people = self.people if people is None else people
        mean = self._mean(people)
        return {n: _unit(np.mean([self._norm(np.array(x), mean) for x in e], axis=0))
                for n, e in self._groups(people).items() if e}, mean

    def set_room(self, audio: np.ndarray, parts: int = 4) -> int:
        """Store the room's background sound (nobody talking to the laptop). Replaces the old one."""
        embs = [e for c in np.array_split(audio, parts) if (e := self.embed(c)) is not None]
        if embs:
            self.room = [e.round(5).tolist() for e in embs]
            self._save()
        return len(embs)

    # ---- enrollment -------------------------------------------------------
    def enroll(self, name: str, *clips: np.ndarray) -> int:
        """Add voice samples for `name`. Returns how many usable samples were stored."""
        added = 0
        for clip in clips:
            emb = self.embed(clip)
            if emb is not None:
                self.people.setdefault(name, []).append(emb.round(5).tolist())
                added += 1
        if added:
            self._save()
        return added

    def remove(self, name: str) -> bool:
        found = self.people.pop(name, None) is not None
        self._save()
        return found

    def _save(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.db_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"model": self.model, "threshold": self.calibrated, "normalize": self.normalize,
                                   "room": self.room, "people": self.people},
                                  ensure_ascii=False))
        tmp.replace(self.db_path)

    # ---- identification ---------------------------------------------------
    def scores(self, audio: np.ndarray) -> dict[str, float]:
        emb = self.embed(audio)
        if emb is None:
            return {}
        return self._rank(emb, *self._centroids())

    def _rank(self, emb: np.ndarray, centroids: dict, mean) -> dict[str, float]:
        emb = self._norm(emb, mean)
        return dict(sorted(((n, float(c @ emb)) for n, c in centroids.items()), key=lambda kv: -kv[1]))

    def calibrate(self) -> dict:
        """Try raw and team-normalized scoring; keep the mode that separates people best (d')."""
        results = {}
        for mode in (False, True):
            self.normalize = mode
            results[mode] = self._calibrate_mode()
        ok = {m: r for m, r in results.items() if "error" not in r}
        if not ok:
            return results[True]
        self.normalize = max(ok, key=lambda m: ok[m]["d_prime"])
        best = ok[self.normalize]
        self.calibrated = best["threshold"]
        self._save()
        return {"mode": "normalized" if self.normalize else "raw", **best,
                "other_mode_d_prime": ok.get(not self.normalize, {}).get("d_prime")}

    def _calibrate_mode(self) -> dict:
        """Leave-one-out over stored samples: each sample is scored against everyone's voiceprint
        built WITHOUT it. Threshold goes between same-person and other-person scores."""
        genuine, impostor, correct, total = [], [], 0, 0
        for name, samples in self.people.items():
            if len(samples) < 2:
                continue
            for i, s in enumerate(samples):
                held_out = {**self.people, name: samples[:i] + samples[i + 1:]}
                ranked = self._rank(np.array(s), *self._centroids(held_out))
                genuine.append(ranked[name])
                impostor += [v for n, v in ranked.items() if n != name]
                correct += next(iter(ranked)) == name
                total += 1
        if not genuine or not impostor:
            return {"error": "need ≥2 people with ≥2 samples each"}
        g10, imax = float(np.percentile(genuine, 10)), float(max(impostor))  # impostors include the room
        # midway between the worst-case other person and the low end of the right person
        d_prime = (np.mean(genuine) - np.mean(impostor)) / np.sqrt((np.var(genuine) + np.var(impostor)) / 2 + 1e-9)
        return {"threshold": round(float(np.clip((g10 + imax) / 2, 0.05, 0.9)), 3),
                "d_prime": round(float(d_prime), 2), "leave_one_out_top1": f"{correct}/{total}",
                "same_person": [round(min(genuine), 2), round(float(np.median(genuine)), 2)],
                "other_person": [round(float(np.median(impostor)), 2), round(imax, 2)],
                "separated": min(genuine) > imax}

    def identify(self, audio: np.ndarray) -> Match:
        ranked = list(self.scores(audio).items())
        if not ranked:
            return Match(None, 0.0, None)
        best, score = ranked[0]
        runner_up = ranked[1][1] if len(ranked) > 1 else -1.0
        ok = best != ROOM and score >= self.threshold and score - runner_up >= MIN_MARGIN
        closest = next((n for n, _ in ranked if n != ROOM), None)
        return Match(best if ok else None, score, closest)


# ---- CLI ------------------------------------------------------------------
def _record_until_enter(prompt: str) -> np.ndarray:
    import sounddevice as sd

    chunks: list[np.ndarray] = []
    input(f"\n{prompt}\n⏎  Press Enter, read it aloud, then press Enter again ")
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        callback=lambda d, f, t, s: chunks.append(d[:, 0].copy())):
        print("🎙  Recording… (Enter to stop)")
        input()
    return np.concatenate(chunks) if chunks else np.zeros(0, np.float32)


def _record_seconds(seconds: float) -> np.ndarray:
    import sounddevice as sd

    print(f"🎙  Recording {seconds:.0f} s — speak now…", file=sys.stderr, flush=True)
    audio = sd.rec(int(seconds * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype="float32")
    sd.wait()
    return audio[:, 0]


def _split(audio: np.ndarray, parts: int) -> list[np.ndarray]:
    return [a for a in np.array_split(audio, parts) if len(a) >= SAMPLE_RATE * 2]


def _team_session(sid: "SpeakerID", names: list[str], seconds: float, reset: bool, room: bool = True) -> int:
    """Guided session: enroll each person in turn, then a test round, then calibrate."""
    import sounddevice as sd

    def countdown():
        for n in (3, 2, 1):
            print(f"   {n}…", end=" ", flush=True); time.sleep(1)
        print()

    print("\n=== Voice enrollment session ===")
    print(f"Order: {' → '.join(names)}. About {seconds + 15:.0f} s each, then a 5 s test per person.")
    print("Tips: sit where you'll normally talk to the laptop, normal voice, one person at a time.\n")
    if room:
        input("[room] First 12 s of normal room sound: nobody talks TO the laptop (background chatter is fine). Enter ")
        countdown()
        print(f"✓ room: {sid.set_room(_record_seconds(12))} background samples stored\n")
    for i, name in enumerate(names, 1):
        input(f"[{i}/{len(names)}] {name}: sit at the laptop and press Enter ")
        print("Read this aloud (keep talking until the timer ends, repeat if you finish early):\n")
        print("   " + TEAM_SCRIPT.format(name=name) + "\n")
        countdown()
        audio = _record_seconds(seconds)
        if reset:
            sid.people.pop(name, None)
        n = sid.enroll(name, *_split(audio, max(2, int(seconds // 6))))
        print(f"✓ {name}: {n} samples stored\n" if n else f"✗ {name}: too quiet, try again later\n")

    print("=== Calibration ===")
    print(json.dumps(sid.calibrate(), ensure_ascii=False))
    print(f"\n=== Test round: everyone says something new, e.g. 'vi trenger mer kaffe' ===")
    ok = 0
    for name in names:
        input(f"{name}: press Enter, then speak for 5 s ")
        countdown()
        m = sid.identify(_record_seconds(5))
        ok += m.name == name
        print(f"   → {m.name or 'unknown'} (score {m.score:.2f}, closest {m.best}) "
              f"{'✓' if m.name == name else '✗'}\n")
    print(f"Result: {ok}/{len(names)} recognised. Threshold {sid.threshold:.2f}. "
          "Rerun `team --names X` to redo one person.")
    return 0


def _selftest(sid_args: dict) -> int:
    """Enroll a few macOS voices as fake people, then identify unseen sentences."""
    voices = ["Nora", "Daniel", "Samantha", "Karen", "Fred"]
    enroll_text = ["Hei, jeg jobber på kontoret, og jeg liker god kaffe om morgenen.",
                   "The printer on the second floor is out of paper again today."]
    test_text = ["Vi trenger mer kaffe på kjøkkenet.", "How is the air quality in here right now?"]
    with tempfile.TemporaryDirectory() as tmp:
        def say(voice: str, text: str) -> np.ndarray:
            out = Path(tmp) / f"{voice}{abs(hash(text))}.aiff"
            subprocess.run(["say", "-v", voice, "-o", str(out), text], check=True)
            return _load_audio(str(out))

        available = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
        voices = [v for v in voices if any(line.startswith(v + " ") for line in available.splitlines())]
        sid = SpeakerID(db_path=Path(tmp) / "db.json", **sid_args)
        enrolled, impostor = voices[:-1], voices[-1]
        for v in enrolled:
            sid.enroll(v, *(say(v, t) for t in enroll_text))
        correct = total = 0
        for v in voices:
            for t in test_text:
                t0 = time.monotonic()
                m = sid.identify(say(v, t))
                expected = v if v in enrolled else None
                correct += m.name == expected
                total += 1
                print(f"{v:9s} → {str(m.name):9s} score={m.score:.2f} best={m.best}  "
                      f"{'✓' if m.name == expected else '✗'}  ({(time.monotonic() - t0) * 1000:.0f} ms)")
        print(f"\n{correct}/{total} correct (enrolled: {', '.join(enrolled)}; impostor: {impostor})")
        return 0 if correct == total else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--threshold", type=float, default=None, help="Override the calibrated threshold")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("enroll"); e.add_argument("name"); e.add_argument("--file", action="append")
    e.add_argument("--seconds", type=float, help="Record one timed clip (no Enter presses), split into samples")
    i = sub.add_parser("identify"); i.add_argument("--file"); i.add_argument("--seconds", type=float)
    sub.add_parser("list")
    sub.add_parser("calibrate")
    t = sub.add_parser("team", help="Guided enrollment of the whole team + test round")
    t.add_argument("--names", nargs="+", default=list(TEAM))
    t.add_argument("--seconds", type=float, default=30)
    t.add_argument("--keep", action="store_true", help="Add to existing samples instead of replacing")
    t.add_argument("--no-room", action="store_true", help="Skip re-recording the room background")
    rm = sub.add_parser("room", help="Re-record the room background (nobody talking to the laptop)")
    rm.add_argument("--seconds", type=float, default=12)
    r = sub.add_parser("remove"); r.add_argument("name")
    sub.add_parser("selftest")
    args = p.parse_args()
    sid_args = {"model": args.model, "threshold": args.threshold}

    if args.cmd == "selftest":
        return _selftest(sid_args)
    sid = SpeakerID(**sid_args)
    if args.cmd == "team":
        return _team_session(sid, args.names, args.seconds, reset=not args.keep, room=not args.no_room)
    if args.cmd == "room":
        print(f"room: {sid.set_room(_record_seconds(args.seconds))} samples; recalibrating…")
        print(json.dumps(sid.calibrate(), ensure_ascii=False))
        return 0
    if args.cmd == "calibrate":
        print(json.dumps(sid.calibrate(), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "list":
        for name, samples in sid.people.items():
            print(f"{name}: {len(samples)} samples")
        print(f"room: {len(sid.room)} samples; mode: {'normalized' if sid._mean(sid.people) is not None else 'raw'}; "
              f"threshold: {sid.threshold:.2f} ({'calibrated' if sid.calibrated else 'default'})")
        return 0
    if args.cmd == "remove":
        print("removed" if sid.remove(args.name) else "not enrolled")
        return 0
    if args.cmd == "enroll":
        if args.file:
            clips = [_load_audio(f) for f in args.file]
        elif args.seconds:
            clips = _split(_record_seconds(args.seconds), max(1, int(args.seconds // 6)))
        else:
            clips = [_record_until_enter("Say: " + t.format(name=args.name)) for t in ENROLL_PROMPTS]
        n = sid.enroll(args.name, *clips)
        print(f"Enrolled {args.name}: {n}/{len(clips)} usable samples "
              f"({len(sid.people.get(args.name, []))} total)")
        return 0 if n else 1
    # identify
    audio = (_load_audio(args.file) if args.file else _record_seconds(args.seconds) if args.seconds
             else _record_until_enter("Say anything"))
    print(json.dumps(sid.scores(audio), indent=2))
    m = sid.identify(audio)
    print(f"→ {m.name or 'unknown'} (score {m.score:.2f}, closest {m.best})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
