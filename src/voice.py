"""Local voice input and output helpers."""
from __future__ import annotations

import logging
import re
import tempfile
import threading
from pathlib import Path

import pyttsx3
from faster_whisper import WhisperModel

logger = logging.getLogger(__name__)

# Load Whisper only when someone actually records audio. Importing the web UI
# should not allocate a speech model just to render the chat page.
_WHISPER_MODEL: WhisperModel | None = None
_whisper_lock = threading.Lock()
_speech_lock = threading.RLock()
_active_engine: object | None = None


def _whisper_model() -> WhisperModel:
    global _WHISPER_MODEL
    with _whisper_lock:
        if _WHISPER_MODEL is None:
            _WHISPER_MODEL = WhisperModel("base", device="cpu", compute_type="int8")
        return _WHISPER_MODEL


def transcribe_audio(audio_bytes: bytes) -> str:
    """Transcribe browser-recorded audio locally, returning an empty string on failure."""
    if not audio_bytes:
        return ""
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".webm", delete=False) as temporary_file:
            temporary_file.write(audio_bytes)
            temporary_path = Path(temporary_file.name)

        segments, _info = _whisper_model().transcribe(str(temporary_path))
        return " ".join(segment.text.strip() for segment in segments).strip()
    except Exception as exc:
        logger.warning("Voice transcription failed: %s", exc)
        return ""
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def speech_text(markdown: str) -> str:
    """Turn an assistant reply into concise, natural text for local TTS.

    The chat renderer may contain Markdown syntax, images, model labels and
    dense tables. Passing that directly to a screen reader or TTS engine makes
    it spell punctuation and internal metadata, so speech uses this dedicated
    representation instead of the rendered chat content.
    """
    text = markdown.replace("\r\n", "\n")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"```.*?```", "I included code in the chat.", text, flags=re.DOTALL)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)

    lines = text.splitlines()
    output: list[str] = []
    table_lines: list[str] = []

    def flush_table() -> None:
        if not table_lines:
            return
        rows = [
            [cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in table_lines
            if not re.fullmatch(r"\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?", line.strip())
        ]
        table_lines.clear()
        if not rows:
            return
        headers = [header for header in rows[0] if header]
        data_rows = rows[1:]
        if headers:
            output.append(f"Table with columns: {', '.join(headers)}.")
        for row in data_rows[:3]:
            values = [value for value in row if value]
            if values:
                output.append("; ".join(values) + ".")
        if len(data_rows) > 3:
            output.append(f"The table contains {len(data_rows) - 3} additional rows in the chat.")

    for raw_line in lines:
        line = raw_line.strip()
        if "|" in line and line.count("|") >= 2:
            table_lines.append(line)
            continue
        flush_table()
        if not line or re.fullmatch(r"[-*_]{3,}", line):
            continue
        if re.match(r"^via\s+[^\n]+$", line, flags=re.IGNORECASE):
            continue
        if re.match(r"^(?:workspace|generated_images|screenshots)[/\\]", line, flags=re.IGNORECASE):
            continue
        line = re.sub(r"^#{1,6}\s*", "", line)
        line = re.sub(r"^>\s*", "", line)
        line = re.sub(r"^\s*(?:[-*+] |\d+[.)] )", "", line)
        line = re.sub(r"(`{1,3}|\*{1,3}|_{1,3}|~~)", "", line)
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            output.append(line)
    flush_table()
    return " ".join(output).strip()


def stop_speaking() -> None:
    """Stop the current pyttsx3 playback, if one exists."""
    with _speech_lock:
        engine = _active_engine
        if engine is None:
            return
        try:
            engine.stop()  # type: ignore[union-attr]
        except Exception as exc:
            logger.debug("Could not stop voice output: %s", exc)


def speak_text(text: str) -> None:
    """Speak already-cleaned text through the system default audio output."""
    global _active_engine
    if not text.strip():
        return
    engine: object | None = None
    try:
        engine = pyttsx3.init()
        with _speech_lock:
            _active_engine = engine
        engine.say(text)
        engine.runAndWait()
    except Exception as exc:
        logger.warning("Voice output failed: %s", exc)
    finally:
        try:
            if engine is not None:
                engine.stop()  # type: ignore[union-attr]
        except Exception:
            pass
        with _speech_lock:
            if _active_engine is engine:
                _active_engine = None
