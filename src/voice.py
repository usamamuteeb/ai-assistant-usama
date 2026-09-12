"""Local voice input and output helpers."""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import pyttsx3
from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)

# Change to "small" or "medium" for better accuracy at the cost of speed.
_WHISPER_MODEL = WhisperModel("base", device="cpu", compute_type="int8")


def transcribe_audio(audio_bytes: bytes) -> str:
    """Transcribe browser-recorded audio locally, returning an empty string on failure."""
    if not audio_bytes:
        return ""
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as temporary_file:
            temporary_file.write(audio_bytes)
            temporary_path = Path(temporary_file.name)

        segments, _info = _WHISPER_MODEL.transcribe(str(temporary_path))
        return " ".join(segment.text.strip() for segment in segments).strip()
    except Exception as exc:
        logger.warning("Voice transcription failed: %s", exc)
        return ""
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def speak_text(text: str) -> None:
    """Speak text through the system default audio output."""
    if not text.strip():
        return
    try:
        engine = pyttsx3.init()
        engine.say(text)
        engine.runAndWait()
        engine.stop()
    except Exception as exc:
        logger.warning("Voice output failed: %s", exc)
