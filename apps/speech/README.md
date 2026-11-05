# Voice station (physical Atech board)

`voice_station.py` drives the board running Leo's `office_station` firmware:

- **Button** → push-to-talk on the laptop mic → speech-to-text (ElevenLabs Scribe v2, local Whisper fallback).
- Air/temperature/CO₂ questions are answered from the board's live readings; everything else goes to the API as a `speech_transcript` (follow-ups answer the open clarification).
- The answer is shown on the board screen and spoken on the board speaker (macOS `say`, Nora for Norwegian).
- Physical `co2_co2` / `aht20_*` samples are forwarded to `POST /api/telemetry`; sound actions from `/api/devices/kitchen/actions` are played and acknowledged.

## Setup

```sh
uv venv --python 3.12 apps/speech/.venv
uv pip install --python apps/speech/.venv/bin/python -r apps/speech/requirements.txt atech pyserial
# repo-root .env (ignored): ELEVENLABS_API_KEY=sk_...   (optional; Whisper is used without it)
```

## Run

Start the app first (`npm run dev`), then — as the **only** program using the board's USB port:

```sh
apps/speech/.venv/bin/python apps/speech/voice_station.py --port "$(ls /dev/cu.usbmodem* | head -1)" --api http://127.0.0.1:3001
```

Ready when the board screen shows "Trykk paa knappen og snakk" with a blue LED. Speak close to the laptop.

`stt.py` also works on its own (`python apps/speech/stt.py --station kitchen`).
