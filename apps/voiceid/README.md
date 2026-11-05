# Speaker identification (Modal)

Identifies which enrolled team member is speaking, or returns `null` for unknown. It runs alongside transcription and does not transcribe. Identity is informational only and never grants purchasing authority.

- Model: WeSpeaker ResNet293-LM (VoxCeleb, CC BY 4.0) via sherpa-onnx; Silero VAD strips silence.
- Container: CPU only, 8 cores, 16 GiB, one container (enrollments are held in memory).
- Enrollments (voice embeddings, i.e. biometric data) are stored in the Modal Volume `office-voiceid`. Get consent; delete on request.

## Deploy

```sh
modal secret create office-voiceid VOICEID_TOKEN=<random>   # once; same value as VOICEID_TOKEN in .env
modal deploy apps/voiceid/app.py                            # scales to zero when idle
VOICEID_MIN_CONTAINERS=1 modal deploy apps/voiceid/app.py   # demo: one warm container, no cold start
```

Redeploy without `VOICEID_MIN_CONTAINERS` after the demo so it stops billing.

## Use

The request body is the raw audio file (any format ffmpeg reads). Prefer compressed audio (Opus/webm, m4a) on slow uplinks.

```sh
H="Authorization: Bearer $VOICEID_TOKEN"
curl -X POST -H "$H" --data-binary @leo.m4a "$VOICEID_URL/speakers/leo"              # enroll (>= 10 s speech)
curl -X POST -H "$H" --data-binary @leo-2.m4a "$VOICEID_URL/speakers/leo?append=true" # add more
curl -X POST -H "$H" --data-binary @clip.webm "$VOICEID_URL/identify"
curl -H "$H" "$VOICEID_URL/speakers"
curl -X DELETE -H "$H" "$VOICEID_URL/speakers/leo"
```

Names are the lower-cased `name` from `config/people.json`; URL-encode spaces as `%20`.

`/identify` returns `speaker` (or `null`), `reason` (`match`, `below_threshold`, `ambiguous`, `too_short`, `no_enrollments`), the cosine `scores` for every enrolled person, and `timing_ms`.

## Tuning

Scores are cosine similarities after subtracting a population-mean voiceprint, computed at image build from 40 LibriSpeech dev-clean speakers (CC BY 4.0), as WeSpeaker's own scoring does. Without centring, every pair of voices scores 0.6–0.9.

The decision rule accepts the best match if its cosine score is at least `VOICEID_THRESHOLD` (default 0.7) and beats the runner-up by `VOICEID_MARGIN` (default 0.1). Clips with less than `VOICEID_MIN_SPEECH_S` (default 1.0 s) of speech return `too_short`. These defaults are provisional, based on seven public test clips. Calibrate them with real clips from the team, recorded on the demo microphone, plus a few outside voices. Set overrides as keys in the `office-voiceid` Modal Secret.

## Agent integration

`POST /api/input` with `audio_b64` transcribes the clip and calls `/identify` in parallel (`office/voice.py`). A `person` in the request (dashboard or device pick) takes precedence, and voice ID is skipped. Without `VOICEID_URL`/`VOICEID_TOKEN`, or when the service fails or times out (`VOICEID_TIMEOUT_S`, default 20 s), the request continues from an unknown speaker.

The result is a `Speaker` annotation (`office/speaker.py`). It records the name, its source (`voice_id`, `declared` or `none`), the score, margin and reason, and is never marked verified. The annotation:

- is stored per task in `task_speakers`, so a resumed approval keeps the same speaker;
- is shown in task views as `speaker` and in approval details as `requester`;
- is available to tools and server policy as `ctx.deps.speaker`;
- reaches the model through the `SpeakerContext` capability, as an instruction part named `speaker`.

Access control may only restrict. A tool that needs a known requester declares the tool metadata `{"speaker": "identified"}`, and `SpeakerContext` hides it from unidentified speakers. Approval policy never depends on who spoke.
