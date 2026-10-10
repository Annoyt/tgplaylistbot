"""Audio recognition service: shazamio (primary) + AcoustID (fallback)."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile

from app.db.models import RecognizedTrack

logger = logging.getLogger(__name__)


_WAV_RATE = 44100

# Speed factors to retry when normal recognition fails. "Sped up" (nightcore)
# and "slowed + reverb" edits resample the audio, shifting tempo AND pitch
# together, which breaks Shazam's fingerprint. Re-resampling by the inverse
# factor restores the original. We don't know the direction, so we probe both:
# <1 slows down (reverses a sped-up edit), >1 speeds up (reverses a slowed edit).
_SPEED_FACTORS = (0.8, 1.25, 0.7, 1.5)


async def _convert_to_wav(input_path: str) -> str:
    """Convert any audio/video to WAV using ffmpeg."""
    output = tempfile.mktemp(suffix=".wav")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", input_path, "-ar", str(_WAV_RATE), "-ac", "1", "-f", "wav", output,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return output


async def _speed_variant(wav_path: str, factor: float) -> str | None:
    """Resample a WAV by `factor`, shifting tempo+pitch together (like a re-edit).

    Returns the path to a new WAV, or None if ffmpeg produced nothing.
    """
    output = tempfile.mktemp(suffix=".wav")
    # asetrate reinterprets the sample rate (changes tempo+pitch); aresample
    # brings it back to a standard rate so Shazam/ffmpeg read it correctly.
    new_rate = int(_WAV_RATE * factor)
    filt = f"asetrate={new_rate},aresample={_WAV_RATE}"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", wav_path, "-af", filt, "-ac", "1", "-f", "wav", output,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    if os.path.exists(output) and os.path.getsize(output) > 0:
        return output
    if os.path.exists(output):
        os.remove(output)
    return None


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


async def recognize(
    audio_path: str,
    acoustid_key: str = "",
    try_speed_variants: bool = True,
) -> RecognizedTrack | None:
    """Recognize audio: Shazam, then AcoustID, then speed-adjusted retries.

    Many shared clips are sped up or slowed down; if the original-speed pass
    fails we retry Shazam on tempo-corrected variants before giving up.
    """
    wav_path = await _convert_to_wav(audio_path)
    try:
        result = await recognize_shazam(wav_path)
        if result:
            return result

        if acoustid_key:
            result = await recognize_acoustid(wav_path, acoustid_key)
            if result:
                return result

        if try_speed_variants:
            for factor in _SPEED_FACTORS:
                variant = await _speed_variant(wav_path, factor)
                if not variant:
                    continue
                try:
                    result = await recognize_shazam(variant)
                    if result:
                        logger.info("Recognized via speed variant x%.2f", factor)
                        return result
                finally:
                    if os.path.exists(variant):
                        os.remove(variant)

        return None
    finally:
        if os.path.exists(wav_path):
            os.remove(wav_path)
