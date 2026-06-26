"""Audio/video helpers: stitch per-line clips into one chapter file, and render
an audio-+-cover video for YouTube. Uses ffmpeg when available, with a pure-
Python WAV concatenation fallback so it always produces a playable file.
"""
from __future__ import annotations

import shutil
import subprocess
import wave
from pathlib import Path

from .util import log


def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def stitch_audio(clip_paths: list[str], out_path: str) -> str:
    """Concatenate audio clips in order into a single chapter file."""
    clips = [c for c in clip_paths if c and Path(c).exists()]
    if not clips:
        raise ValueError("no audio clips to stitch")

    if _has_ffmpeg():
        list_file = Path(out_path).with_suffix(".txt")
        list_file.write_text("".join(f"file '{Path(c).resolve()}'\n" for c in clips))
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
               "-ar", "44100", "-ac", "1", out_path]
        subprocess.run(cmd, check=True, capture_output=True)
        list_file.unlink(missing_ok=True)
        log("media", f"stitched {len(clips)} clips -> {out_path} (ffmpeg)")
        return out_path

    # Fallback: concatenate WAVs natively, write a .wav next to requested path.
    wav_out = out_path.rsplit(".", 1)[0] + ".wav"
    with wave.open(clips[0], "rb") as first:
        params = first.getparams()
    with wave.open(wav_out, "wb") as out:
        out.setparams(params)
        for c in clips:
            cw = c.rsplit(".", 1)[0] + ".wav"
            cw = cw if Path(cw).exists() else c
            try:
                with wave.open(cw, "rb") as w:
                    out.writeframes(w.readframes(w.getnframes()))
            except Exception as e:  # noqa: BLE001
                log("media", f"skip clip {cw}: {e}")
    log("media", f"stitched {len(clips)} clips -> {wav_out} (wav fallback)")
    return wav_out


def make_video(audio_path: str, image_path: str, out_path: str) -> str:
    """Combine a still cover + chapter audio into an MP4 for YouTube."""
    if not _has_ffmpeg():
        log("media", "ffmpeg missing; skipping video render (YouTube will be stubbed)")
        return ""
    cmd = ["ffmpeg", "-y", "-loop", "1", "-i", image_path, "-i", audio_path,
           "-c:v", "libx264", "-tune", "stillimage", "-c:a", "aac", "-b:a", "192k",
           "-pix_fmt", "yuv420p", "-shortest", out_path]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        log("media", f"rendered video -> {out_path}")
        return out_path
    except Exception as e:  # noqa: BLE001
        log("media", f"video render failed: {e}")
        return ""
