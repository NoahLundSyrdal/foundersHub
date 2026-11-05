"""Speaker identification service on Modal. Owner: voice.

Closed set of enrolled team members + "unknown". Runs next to transcription; it never transcribes.

    modal deploy apps/voiceid/app.py      # from the repo root

Model: WeSpeaker ResNet293-LM (VoxCeleb, CC BY 4.0) via sherpa-onnx, Silero VAD to drop silence.
Enrollments (voice embeddings = biometric data) live in the Modal Volume `office-voiceid`.
Identity is informational only: it must never grant purchasing authority.
"""

import os
import modal

MODEL = "wespeaker_en_voxceleb_resnet293_LM.onnx"
MODEL_SHA256 = "f65dbc820e534eef64ae12d1e289e20244d60e60f7f00d7b092092b1c458be2e"
VAD_SHA256 = "9e2449e1087496d8d4caba907f23e0bd3f78d91fa552479bb9c23ac09cbb1fd6"
RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("ffmpeg", "curl")
    .pip_install("sherpa-onnx==2.0.1", "numpy>=2", "fastapi[standard]>=0.115")
    .run_commands(
        "mkdir -p /models",
        f"curl -fsSL -o /models/{MODEL} {RELEASES}/speaker-recongition-models/{MODEL}",
        f"curl -fsSL -o /models/silero_vad.onnx {RELEASES}/asr-models/silero_vad.onnx",
        f"echo '{MODEL_SHA256}  /models/{MODEL}' | sha256sum -c",
        f"echo '{VAD_SHA256}  /models/silero_vad.onnx' | sha256sum -c",
    )
)


COHORT_URL = "https://www.openslr.org/resources/12/dev-clean.tar.gz"  # LibriSpeech dev-clean, CC BY 4.0
COHORT_PER_SPEAKER = 4


def build_cohort_mean():
    """Mean voiceprint of 40 LibriSpeech speakers, computed once at image build.

    Raw WeSpeaker embeddings share a large common component, so raw cosine scores every pair of voices
    high (0.6-0.9). WeSpeaker's own scoring subtracts a population mean first; this is that mean.
    """
    import collections
    import io
    import tarfile
    import urllib.request

    import numpy as np
    import sherpa_onnx
    import soundfile as sf

    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
        sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=f"/models/{MODEL}", num_threads=8))
    taken, embeddings = collections.Counter(), []
    with urllib.request.urlopen(COHORT_URL, timeout=600) as r, tarfile.open(fileobj=r, mode="r|gz") as tar:
        for member in tar:  # LibriSpeech/dev-clean/<speaker>/<chapter>/<utterance>.flac
            if not member.name.endswith(".flac") or taken[speaker := member.name.split("/")[2]] >= COHORT_PER_SPEAKER:
                continue
            samples, sr = sf.read(io.BytesIO(tar.extractfile(member).read()), dtype="float32")
            stream = extractor.create_stream()
            stream.accept_waveform(sample_rate=sr, waveform=samples[: 8 * sr])
            stream.input_finished()
            e = np.asarray(extractor.compute(stream), dtype=np.float32)
            embeddings.append(e / np.linalg.norm(e))
            taken[speaker] += 1
    assert len(taken) >= 30, f"cohort too small: {len(taken)} speakers"
    np.save("/models/cohort_mean.npy", np.mean(embeddings, axis=0))
    print(f"cohort: {len(taken)} speakers, {len(embeddings)} utterances")


image = image.pip_install("soundfile>=0.12").run_function(build_cohort_mean, cpu=8.0, memory=8192, timeout=1800)

app = modal.App("office-voiceid", image=image)
volume = modal.Volume.from_name("office-voiceid", create_if_missing=True)

SR = 16_000
DATA = "/data/speakers.json"


