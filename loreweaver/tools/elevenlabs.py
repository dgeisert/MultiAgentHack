"""ElevenLabs wrapper: stock-voice catalog lookup + multi-voice TTS.

We do NOT train/design custom voices. Instead the Casting Director picks an
appropriate, unique premade voice per character from this catalog (matched on
gender/age/accent), so there are no voice-quota costs and casting is instant.

Mock path: a curated catalog of real ElevenLabs premade voice ids + metadata,
and per-voice sine-tone WAVs so the Audio Producer can stitch a playable file
with no API key.
"""
from __future__ import annotations

import hashlib
import math
import struct
import wave

from .. import settings
from .util import log, retry

# Real ElevenLabs premade voice ids with metadata — used directly in mock mode
# and as a fallback if the live catalog can't be fetched.
_MOCK_CATALOG = [
    {"voice_id": "21m00Tcm4TlvDq8ikWAM", "name": "Rachel", "gender": "female",
     "age": "young", "accent": "american", "description": "calm, even narration"},
    {"voice_id": "pNInz6obpgDQGcFmaJgB", "name": "Adam", "gender": "male",
     "age": "middle-aged", "accent": "american", "description": "deep, narration"},
    {"voice_id": "onwK4e9ZLuTAKqWW03F9", "name": "Daniel", "gender": "male",
     "age": "middle-aged", "accent": "british", "description": "authoritative"},
    {"voice_id": "ThT5KcBeYPX3keUQqHPh", "name": "Dorothy", "gender": "female",
     "age": "young", "accent": "british", "description": "pleasant storytelling"},
    {"voice_id": "ErXwobaYiN019PkySvjV", "name": "Antoni", "gender": "male",
     "age": "young", "accent": "american", "description": "well-rounded, warm"},
    {"voice_id": "AZnzlk1XvdvUeBnXmlld", "name": "Domi", "gender": "female",
     "age": "young", "accent": "american", "description": "strong, confident"},
    {"voice_id": "VR6AewLTigWG4xSOukaG", "name": "Arnold", "gender": "male",
     "age": "middle-aged", "accent": "american", "description": "crisp, gruff"},
    {"voice_id": "yoZ06aMxZJJ28mfd3POQ", "name": "Sam", "gender": "male",
     "age": "young", "accent": "american", "description": "raspy, youthful"},
    {"voice_id": "EXAVITQu4vr4xnSDxMaL", "name": "Bella", "gender": "female",
     "age": "young", "accent": "american", "description": "soft, gentle"},
    {"voice_id": "oWAxZDx7w5VEj9dCyTzz", "name": "Grace", "gender": "female",
     "age": "young", "accent": "american-southern", "description": "gentle, lilting"},
    {"voice_id": "CYw3kZ02Hs0563khs1Fj", "name": "Dave", "gender": "male",
     "age": "young", "accent": "british-essex", "description": "conversational"},
    {"voice_id": "TxGEqnHWrfWFTfGW9XjX", "name": "Josh", "gender": "male",
     "age": "young", "accent": "american", "description": "deep, earnest"},
]


@retry(times=2)
def _live_list_voices() -> list[dict]:
    from elevenlabs.client import ElevenLabs

    client = ElevenLabs(api_key=settings.ELEVENLABS_API_KEY)
    resp = client.voices.get_all()
    out = []
    for v in resp.voices:
        labels = getattr(v, "labels", None) or {}
        out.append({
            "voice_id": v.voice_id,
            "name": getattr(v, "name", "") or "",
            "gender": (labels.get("gender") or "").lower(),
            "age": (labels.get("age") or "").lower(),
            "accent": (labels.get("accent") or "").lower(),
            "description": (labels.get("description") or labels.get("descriptive")
                            or getattr(v, "description", "") or "").lower(),
        })
    return out or list(_MOCK_CATALOG)


def list_voices() -> list[dict]:
    """Return the catalog of available premade voices with metadata."""
    if settings.mock_mode():
        log("elevenlabs", f"(mock) catalog of {len(_MOCK_CATALOG)} premade voices")
        return list(_MOCK_CATALOG)
    log("elevenlabs", "fetching premade voice catalog")
    try:
        return _live_list_voices()
    except Exception as e:  # noqa: BLE001
        log("elevenlabs", f"catalog fetch failed ({e}); using built-in catalog")
        return list(_MOCK_CATALOG)


@retry(times=3)
def _live_tts(text: str, voice_id: str, out_path: str) -> str:
    from elevenlabs.client import ElevenLabs

    client = ElevenLabs(api_key=settings.ELEVENLABS_API_KEY)
    audio = client.text_to_speech.convert(
        voice_id=voice_id, model_id=settings.ELEVENLABS_MODEL, text=text
    )
    with open(out_path, "wb") as f:
        for chunk in audio:
            f.write(chunk)
    return out_path


def tts(text: str, voice_id: str, out_path: str, *, emotion: str = "") -> str:
    """Render one line to an audio file. Mock writes a short tone-coded WAV."""
    if settings.mock_mode():
        return _mock_tts(text, voice_id, out_path)
    log("elevenlabs", f"tts ({voice_id}, {len(text)} chars)")
    return _live_tts(text, voice_id, out_path)


def _mock_tts(text: str, voice_id: str, out_path: str) -> str:
    """Write a short sine tone whose pitch is derived from the voice id, and
    whose length scales with the text. Produces a real, audible WAV file."""
    wav_path = out_path.rsplit(".", 1)[0] + ".wav"
    seed = int(hashlib.sha1(voice_id.encode()).hexdigest(), 16)
    freq = 160 + (seed % 220)  # 160-380 Hz, distinct per voice
    rate = 22050
    dur = min(4.0, max(0.6, len(text) / 18.0))
    n = int(rate * dur)
    with wave.open(wav_path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = bytearray()
        for i in range(n):
            env = min(1.0, i / 1000) * min(1.0, (n - i) / 1000)  # fade in/out
            val = int(12000 * env * math.sin(2 * math.pi * freq * i / rate))
            frames += struct.pack("<h", val)
        w.writeframes(bytes(frames))
    log("elevenlabs", f"(mock) tts -> {wav_path} ({dur:.1f}s @ {freq}Hz)")
    return wav_path
