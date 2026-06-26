"""ElevenLabs wrapper: Voice Design (mint character voices) + multi-voice TTS.

Mock path writes short silent/sine WAV segments so the Audio Producer can still
stitch a real, playable MP3 for the demo without any API key.
"""
from __future__ import annotations

import hashlib
import math
import struct
import wave

from .. import settings
from .util import log, retry

# A few stable preset voice ids used in mock mode (and as live fallbacks).
_PRESET_VOICES = {
    "Narrator": "voice_narrator_mock",
    "default": "voice_default_mock",
}


@retry(times=3)
def _live_design_voice(name: str, brief: str) -> str:
    from elevenlabs.client import ElevenLabs

    client = ElevenLabs(api_key=settings.ELEVENLABS_API_KEY)
    # Voice Design: create a voice from a natural-language description.
    preview = client.text_to_voice.create_previews(
        voice_description=brief, text=f"This is the voice of {name}."
    )
    gen_id = preview.previews[0].generated_voice_id
    created = client.text_to_voice.create_voice_from_preview(
        voice_name=name, voice_description=brief, generated_voice_id=gen_id
    )
    return created.voice_id


def design_voice(name: str, brief: str) -> str:
    """Create (or look up) a voice for a character; returns a voice_id."""
    if settings.mock_mode():
        vid = "voice_" + hashlib.sha1(f"{name}:{brief}".encode()).hexdigest()[:10]
        log("elevenlabs", f"(mock) designed voice {vid} for {name}")
        return vid
    log("elevenlabs", f"design_voice for {name}")
    return _live_design_voice(name, brief)


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