def _env(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


THREADS = 8


@app.cls(
    cpu=8.0,  # deliberately generous: ResNet293 is ~28 GFLOPs per window
    memory=16384,
    volumes={"/data": volume},
    secrets=[modal.Secret.from_name("office-voiceid")],
    max_containers=1,  # enrollments are held in memory; one writer keeps them consistent
    # Demo: `VOICEID_MIN_CONTAINERS=1 modal deploy ...` keeps one container warm (no cold start);
    # redeploy without it afterwards so it scales to zero.
    min_containers=int(os.environ.get("VOICEID_MIN_CONTAINERS", "0")),
    scaledown_window=1200,
)
@modal.concurrent(max_inputs=4)
class VoiceID:
    @modal.enter()
    def load(self):
        import json
        import threading

        import numpy as np
        import sherpa_onnx

        self.np, self.sherpa = np, sherpa_onnx
        self.lock = threading.Lock()
        self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=f"/models/{MODEL}", num_threads=THREADS)
        )
        self.mean = np.load("/models/cohort_mean.npy")  # population mean, subtracted before scoring
        self.threshold = _env("VOICEID_THRESHOLD", 0.7)  # min centred cosine to the best speaker
        self.margin = _env("VOICEID_MARGIN", 0.1)  # best must beat runner-up by this much
        self.min_speech = _env("VOICEID_MIN_SPEECH_S", 1.0)
        self.min_enroll_speech = _env("VOICEID_MIN_ENROLL_SPEECH_S", 10.0)
        self.speakers: dict[str, dict] = {}
        if os.path.exists(DATA):
            with open(DATA) as f:
                self.speakers = json.load(f)
        self.centroids = {n: self._centroid(s["embeddings"]) for n, s in self.speakers.items()}

    # --- audio -------------------------------------------------------------------------------

    def _decode(self, audio: bytes):
        """Any container/codec ffmpeg reads → 16 kHz mono float32. A temp file keeps m4a/mp4 seekable."""
        import subprocess
        import tempfile

        with tempfile.NamedTemporaryFile() as f:
            f.write(audio)
            f.flush()
            out = subprocess.run(
                ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", f.name,
                 "-ac", "1", "-ar", str(SR), "-f", "f32le", "pipe:1"],
                capture_output=True, timeout=30,
            )
        if out.returncode != 0:
            raise ValueError(f"could not decode audio: {out.stderr.decode(errors='replace')[-200:]}")
        return self.np.frombuffer(out.stdout, dtype=self.np.float32)

    def _speech(self, samples):
        """Concatenated speech-only samples (Silero VAD)."""
        cfg = self.sherpa.VadModelConfig()
        cfg.silero_vad.model = "/models/silero_vad.onnx"
        cfg.silero_vad.min_silence_duration = 0.25
        cfg.silero_vad.min_speech_duration = 0.25
        cfg.silero_vad.max_speech_duration = 20
        cfg.sample_rate = SR
        vad = self.sherpa.VoiceActivityDetector(cfg, buffer_size_in_seconds=180)
        window = cfg.silero_vad.window_size
        chunks = []
        for i in range(0, len(samples), window):
            vad.accept_waveform(samples[i : i + window])
            while not vad.empty():
                chunks.append(self.np.asarray(vad.front.samples, dtype=self.np.float32))
                vad.pop()
        vad.flush()
        while not vad.empty():
            chunks.append(self.np.asarray(vad.front.samples, dtype=self.np.float32))
            vad.pop()
        return self.np.concatenate(chunks) if chunks else self.np.zeros(0, dtype=self.np.float32)

    def _embed(self, samples):
        stream = self.extractor.create_stream()
        stream.accept_waveform(sample_rate=SR, waveform=samples)
        stream.input_finished()
        e = self.np.asarray(self.extractor.compute(stream), dtype=self.np.float32)
        return e / self.np.linalg.norm(e)

    def _center(self, e):
        """Stored embeddings stay raw (unit-norm); centring happens at scoring time, so the mean can change."""
        c = self.np.asarray(e, dtype=self.np.float32) - self.mean
        return c / self.np.linalg.norm(c, axis=-1, keepdims=True)

    def _centroid(self, embeddings):
        c = self._center(embeddings).mean(axis=0)
        return c / self.np.linalg.norm(c)

    # --- operations --------------------------------------------------------------------------

    def enroll(self, name: str, audio: bytes, append: bool) -> dict:
        import json
        import time

        speech = self._speech(self._decode(audio))
        seconds = len(speech) / SR
        if seconds < (self.min_speech if append else self.min_enroll_speech):
            raise ValueError(f"only {seconds:.1f}s of speech detected; record a longer clip")
        win, hop = 3 * SR, int(1.5 * SR)  # one embedding per 3 s window: robust centroid + spread
        starts = range(0, max(len(speech) - win, 0) + 1, hop)
        new = [self._embed(speech[s : s + win]).tolist() for s in starts]
        with self.lock:
            old = self.speakers.get(name) if append else None
            entry = {
                "embeddings": (old["embeddings"] if old else []) + new,
                "speech_seconds": round((old["speech_seconds"] if old else 0) + seconds, 1),
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
            self.speakers[name] = entry
            self.centroids[name] = self._centroid(entry["embeddings"])
            self._save(json)
        return {"name": name, "segments": len(entry["embeddings"]), "speech_seconds": entry["speech_seconds"]}

    def delete(self, name: str) -> bool:
        import json

        with self.lock:
            if self.speakers.pop(name, None) is None:
                return False
            self.centroids.pop(name, None)
            self._save(json)
        return True

    def _save(self, json) -> None:
        tmp = DATA + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.speakers, f)
        os.replace(tmp, DATA)
        volume.commit()

    def identify(self, audio: bytes) -> dict:
        import time

        t0 = time.perf_counter()
        samples = self._decode(audio)
        t1 = time.perf_counter()
        speech = self._speech(samples)[: 20 * SR]
        t2 = time.perf_counter()
        seconds = round(len(speech) / SR, 2)
        result = {"speaker": None, "score": None, "margin": None, "scores": {}, "speech_seconds": seconds,
                  "timing_ms": {"decode": round((t1 - t0) * 1000), "vad": round((t2 - t1) * 1000)}}
        centroids = dict(self.centroids)
        if not centroids:
            return result | {"reason": "no_enrollments"}
        if seconds < self.min_speech:
            return result | {"reason": "too_short"}
        e = self._center(self._embed(speech))
        result["timing_ms"]["embed"] = round((time.perf_counter() - t2) * 1000)
        scores = sorted(((float(c @ e), n) for n, c in centroids.items()), reverse=True)
        best, name = scores[0]
        margin = best - scores[1][0] if len(scores) > 1 else best
        result |= {"score": round(best, 4), "margin": round(margin, 4),
                   "scores": {n: round(s, 4) for s, n in scores}}
        if best < self.threshold:
            return result | {"reason": "below_threshold"}
        if margin < self.margin:
            return result | {"reason": "ambiguous"}
        return result | {"speaker": name, "reason": "match"}

    # --- HTTP --------------------------------------------------------------------------------

    @modal.asgi_app()
    def web(self):
        import re
        import secrets

        from fastapi import Depends, FastAPI, HTTPException, Request
        from fastapi.concurrency import run_in_threadpool
        from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

        api = FastAPI(title="office-voiceid")
        bearer = HTTPBearer()
        token = os.environ["VOICEID_TOKEN"]

        def auth(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> None:
            if not secrets.compare_digest(creds.credentials, token):
                raise HTTPException(401, "bad token")

        def speaker_name(name: str) -> str:
            name = name.strip().lower()  # matches config/people.json keys (lower-cased names)
            if not re.fullmatch(r"[a-z0-9][a-z0-9 _-]{0,31}", name):
                raise HTTPException(400, "name: 1-32 chars of a-z, 0-9, space, _ or -")
            return name

        async def body(request: Request, limit: int) -> bytes:
            data = await request.body()
            if not data:
                raise HTTPException(400, "send the audio file as the raw request body")
            if len(data) > limit:
                raise HTTPException(413, f"audio larger than {limit // 1_000_000} MB")
            return data

        @api.get("/health")
        def health():
            return {"ok": True, "model": MODEL}

        @api.get("/speakers", dependencies=[Depends(auth)])
        def speakers():
            return {"model": MODEL, "threshold": self.threshold, "margin": self.margin,
                    "speakers": [{"name": n, "segments": len(s["embeddings"]),
                                  "speech_seconds": s["speech_seconds"], "updated_at": s["updated_at"]}
                                 for n, s in sorted(self.speakers.items())]}

        @api.post("/speakers/{name}", dependencies=[Depends(auth)])
        async def enroll(name: str, request: Request, append: bool = False):
            data = await body(request, 25_000_000)
            try:
                return await run_in_threadpool(self.enroll, speaker_name(name), data, append)
            except ValueError as e:
                raise HTTPException(422, str(e)) from e

        @api.delete("/speakers/{name}", dependencies=[Depends(auth)])
        def delete(name: str):
            if not self.delete(speaker_name(name)):
                raise HTTPException(404, "not enrolled")
            return {"deleted": name}

        @api.post("/identify", dependencies=[Depends(auth)])
        async def identify(request: Request):
            data = await body(request, 10_000_000)
            try:
                return await run_in_threadpool(self.identify, data)
            except ValueError as e:
                raise HTTPException(422, str(e)) from e

        return api
