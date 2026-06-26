"""Audio Producer — multi-voice TTS + mastering (ElevenLabs + ffmpeg).

Renders each script line with its assigned voice/emotion, then stitches the
clips into a single chapter audio file.
"""
from __future__ import annotations

from .. import settings
from ..state import SeriesState
from ..tools import elevenlabs, media
from ..tools.util import log


def run(state: SeriesState) -> dict:
    settings.ensure_dirs()
    sid = state["series_id"]
    chapter = state.get("current_chapter", 1)
    lines = state["performance_script"]

    log("audio", f"rendering {len(lines)} lines")
    clip_dir = settings.ARTIFACTS_DIR / sid / f"ch{chapter:02d}" / "clips"
    clip_dir.mkdir(parents=True, exist_ok=True)

    clips = []
    for line in lines:
        out = str(clip_dir / f"{line['idx']:03d}.mp3")
        path = elevenlabs.tts(line["text"], line["voice_id"], out, emotion=line.get("emotion", ""))
        clips.append(path)

    chapter_path = str(settings.ARTIFACTS_DIR / sid / f"ch{chapter:02d}" / "chapter.mp3")
    final = media.stitch_audio(clips, chapter_path)
    transcript = "\n".join(f"{l['speaker']}: {l['text']}" for l in lines)
    log("audio", f"mastered chapter audio -> {final}")
    return {"audio_path": final, "transcript": transcript}
