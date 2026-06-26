"""Audio Producer — multi-voice TTS + mastering (ElevenLabs + ffmpeg).

Renders each script line with its assigned (unique, premade) voice, saving every
clip as data/<story>/audio/chNN/clip_NN_<character>.mp3, then stitches them into
a single chapter file.
"""
from __future__ import annotations

from .. import settings
from ..state import SeriesState
from ..store import files
from ..tools import elevenlabs, media
from ..tools.util import log


def run(state: SeriesState) -> dict:
    settings.ensure_dirs()
    story = state["series_id"]
    chapter = state.get("current_chapter", 1)
    lines = state["performance_script"]

    log("audio", f"rendering {len(lines)} lines")
    clips = []
    for i, line in enumerate(lines, start=1):
        speaker = line.get("speaker", "Narrator")
        out = files.clip_path(story, chapter, i, speaker)
        path = elevenlabs.tts(line["text"], line["voice_id"], out, emotion=line.get("emotion", ""))
        clips.append(path)

    chapter_path = files.chapter_audio_path(story, chapter)
    final = media.stitch_audio(clips, chapter_path)
    transcript = "\n".join(f"{l['speaker']}: {l['text']}" for l in lines)
    log("audio", f"mastered chapter audio -> {final}")
    return {"audio_path": final, "transcript": transcript}
