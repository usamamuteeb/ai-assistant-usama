"""Read visible text from the entire screen using local OCR."""
from __future__ import annotations

from typing import Any

from src.tools.base import Tool

try:
    import pytesseract
    from PIL import ImageGrab
except ImportError as exc:  # pragma: no cover - dependency is declared in requirements.txt
    pytesseract = None
    ImageGrab = None
    _OCR_IMPORT_ERROR = str(exc)
else:
    try:
        pytesseract.get_tesseract_version()
    except Exception as exc:
        _OCR_IMPORT_ERROR = str(exc)
    else:
        _OCR_IMPORT_ERROR = None


class CaptureScreenTextTool(Tool):
    name = "read_screen_text"
    description = (
        "Captures and reads text from whatever is currently visible on the ENTIRE screen, "
        "not a specific app — the extracted text is sent to whichever model answers this request."
    )
    input_schema = {"type": "object", "properties": {}, "required": []}

    def run(self) -> Any:
        if _OCR_IMPORT_ERROR is not None or pytesseract is None or ImageGrab is None:
            return {
                "error": (
                    "Tesseract OCR is unavailable. Install Tesseract for Windows from "
                    "https://github.com/UB-Mannheim/tesseract/wiki, ensure it is on PATH, "
                    "or set pytesseract.pytesseract.tesseract_cmd to the installed tesseract.exe path. "
                    f"Details: {_OCR_IMPORT_ERROR or 'OCR dependencies are unavailable.'}"
                )
            }

        try:
            screenshot = ImageGrab.grab()
            return {"text": pytesseract.image_to_string(screenshot).strip()}
        except Exception as exc:
            return {"error": f"Screen OCR failed: {exc}"}


def register(confirm_fn=None) -> list[Tool]:
    _ = confirm_fn
    return [CaptureScreenTextTool()]
