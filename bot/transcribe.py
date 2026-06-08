"""Local speech-to-text for incoming voice/audio messages (faster-whisper).

The model is loaded lazily and cached for the life of the process. Transcription
is CPU-bound, so callers should run :func:`transcribe` in a thread
(``asyncio.to_thread``) to avoid blocking the event loop.
"""

import logging

import config

logger = logging.getLogger(__name__)

_MODEL = None


def _model():
    global _MODEL
    if _MODEL is None:
        from faster_whisper import WhisperModel

        _MODEL = WhisperModel(
            config.WHISPER_MODEL, device="cpu", compute_type="int8"
        )
    return _MODEL


def transcribe(path: str) -> str:
    """Transcribe an audio/video file to text (best-effort)."""
    try:
        segments, _info = _model().transcribe(path)
        return " ".join(s.text.strip() for s in segments).strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Transcription failed for %s: %s", path, exc)
        return ""
