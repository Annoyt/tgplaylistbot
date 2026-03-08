"""Audio recognition service: shazamio (primary) + AcoustID (fallback)."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
from pathlib import Path

from app.db.models import RecognizedTrack

logger = logging.getLogger(__name__)


async def _convert_to_wav(input_path: str) -> str:
    """Convert any audio/video to WAV using ffmpeg."""
    output = tempfile.mktemp(suffix=".wav")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", input_path, "-ar", "44100", "-ac", "1", "-f", "wav", output,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return output


async def recognize_shazam(audio_path: str) -> RecognizedTrack | None:
    """Recognize audio via shazamio."""
    try:
        from shazamio import Shazam

        shazam = Shazam()
        result = await shazam.recognize(audio_path)

        if not result or "track" not in result:
            return None

        track = result["track"]
        return RecognizedTrack(
            title=track.get("title", "Unknown"),
            artist=track.get("subtitle", "Unknown"),
            album=track.get("sections", [{}])[0].get("metadata", [{}])[0].get("text", "")
            if track.get("sections")
            else "",
            cover_url=track.get("images", {}).get("coverart", ""),
        )
    except Exception as e:
        logger.warning("Shazam recognition failed: %s", e)
        return None


async def recognize_acoustid(audio_path: str, api_key: str) -> RecognizedTrack | None:
    """Fallback recognition via AcoustID + Chromaprint."""
    try:
        import acoustid

        results = acoustid.match(api_key, audio_path)
        for score, recording_id, title, artist in results:
            if score > 0.5:
                return RecognizedTrack(
                    title=title or "Unknown",
                    artist=artist or "Unknown",
                )
        return None
    except Exception as e:
        logger.warning("AcoustID recognition failed: %s", e)
        return None


async def recognize(audio_path: str, acoustid_key: str = "") -> RecognizedTrack | None:
    """Recognize audio: try Shazam first, then AcoustID as fallback."""
    wav_path = await _convert_to_wav(audio_path)
    try:
        result = await recognize_shazam(wav_path)
        if result:
            return result

        if acoustid_key:
            result = await recognize_acoustid(wav_path, acoustid_key)
            if result:
                return result

        return None
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)
