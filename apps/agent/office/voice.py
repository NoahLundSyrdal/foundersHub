"""Speech-to-text and speaker identification. Owner: voice.

Transcription uses ElevenLabs Scribe (ELEVENLABS_API_KEY); the OpenAI fallback needs `uv sync --extra voice`.
Speaker ID calls the Modal service in apps/voiceid (VOICEID_URL + VOICEID_TOKEN); without it every voice
request is from an unknown speaker.
"""

import logging
import os

import httpx

from office.speaker import Speaker

log = logging.getLogger(__name__)


def transcribe(audio: bytes, filename: str = "audio.wav") -> str:
    """Audio file bytes (wav/webm/m4a...) → text. ElevenLabs Scribe with ELEVENLABS_API_KEY, else OpenAI."""
    if key := os.getenv("ELEVENLABS_API_KEY"):
        r = httpx.post(
            "https://api.elevenlabs.io/v1/speech-to-text",
            headers={"xi-api-key": key},
            data={"model_id": os.getenv("TRANSCRIBE_MODEL", "scribe_v2"), "tag_audio_events": "false"},
            files={"file": (filename, audio)},
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["text"]
    from openai import OpenAI

    model = os.getenv("TRANSCRIBE_MODEL", "gpt-4o-transcribe")
    return OpenAI().audio.transcriptions.create(model=model, file=(filename, audio)).text


def identify(audio: bytes) -> Speaker:
    """Audio → who is speaking among the enrolled team, or an unknown speaker with the reason.

    Never raises: speaker ID is informational, so a slow or failing service must not block the request.
    The timeout allows one Modal cold start; deploy with VOICEID_MIN_CONTAINERS=1 for demos.
    """
    url, token = os.getenv("VOICEID_URL"), os.getenv("VOICEID_TOKEN")
    if not url or not token:
        return Speaker.unknown("not_configured")
    try:
        r = httpx.post(f"{url.rstrip('/')}/identify", content=audio, timeout=float(os.getenv("VOICEID_TIMEOUT_S", "20")),
                       headers={"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"})
        r.raise_for_status()
        return Speaker.from_voice_id(r.json())
    except (httpx.HTTPError, ValueError) as e:
        log.warning("speaker identification unavailable: %s", type(e).__name__)
        return Speaker.unknown("unavailable")
