"""Speech-to-text and speaker identification. Owner: voice.

Install extras first: `uv sync --extra voice` (from apps/agent).
"""

import os


def transcribe(audio: bytes, filename: str = "audio.wav") -> str:
    """Audio file bytes (wav/webm/m4a...) → text, via OpenAI. Needs OPENAI_API_KEY."""
    from openai import OpenAI

    model = os.getenv("TRANSCRIBE_MODEL", "gpt-4o-transcribe")
    return OpenAI().audio.transcriptions.create(model=model, file=(filename, audio)).text


def identify(audio: bytes) -> str | None:
    """Audio → enrolled person's name (a key in config/people.json), or None if unsure.

    TODO(voice): enroll each person once (~20 s each) with resemblyzer's VoiceEncoder, store the mean
    embedding under .data/voices/<name>.npy (gitignored, it's biometric data), then compare with cosine
    similarity and return the best match above a threshold (~0.75, tune it on real clips).
    Voice ID only sets the speaker's role; it is never authentication for real money.
    """
    return None
