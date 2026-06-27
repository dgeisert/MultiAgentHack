"""Audio Producer — multi-voice TTS + mastering (ElevenLabs + ffmpeg).

Renders each script line with its assigned (unique, premade) voice, saving every
clip as data/<story>/audio/chNN/clip_NN_<character>.mp3, then stitches them into
a single chapter file.
"""
from __future__ import annotations

import os

from .. import settings
from ..state import SeriesState
from ..store import files
from ..tools import elevenlabs, media
from ..tools.util import log


def _existing_clip(out_path: str) -> str | None:
    """Return an already-rendered clip for this line (mp3 or mock .wav), if any."""
    for cand in (out_path, out_path.rsplit(".", 1)[0] + ".wav"):
        if os.path.exists(cand) and os.path.getsize(cand) > 0:
            return cand
    return None


def run(state: SeriesState) -> dict:
    settings.ensure_dirs()
    story = state["series_id"]
    chapter = state.get("current_chapter", 1)
    lines = state["performance_script"]

    log("audio", f"rendering {len(lines)} lines")
    clips, reused = [], 0
    for i, line in enumerate(lines, start=1):
        speaker = line.get("speaker", "Narrator")
        out = files.clip_path(story, chapter, i, speaker)
        # Intra-step resume: skip lines already rendered on a previous attempt.
        done = _existing_clip(out)
        if done:
            clips.append(done)
            reused += 1
            continue
        path = elevenlabs.tts(line["text"], line["voice_id"], out, emotion=line.get("emotion", ""))
        clips.append(path)
    if reused:
        log("audio", f"reused {reused} previously rendered clip(s)")

    chapter_path = files.chapter_audio_path(story, chapter)
    final = media.stitch_audio(clips, chapter_path)
    transcript = "\n".join(f"{l['speaker']}: {l['text']}" for l in lines)
    log("audio", f"mastered chapter audio -> {final}")
    return {"audio_path": final, "transcript": transcript}
