"""NiceGUI web interface for the personal assistant."""

from __future__ import annotations

import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import asyncio
import base64
import logging
import platform
import re
import socket
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nicegui import app, run, ui

try:
    from watchdog.events import FileSystemEventHandler
except ImportError:  # optional until requirements are installed
    class FileSystemEventHandler:  # type: ignore[no-redef]
        pass

from src.config import load_settings
from src.orchestrator import Orchestrator
from src.voice import speak_text, speech_text, stop_speaking, transcribe_audio

APPROVAL_MODES = ["manual", "auto"]
PAGE_TITLE = "Personal AI Assistant"
# A long local image job must not make the chat appear frozen. The actual
# worker continues after this UI handoff and replaces its pending chat bubble
# when it finishes.
REQUEST_UI_TIMEOUT_SECONDS = 20
VOICE_CAPTURE_JS = """
async (event) => {
    const button = event.currentTarget;
    if (window.__neuralVoiceCapture) {
        window.__neuralVoiceCapture.stop();
        return;
    }
    const emitVoice = (phase, text = '', payload = '', error = '') => emit({phase, text, payload, error});
    const startLocalRecorder = (stream) => {
        const recorder = new MediaRecorder(stream);
        const chunks = [];
        const audioContext = new (window.AudioContext || window.webkitAudioContext)();
        const analyser = audioContext.createAnalyser();
        const source = audioContext.createMediaStreamSource(stream);
        const samples = new Uint8Array(analyser.fftSize);
        let lastSpeechAt = performance.now();
        source.connect(analyser);
        const silenceTimer = window.setInterval(() => {
            analyser.getByteTimeDomainData(samples);
            let total = 0;
            for (const sample of samples) total += Math.abs(sample - 128);
            if (total / samples.length > 3) lastSpeechAt = performance.now();
            if (performance.now() - lastSpeechAt > 1800 && recorder.state === 'recording') recorder.stop();
        }, 180);
        recorder.ondataavailable = (recordedEvent) => {
            if (recordedEvent.data.size > 0) chunks.push(recordedEvent.data);
        };
        recorder.onstop = () => {
            stream.getTracks().forEach((track) => track.stop());
            window.clearInterval(silenceTimer);
            source.disconnect();
            audioContext.close();
            window.__neuralVoiceCapture = null;
            button.classList.remove('voice-recording');
            const reader = new FileReader();
            reader.onloadend = () => emitVoice('audio', '', reader.result.split(',')[1] || '');
            reader.readAsDataURL(new Blob(chunks, {type: recorder.mimeType || 'audio/webm'}));
        };
        window.__neuralVoiceCapture = {stop: () => recorder.stop()};
        button.classList.add('voice-recording');
        emitVoice('listening');
        recorder.start();
        window.setTimeout(() => window.__neuralVoiceCapture?.stop(), 20000);
    };
    try {
        const stream = await navigator.mediaDevices.getUserMedia({audio: true});
        const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (Recognition) {
            stream.getTracks().forEach((track) => track.stop());
            const recognition = new Recognition();
            let finalText = '';
            let stopped = false;
            recognition.continuous = false;
            recognition.interimResults = true;
            recognition.lang = navigator.language || 'en-US';
            recognition.onstart = () => {
                button.classList.add('voice-recording');
                emitVoice('listening');
            };
            recognition.onresult = (resultEvent) => {
                let interim = '';
                for (let index = resultEvent.resultIndex; index < resultEvent.results.length; index++) {
                    const text = resultEvent.results[index][0].transcript;
                    if (resultEvent.results[index].isFinal) finalText += `${text} `;
                    else interim += text;
                }
                emitVoice('partial', (finalText + interim).trim());
            };
            recognition.onerror = async (errorEvent) => {
                const error = errorEvent.error || 'Speech recognition failed.';
                if (error === 'network') {
                    // Chromium recognition is often cloud-backed. Preserve a
                    // useful mic experience by falling back to local Whisper.
                    try {
                        recognition.onend = null;
                        window.__neuralVoiceCapture = null;
                        button.classList.remove('voice-recording');
                        startLocalRecorder(await navigator.mediaDevices.getUserMedia({audio: true}));
                    } catch (_) {
                        emitVoice('error', '', '', 'Browser recognition failed and local microphone recording could not start.');
                    }
                    return;
                }
                emitVoice('error', '', '', error);
            };
            recognition.onend = () => {
                button.classList.remove('voice-recording');
                window.__neuralVoiceCapture = null;
                const transcript = finalText.trim();
                emitVoice(transcript ? 'final' : (stopped ? 'cancelled' : 'stopped'), transcript);
            };
            recognition.start();
            window.__neuralVoiceCapture = {stop: () => { stopped = true; recognition.stop(); }};
            window.setTimeout(() => window.__neuralVoiceCapture?.stop(), 20000);
            return;
        }
        startLocalRecorder(stream);
    } catch (error) {
        button.classList.remove('voice-recording');
        emitVoice('error', '', '', 'Microphone access was denied or unavailable.');
    }
}
"""

VOICE_HANDSFREE_JS = """
async (event) => {
    const button = event.currentTarget;
    const emitVoice = (phase, text = '', error = '') => emit({phase, text, error});
    if (window.__neuralHandsFree) {
        window.__neuralHandsFree.stop();
        return;
    }
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Recognition) {
        emitVoice('error', '', 'Hands-free mode requires browser speech recognition. Use the microphone button instead.');
        return;
    }
    try {
        await navigator.mediaDevices.getUserMedia({audio: true}).then((stream) => stream.getTracks().forEach((track) => track.stop()));
        const recognition = new Recognition();
        let active = true;
        let armedUntil = 0;
        let announced = false;
        recognition.continuous = true;
        recognition.interimResults = false;
        recognition.lang = navigator.language || 'en-US';
        recognition.onstart = () => {
            button.classList.add('hands-free-active');
            if (!announced) {
                announced = true;
                emitVoice('hands_free_started');
            }
        };
        recognition.onresult = (resultEvent) => {
            for (let index = resultEvent.resultIndex; index < resultEvent.results.length; index++) {
                if (!resultEvent.results[index].isFinal) continue;
                const heard = resultEvent.results[index][0].transcript.trim();
                const match = heard.match(/(?:^|\\b)hey\\s+neural\\b[,:.!\\s-]*(.*)$/i);
                if (match) {
                    const command = match[1].trim();
                    if (command) emitVoice('wake', command);
                    else {
                        armedUntil = Date.now() + 8000;
                        emitVoice('wake_armed');
                    }
                } else if (Date.now() < armedUntil && heard) {
                    armedUntil = 0;
                    emitVoice('wake', heard);
                }
            }
        };
        recognition.onerror = (errorEvent) => {
            if (errorEvent.error !== 'no-speech' && errorEvent.error !== 'aborted') {
                emitVoice('error', '', errorEvent.error || 'Hands-free recognition failed.');
                // Network, permission, and capture failures cannot recover by
                // immediately restarting the same browser recognition session.
                if (['network', 'not-allowed', 'service-not-allowed', 'audio-capture', 'language-not-supported'].includes(errorEvent.error)) {
                    active = false;
                }
            }
        };
        recognition.onend = () => {
            if (active) window.setTimeout(() => { try { recognition.start(); } catch (_) {} }, 250);
            else {
                button.classList.remove('hands-free-active');
                window.__neuralHandsFree = null;
                emitVoice('hands_free_stopped');
            }
        };
        window.__neuralHandsFree = {stop: () => { active = false; recognition.stop(); }};
        recognition.start();
    } catch (_) {
        emitVoice('error', '', 'Microphone permission is required for hands-free mode.');
    }
}
"""
logger = logging.getLogger(__name__)
_pending_confirmations: dict[str, dict[str, Any]] = {}
_pending_lock = threading.Lock()
_session_confirm_fns: dict[str, Any] = {}
_approval_modes: dict[str, str] = {}
_last_confirmation_results: dict[str, bool] = {}
_activity_by_session: dict[str, list[dict[str, str]]] = {}
_activity_lock = threading.Lock()
_background_results: dict[str, dict[str, Any]] = {}
_background_results_lock = threading.Lock()
_kb_watch_notifications: list[str] = []
_kb_watch_lock = threading.Lock()
_kb_watch_timers: dict[str, threading.Timer] = {}
_kb_watch_observer: Any | None = None
_active_session_id: str | None = None
_active_request_task: asyncio.Task[str] | None = None
_request_tasks: dict[tuple[str, str], asyncio.Task[str]] = {}

# Browser screenshots are saved by the browser plugin under the workspace. Make
# them available to this local UI without exposing the rest of the workspace.
_ui_settings = load_settings()
_browser_screenshots_dir = _ui_settings.workspace_root() / "screenshots"
_browser_screenshots_dir.mkdir(parents=True, exist_ok=True)
app.add_static_files("/browser-screenshots", _browser_screenshots_dir)
_generated_images_dir = _ui_settings.workspace_root() / "generated_images"
_generated_images_dir.mkdir(parents=True, exist_ok=True)
app.add_static_files("/generated-images", _generated_images_dir)
app.add_static_files("/screenshots", _browser_screenshots_dir)


def _model_label(provider: str, model: str) -> str:
    readable_model = model.rsplit("/", 1)[-1].replace("-", " ").replace("_", " ")
    readable_model = " ".join(
        word.upper() if word.isdigit() else word.title() for word in readable_model.split()
    )
    return f"{provider.title()}: {readable_model}"


MODEL_OPTIONS: dict[str, str] = {"auto": "Auto", "local": "Local (Ollama)"}
for _index, _entry in enumerate(_ui_settings.free_api_model.get("chain", [])):
    MODEL_OPTIONS[f"free_api:{_index}"] = _model_label(_entry["provider"], _entry["model"])
if _ui_settings.routing.get("allow_premium", True):
    MODEL_OPTIONS["premium"] = "Premium (Claude)"
_LEGACY_SCREENSHOT_MARKDOWN = re.compile(
    r"(!\[[^\]]*\]\()\s*(?:\./)?workspace/screenshots/([^\s)]+)(\))"
)
_LOCAL_IMAGE_MARKDOWN = re.compile(
    r"!\[(?P<alt>[^\]]*)\]\(\s*(?P<path>"
    r"(?:\./)?(?:workspace/)?(?:generated_images|screenshots)/[^\s)]+|"
    r"/(?:generated-images|screenshots)/[^\s)]+)\s*\)"
)


def _record_activity(
    session_id: str,
    status: str,
    title: str,
    detail: str = "",
    *,
    kind: str = "assistant",
) -> None:
    """Store a small, thread-safe execution event for that browser session."""
    event = {
        "time": time.strftime("%H:%M:%S"),
        "status": status,
        "title": title,
        "detail": detail,
        "kind": kind,
    }
    with _activity_lock:
        events = _activity_by_session.setdefault(session_id, [])
        events.append(event)
        del events[:-40]


def _clear_activity(session_id: str, *, preserve_voice: bool = False) -> None:
    with _activity_lock:
        if preserve_voice:
            _activity_by_session[session_id] = [
                event
                for event in _activity_by_session.get(session_id, [])
                if event.get("kind") == "voice"
            ]
        else:
            _activity_by_session[session_id] = []


def _activity_snapshot(session_id: str) -> list[dict[str, str]]:
    with _activity_lock:
        return [dict(event) for event in _activity_by_session.get(session_id, [])]


def _surface_confirmation_notification() -> None:
    """Best-effort cue for a browser-hosted approval request on Windows."""
    if platform.system() != "Windows":
        return

    try:
        import win32api
        import win32con
        import win32gui
        import win32process

        candidates: list[int] = []

        def collect_window(handle: int, _extra: Any) -> bool:
            if win32gui.IsWindowVisible(handle) and PAGE_TITLE.casefold() in win32gui.GetWindowText(handle).casefold():
                candidates.append(handle)
            return True

        win32gui.EnumWindows(collect_window, None)
        if not candidates:
            raise RuntimeError(f"No visible browser window title contains '{PAGE_TITLE}'.")
        target = candidates[0]
        foreground = win32gui.GetForegroundWindow()
        current_thread = win32api.GetCurrentThreadId()
        foreground_thread, _ = win32process.GetWindowThreadProcessId(foreground)
        target_thread, _ = win32process.GetWindowThreadProcessId(target)
        attached_foreground = attached_target = False
        try:
            if foreground_thread and foreground_thread != current_thread:
                win32process.AttachThreadInput(current_thread, foreground_thread, True)
                attached_foreground = True
            if target_thread and target_thread != current_thread:
                win32process.AttachThreadInput(current_thread, target_thread, True)
                attached_target = True

            win32gui.ShowWindow(target, win32con.SW_RESTORE)
            win32gui.BringWindowToTop(target)
            # A brief topmost raise is a fallback for Windows focus-stealing
            # prevention. It is immediately reverted and never left topmost.
            position_flags = win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW
            win32gui.SetWindowPos(target, win32con.HWND_TOPMOST, 0, 0, 0, 0, position_flags)
            win32gui.SetWindowPos(target, win32con.HWND_NOTOPMOST, 0, 0, 0, 0, position_flags)
            win32gui.SetForegroundWindow(target)
            if win32gui.GetForegroundWindow() != target:
                raise RuntimeError("Windows focus-stealing protection rejected the foreground request.")
        finally:
            if attached_target:
                win32process.AttachThreadInput(current_thread, target_thread, False)
            if attached_foreground:
                win32process.AttachThreadInput(current_thread, foreground_thread, False)
    except Exception as exc:
        # Windows can reject focus stealing; never let that block the worker.
        logger.warning("Could not bring the approval browser window to the foreground: %s", exc)

    try:
        import winsound

        winsound.Beep(880, 180)
    except Exception as exc:
        logger.warning("Could not play the approval notification beep: %s", exc)


def _confirm_for_session(session_id: str, command: str, *, force_manual: bool = False) -> bool:
    if not force_manual and _approval_modes.get(session_id, "auto") == "auto":
        _record_activity(session_id, "done", "Action approved automatically", command)
        return True
    event = threading.Event()
    pending = {"command": command, "event": event, "result": None, "shown": False}
    with _pending_lock:
        _pending_confirmations[session_id] = pending
    _record_activity(session_id, "waiting", "Waiting for your approval", command)
    _surface_confirmation_notification()
    event.wait()
    with _pending_lock:
        result = _pending_confirmations.get(session_id, pending).get("result")
        _pending_confirmations.pop(session_id, None)
        _last_confirmation_results[session_id] = bool(result)
    _record_activity(
        session_id,
        "done" if result else "error",
        "Action approved" if result else "Action denied",
        command,
    )
    return bool(result)


def _make_confirm_fn(session_id: str):
    def confirm(command: str) -> bool:
        return _confirm_for_session(session_id, command)

    return confirm


def _dispatch_confirm(command: str) -> bool:
    session_id = _active_session_id
    if session_id is None:
        return False
    return _session_confirm_fns[session_id](command)


def _dispatch_manual_confirm(command: str) -> bool:
    """Always use the browser dialog, even while global approval mode is auto."""
    session_id = _active_session_id
    if session_id is None:
        return False
    return _confirm_for_session(session_id, command, force_manual=True)


# Plugins which opt into a force-manual policy use this explicit, fail-closed
# path rather than inferring manual mode from the global approval toggle.
_dispatch_confirm.manual_confirm = _dispatch_manual_confirm


# Keep one long-lived orchestrator for its SQLite/vector/tool resources.
orchestrator = Orchestrator(_ui_settings, confirm_fn=_dispatch_confirm)

# Local image generation is intentionally started only for the web application.
# The image plugin remains safe to discover from the CLI and scheduler without
# starting another ComfyUI server process.
_image_generation_tool = getattr(orchestrator.tools, "_tools", {}).get("generate_image")
if _image_generation_tool is not None:
    start_local_image_server = getattr(_image_generation_tool, "start_if_configured", None)
    if callable(start_local_image_server):
        start_local_image_server()

# The registry is the single execution boundary used by the orchestrator.  Wrap
# its already-bound dispatcher here (rather than changing the orchestrator
# contract) so the NiceGUI client can show actual tool starts and finishes.
_registry_call = orchestrator.tools.call


def _tracked_tool_call(name: str, **kwargs: Any) -> Any:
    session_id = _active_session_id
    if session_id:
        inputs = ", ".join(sorted(kwargs)) or "no inputs"
        _record_activity(session_id, "active", f"Running tool: {name}", f"Inputs: {inputs}.")
    result = _registry_call(name, **kwargs)
    if session_id:
        if isinstance(result, dict) and result.get("error"):
            _record_activity(session_id, "error", f"Tool failed: {name}", str(result["error"]))
        else:
            _record_activity(session_id, "done", f"Tool finished: {name}", "Completed successfully.")
    return result


orchestrator.tools.call = _tracked_tool_call


def _knowledge_watch_summary(result: Any) -> str:
    if not isinstance(result, dict):
        return f"Knowledge-base auto-index failed: {result}"
    if result.get("error"):
        return f"Knowledge-base auto-index failed: {result['error']}"
    ingested = len(result.get("ingested_files", []))
    skipped = len(result.get("skipped_unchanged", []))
    duplicates = len(result.get("duplicates", []))
    failed = len(result.get("failed_files", []))
    chunks = result.get("total_chunks_added", 0)
    suffix = f"; {duplicates} duplicate(s)" if duplicates else ""
    suffix += f"; {failed} failed" if failed else ""
    return f"Knowledge base updated: {ingested} new file(s), {chunks} chunks, {skipped} unchanged skipped{suffix}."


def _run_knowledge_watch_ingest(path_text: str) -> None:
    try:
        result = orchestrator.tools.call("ingest_knowledge_base")
        message = _knowledge_watch_summary(result)
    except Exception as exc:
        logger.warning("Knowledge-base watcher ingestion failed for %s: %s", path_text, exc)
        message = f"Knowledge-base auto-index failed for {Path(path_text).name}: {exc}"
    with _kb_watch_lock:
        _kb_watch_notifications.append(message)
        del _kb_watch_notifications[:-20]


class _KnowledgeBaseWatchHandler(FileSystemEventHandler):
    """Debounced watcher; intentionally only created by the web UI process."""

    def __init__(self) -> None:
        super().__init__()

    def on_created(self, event: Any) -> None:
        self._schedule(event)

    def on_modified(self, event: Any) -> None:
        self._schedule(event)

    def _schedule(self, event: Any) -> None:
        if getattr(event, "is_directory", False):
            return
        path = Path(str(getattr(event, "src_path", "")))
        if path.suffix.lower() not in {".pdf", ".txt", ".md", ".docx", ".xlsx", ".pptx"}:
            return
        key = str(path.resolve())
        with _kb_watch_lock:
            old_timer = _kb_watch_timers.pop(key, None)
            if old_timer is not None:
                old_timer.cancel()
            timer = threading.Timer(3.0, _run_knowledge_watch_ingest, args=(key,))
            timer.daemon = True
            _kb_watch_timers[key] = timer
            timer.start()


def _start_knowledge_base_watcher() -> None:
    """Start folder watching for the web UI only; CLI and scheduler stay manual."""
    global _kb_watch_observer
    try:
        from watchdog.observers import Observer
    except ImportError:
        logger.warning("Knowledge-base auto-watching is unavailable: install watchdog.")
        return
    ingest_tool = getattr(orchestrator.tools, "_tools", {}).get("ingest_knowledge_base")
    watch_dir = getattr(ingest_tool, "knowledge_base_dir", _ui_settings.workspace_root() / "knowledge_base")
    try:
        watch_dir.mkdir(parents=True, exist_ok=True)
        observer = Observer()
        observer.schedule(_KnowledgeBaseWatchHandler(), str(watch_dir), recursive=False)
        observer.start()
        _kb_watch_observer = observer
        logger.info("Knowledge-base watcher started for %s (web UI only).", watch_dir)
    except Exception as exc:
        logger.warning("Could not start knowledge-base watcher: %s", exc)
        return

    shutdown_hook = getattr(app, "on_shutdown", None)
    if callable(shutdown_hook):
        async def stop_knowledge_base_watcher() -> None:
            global _kb_watch_observer
            if _kb_watch_observer is not None:
                _kb_watch_observer.stop()
                _kb_watch_observer.join(timeout=5)
                _kb_watch_observer = None

        try:
            shutdown_hook(stop_knowledge_base_watcher)
        except Exception as exc:
            logger.warning("Could not register knowledge-base watcher shutdown hook: %s", exc)


_start_knowledge_base_watcher()


def _close_browser_session() -> None:
    """Close Playwright before the UI process exits to avoid a driver EPIPE."""
    try:
        from plugins.browser.plugin import BrowserSession

        instance = BrowserSession._instance
        if instance is not None:
            instance.close()
    except Exception as exc:
        # Shutdown is best-effort and must not obscure the original exit.
        logger.warning("Browser shutdown cleanup warning: %s", exc)


_shutdown_hook = getattr(app, "on_shutdown", None)
if callable(_shutdown_hook):
    try:
        _shutdown_hook(_close_browser_session)
    except Exception as exc:
        logger.warning("Could not register browser shutdown hook: %s", exc)


def _selection_args(selection: str) -> tuple[str | None, int | None]:
    if selection == "auto":
        return None, None
    if selection == "local":
        return "local", None
    if selection == "premium":
        return "premium", None
    if selection.startswith("free_api:"):
        return "free_api", int(selection.split(":", 1)[1])
    raise ValueError(f"Unknown model selection: {selection}")


VOICE_MODEL_ID = "nex-agi/nex-n2.5-pro:free"


def _voice_selection_args() -> tuple[str | None, int | None]:
    """Prefer the configured Nex OpenRouter entry, then use normal routing."""
    chain = _ui_settings.free_api_model.get("chain", [])
    if isinstance(chain, list):
        for index, entry in enumerate(chain):
            if isinstance(entry, dict) and entry.get("provider") == "openrouter" and entry.get("model") == VOICE_MODEL_ID:
                return "free_api", index
    return None, None


def _chat_markdown(content: str, rendered_image_paths: list[str] | None = None) -> str:
    """Normalize local image Markdown and avoid duplicate/broken images.

    The model sometimes echoes a tool path as Markdown.  A path such as
    ``generated_images/result.png`` is not a browser URL and produces a broken
    image icon.  Structured ``ui.image`` elements are preferred because they
    are clickable and use the dedicated static routes; when no structured
    image was captured, keep the Markdown image but rewrite it to that route.
    """
    normalized = content
    structured_urls = {
        image_url
        for image_path in rendered_image_paths or []
        if (image_url := _static_image_url(image_path))
    }

    def replace_image(match: re.Match[str]) -> str:
        url = _static_image_url(match.group("path"))
        if not url:
            return match.group(0)
        if url in structured_urls:
            return ""
        return f"![{match.group('alt')}]({url})"

    return _LOCAL_IMAGE_MARKDOWN.sub(replace_image, normalized)


def _static_image_url(path: str) -> str | None:
    """Map only known workspace image subfolders to their dedicated URL routes."""
    normalized = str(path).replace("\\", "/").lstrip("./")
    for folder, route in (
        ("generated_images", "/generated-images"),
        ("generated-images", "/generated-images"),
        ("screenshots", "/screenshots"),
    ):
        marker = f"/{folder}/"
        start = normalized.find(marker)
        if start < 0:
            if normalized.startswith(f"{folder}/"):
                start = -1
                filename = normalized[len(folder) + 1 :]
            else:
                continue
        else:
            filename = normalized[start + len(marker) :]
        if not filename or "/" in filename or filename in {".", ".."} or ".." in filename:
            continue
        if not re.search(r"\.(?:png|jpe?g|gif|webp)$", filename, re.IGNORECASE):
            continue
        return f"{route}/{quote(filename)}"
    return None


def _clear_finished_request(task: asyncio.Task[str], request_key: tuple[str, str] | None = None) -> None:
    """Release a request channel after a delayed worker finally exits."""
    global _active_request_task, _active_session_id
    try:
        error = task.exception()
        if error is not None:
            logger.warning("Assistant request ended after the UI stopped waiting: %s", error)
    except asyncio.CancelledError:
        pass
    if request_key is not None and _request_tasks.get(request_key) is task:
        _request_tasks.pop(request_key, None)
    if _active_request_task is task:
        # Keep the dispatcher attached to the session while the other channel
        # is still running; typed and voice calls are independent UI lanes.
        _active_request_task = next(iter(_request_tasks.values()), None)
        if _active_request_task is None:
            _active_session_id = None


def _store_background_result(
    session_id: str,
    task: asyncio.Task[str],
    *,
    voice_request: bool = False,
) -> None:
    """Capture a late worker result without touching NiceGUI from a callback.

    NiceGUI elements belong to the page/client event context.  A task done
    callback can run after the original request handler has returned, so UI
    mutations from that callback are unreliable.  The client-scoped timer
    consumes this payload later and performs the actual render safely.
    """
    try:
        reply = task.result()
        payload = {
            "reply": reply,
            "model_used": orchestrator.get_last_model_used(session_id),
            "images": orchestrator.get_last_images(session_id),
            "error": None,
            "voice_request": voice_request,
        }
    except Exception as exc:
        traceback.print_exc()
        payload = {
            "reply": f"[error] {exc}",
            "model_used": orchestrator.get_last_model_used(session_id),
            "images": [],
            "error": str(exc),
            "voice_request": voice_request,
        }
    with _background_results_lock:
        _background_results[session_id] = payload


def _take_background_result(session_id: str) -> dict[str, Any] | None:
    with _background_results_lock:
        return _background_results.pop(session_id, None)


def _available_port(start: int = 8080) -> int:
    """Use the documented port when free, otherwise choose the next free port."""
    port = start
    while port < start + 100:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                # NiceGUI binds all interfaces, so probe the same wildcard
                # address; Windows otherwise permits a misleading localhost
                # bind while 0.0.0.0 is already occupied.
                probe.bind(("0.0.0.0", port))
            except OSError:
                port += 1
                continue
        return port
    raise RuntimeError(f"No available port found in range {start}-{start + 99}")


def _render_history(chat_log: ui.column, history: list[dict[str, Any]]) -> None:
    chat_log.clear()
    with chat_log:
        if not history:
            # Keep an empty conversation surface genuinely empty. The composer
            # is the starting point, so a large onboarding card only consumes
            # useful chat space once the interface is familiar.
            return
        for index, entry in enumerate(history):
            sent = entry["role"] == "user"
            rendered_image_paths = list(entry.get("images", [])) if not sent else []
            message_class = "message-user" if sent else "message-assistant"
            if rendered_image_paths:
                message_class += " message-with-image"
            # History is re-rendered as a whole. Animate only its newest item
            # so fresh replies feel responsive without replaying motion for
            # the rest of the conversation on each UI refresh.
            if index == len(history) - 1:
                message_class += " message-enter"
            with ui.column().classes(f"assistant-message {message_class}"):
                ui.label("You" if sent else "Neural").classes("chat-role-label")
                # Keep reply content, generated media, and its routing label in one
                # visual bubble. NiceGUI's default chat-message creates a separate
                # bubble for each child, which previously split the model label out.
                with ui.column().classes("chat-bubble"):
                    ui.markdown(_chat_markdown(entry["content"], rendered_image_paths)).classes("chat-markdown")
                    if not sent:
                        for image_path in entry.get("images", []):
                            image_url = _static_image_url(image_path)
                            if image_url:
                                with ui.link("", image_url, new_tab=True).classes("chat-image-link"):
                                    ui.image(image_url).classes("chat-image")
                    if not sent and entry.get("model_used"):
                        ui.label(f"via {entry['model_used']}").classes("model-used-caption")


def _show_confirmation(session_id: str, dialog_holder: dict[str, Any]) -> None:
    with _pending_lock:
        pending = _pending_confirmations.get(session_id)
        if not pending or pending.get("shown") or pending.get("result") is not None:
            return
        pending["shown"] = True

    dialog = ui.dialog()
    dialog_holder["dialog"] = dialog
    with dialog, ui.card().classes("min-w-[28rem] approval-dialog-card"):
        ui.label("Approval required").classes("text-lg font-semibold")
        ui.label("The assistant wants to run:")
        ui.code(pending["command"]).classes("w-full whitespace-pre-wrap")
        with ui.row().classes("justify-end w-full"):
            def decide(result: bool) -> None:
                with _pending_lock:
                    current = _pending_confirmations.get(session_id)
                    if current is None or current.get("result") is not None:
                        return
                    current["result"] = result
                    current["event"].set()
                dialog.close()

            ui.button("Deny", on_click=lambda: decide(False)).props("flat")
            ui.button("Approve", on_click=lambda: decide(True), color="primary")
    dialog.open()


@ui.page("/")
def main() -> None:
    client = app.storage.client
    # ``client`` is the page connection's working state. Mirror the sidebar
    # preference into NiceGUI's tab storage as well so a websocket reconnect or
    # page reload in this browser tab does not unexpectedly reopen it.
    try:
        tab_storage = app.storage.tab
    except RuntimeError:
        tab_storage = None
    client.setdefault("session_id", str(uuid.uuid4()))
    client.setdefault("chat_history", [])
    client.setdefault("approval_mode", "auto")
    client.setdefault("selected_tier", "auto")
    client.setdefault("read_replies_aloud", False)
    # Voice prompts should reach the model by default. Users can still turn
    # this off with the send-toggle when they want microphone dictation only.
    # The version marker upgrades existing browser sessions that were created
    # while the old transcript-only default was active.
    if client.get("voice_auto_send_default_version") != 2:
        client["auto_send_voice_commands"] = True
        client["voice_auto_send_default_version"] = 2
    else:
        client.setdefault("auto_send_voice_commands", True)
    # A browser recognition session cannot survive a reconnect, so never show
    # hands-free as enabled until this page explicitly starts it again.
    client["hands_free_enabled"] = False
    if tab_storage is not None and "left_sidebar_visible" in tab_storage:
        client["left_sidebar_visible"] = bool(tab_storage["left_sidebar_visible"])
    else:
        client.setdefault("left_sidebar_visible", False)
        if tab_storage is not None:
            tab_storage["left_sidebar_visible"] = bool(client["left_sidebar_visible"])
    if client["selected_tier"] not in MODEL_OPTIONS:
        client["selected_tier"] = "auto"
    session_id = client["session_id"]
    _session_confirm_fns.setdefault(session_id, _make_confirm_fn(session_id))
    _approval_modes.setdefault(session_id, client["approval_mode"])

    ui.dark_mode().enable()
    ui.colors(primary="#d1202c", secondary="#7f1d1d")
    ui.page_title(PAGE_TITLE)
    ui.add_css("""
        :root { color-scheme: dark; --ink: #f8fafc; --muted: #a8afb9; --panel: #1a1b1e; --panel-2: #222326; --line: rgba(255,255,255,.08); --crimson: #d1202c; }
        body, .q-page, .q-page-container, .q-layout { background: radial-gradient(circle at 78% -18%, rgba(120, 18, 28, .23), transparent 30rem), #111214; color: var(--ink); }
        .q-drawer { background: linear-gradient(180deg, #1f2023 0%, #18191c 100%); box-shadow: 16px 0 40px rgba(0,0,0,.26); }
        .assistant-sidebar { border-right: 1px solid rgba(209,32,44,.30); }
        .assistant-activity-drawer { border-left: 1px solid rgba(209,32,44,.30); box-shadow: -16px 0 40px rgba(0,0,0,.26); }
        .assistant-sidebar { height: 100%; }
        .sidebar-content { width: 100%; height: 100%; gap: 1rem; }
        .sidebar-card { width: 100%; padding: 1rem; border: 1px solid rgba(255,255,255,.075); border-radius: 8px; background: rgba(255,255,255,.025); box-shadow: 0 8px 22px rgba(0,0,0,.08); }
        .sidebar-card-title, .sidebar-section-label { margin: 0 0 .5rem; color: #e7e9ed; font-size: .78rem; font-weight: 750; letter-spacing: .08em; text-transform: uppercase; }
        .sidebar-subsection-label { margin: .9rem 0 .38rem; color: #cbd1d9; font-size: .68rem; font-weight: 700; letter-spacing: .055em; text-transform: uppercase; }
        .assistant-brand { gap: .8rem; margin: .35rem 0 1.75rem; }
        .assistant-brand-mark { width: 42px; height: 42px; border-radius: 13px; display: flex; align-items: center; justify-content: center; color: #fff; background: linear-gradient(145deg, #ef3340, #8f101b); box-shadow: 0 10px 24px rgba(209,32,44,.25); }
        .assistant-brand-name { font-size: 1.05rem; font-weight: 700; letter-spacing: -.01em; }
        .assistant-brand-copy, .setting-copy, .sidebar-footer-copy, .assistant-subtitle, .compose-helper, .assistant-empty-copy { color: var(--muted); font-size: .78rem; line-height: 1.45; }
        .setting-copy { margin-bottom: .8rem; }
        .approval-toggle { display: inline-flex; padding: .25rem; border: 1px solid var(--line); border-radius: 10px; background: #151619; }
        .approval-toggle .q-btn { min-height: 34px; border-radius: 7px; color: #cbd1d9; font-size: .74rem; font-weight: 700; letter-spacing: .04em; }
        .approval-toggle .q-btn.bg-primary, .q-btn.bg-primary { background: linear-gradient(135deg, #e1323e, #b31320) !important; color: #fff !important; box-shadow: 0 7px 16px rgba(209,32,44,.22); }
        .assistant-tier { margin-top: .2rem; }
        .assistant-tier .q-field__control { min-height: 50px; background: #28292d; border: 1px solid rgba(255,255,255,.06); border-radius: 10px; }
        .assistant-tier .q-field__native, .assistant-tier .q-field__label, .assistant-tier .q-field__marginal { color: #f3f4f6 !important; }
        .sidebar-footer { margin-top: auto; padding: 1rem; border: 1px solid rgba(209,32,44,.18); border-radius: 12px; background: rgba(209,32,44,.06); }
        .sidebar-footer-title { margin-bottom: .28rem; color: #fff; font-size: .8rem; font-weight: 700; }
        .assistant-header { width: min(860px, calc(100% - 2rem)); margin: 1.6rem auto .45rem; padding: 0 .25rem; }
        .sidebar-toggle { flex: 0 0 auto; color: #f4f5f7; background: rgba(209,32,44,.13); border: 1px solid rgba(209,32,44,.25); border-radius: 9px; }
        .assistant-header-icon { color: #ff4d57; font-size: 1.55rem; }
        .assistant-title { color: #fff; font-size: 1.55rem; font-weight: 750; letter-spacing: -.035em; line-height: 1.15; }
        .assistant-status { padding: .36rem .62rem; border: 1px solid rgba(105, 223, 153, .24); border-radius: 999px; color: #9ce3ba; background: rgba(74, 222, 128, .08); font-size: .68rem; font-weight: 700; letter-spacing: .07em; }
        .assistant-status-dot { color: #5ee28b; font-size: .62rem; }
        .assistant-chat { width: min(860px, calc(100% - 2rem)); height: calc(100vh - 13.4rem); margin: 0 auto; padding: 1.2rem .25rem 8rem; overflow-y: auto; gap: 1.15rem; }
        .assistant-chat::-webkit-scrollbar { width: 8px; }
        .assistant-chat::-webkit-scrollbar-thumb { background: #3b3d42; border-radius: 999px; }
        .assistant-empty { align-self: center; width: min(420px, 100%); margin: auto; padding: 2.5rem 2rem; border: 1px solid var(--line); border-radius: 18px; background: linear-gradient(145deg, rgba(38,39,43,.88), rgba(27,28,31,.88)); text-align: center; box-shadow: 0 18px 45px rgba(0,0,0,.16); }
        .assistant-empty-icon { margin-bottom: .8rem; color: #f0444f; font-size: 2rem; }
        .assistant-empty-title { color: #fff; font-size: 1.05rem; font-weight: 700; }
        .assistant-empty-copy { margin-top: .4rem; }
        .assistant-message { width: 100%; margin: 0; }
        .assistant-message .q-message-container { max-width: 100%; }
        .assistant-message .q-message-name { margin: 0 0 .35rem .25rem; color: #aeb4be; font-size: .7rem; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; }
        .assistant-message .q-message-text { width: fit-content !important; max-width: 76% !important; min-width: 122px; margin: 0; word-break: normal; overflow-wrap: anywhere; }
        .assistant-message .q-message-text-content { width: 100%; padding: .9rem 1rem; border-radius: 14px; box-shadow: 0 8px 20px rgba(0,0,0,.14); }
        .model-used-caption { margin: .35rem 0 0 .25rem; color: #858b96; font-size: .68rem; }
        .chat-image-link { display: block; width: 100%; max-width: 400px; margin: .85rem 0 .2rem .25rem; }
        .chat-image { display: block; width: 100%; max-width: 400px; aspect-ratio: 1 / 1; border: 1px solid rgba(209,32,44,.3); border-radius: 12px; object-fit: contain; box-shadow: 0 10px 24px rgba(0,0,0,.24); }
        .chat-image .q-img__image { object-fit: contain !important; }
        .message-with-image .q-message-text { width: min(420px, 76%) !important; }
        .message-user .q-message-text, .message-user .q-message-text--sent, .message-user .q-message-text-content, .message-user .q-message-text-content--sent { background: linear-gradient(145deg, #56191e, #3b1115) !important; color: #fff !important; }
        .message-assistant .q-message-text, .message-assistant .q-message-text--received, .message-assistant .q-message-text-content, .message-assistant .q-message-text-content--received { background: #24262a !important; color: #f5f6f8 !important; }
        .message-assistant .q-message-text { width: min(76%, 780px) !important; min-width: 190px; border: 1px solid rgba(255,255,255,.055); }
        .message-user .q-message-text { border: 1px solid rgba(255,101,112,.14); }
        .assistant-message .q-message-text:last-child:before { display: none !important; }
        .chat-markdown { max-width: 100%; overflow-x: auto; line-height: 1.56; }
        .chat-markdown, .chat-markdown * { color: inherit; }
        .chat-markdown p:first-child { margin-top: 0; }
        .chat-markdown p:last-child { margin-bottom: 0; }
        .chat-markdown ul, .chat-markdown ol { padding-left: 1.25rem; }
        .chat-markdown blockquote { margin: .75rem 0; padding-left: .85rem; border-left: 3px solid #d1202c; color: #d8dbe1; }
        .chat-markdown pre { margin: .8rem 0; background: #151619; border: 1px solid rgba(255,255,255,.08); border-radius: 8px; padding: .75rem; overflow-x: auto; }
        .chat-markdown code { background: #16171a; border: 1px solid rgba(255,255,255,.07); border-radius: 5px; padding: .1rem .3rem; }
        .chat-markdown table { width: 100%; min-width: 460px; border-collapse: separate; border-spacing: 0; margin: .85rem 0; overflow: hidden; border: 1px solid rgba(209,32,44,.35); border-radius: 9px; }
        .chat-markdown th, .chat-markdown td { border-right: 1px solid rgba(209,32,44,.22); border-bottom: 1px solid rgba(209,32,44,.22); padding: .58rem .68rem; text-align: left; vertical-align: top; }
        .chat-markdown th:last-child, .chat-markdown td:last-child { border-right: 0; }
        .chat-markdown tr:last-child td { border-bottom: 0; }
        .chat-markdown th { background: #591d22; color: #fff; font-size: .78rem; }
        .chat-markdown td { background: rgba(255,255,255,.015); }
        .assistant-input { position: fixed; bottom: 0; left: 0; right: 300px; z-index: 10; padding: .85rem 1.1rem .9rem; background: rgba(23,24,27,.96); border-top: 1px solid rgba(209,32,44,.3); box-shadow: 0 -12px 30px rgba(0,0,0,.22); backdrop-filter: blur(18px); }
        .assistant-input.left-sidebar-open { left: 300px; }
        .assistant-compose-inner { width: min(860px, 100%); margin: 0 auto; gap: .38rem; }
        .assistant-compose-row { width: 100%; gap: .7rem; }
        .assistant-compose-icon { margin-left: .25rem; color: #d1202c; }
        .assistant-input-field { background: #292a2e; border-radius: 11px; }
        .assistant-input-field .q-field__control { background: #292a2e !important; min-height: 50px; border: 1px solid rgba(255,255,255,.09); border-radius: 11px; }
        .assistant-input-field.q-field--focused .q-field__control { border-color: #d1202c; box-shadow: 0 0 0 3px rgba(209,32,44,.13); }
        .assistant-input-field input, .assistant-input-field .q-field__native { color: #f8fafc !important; -webkit-text-fill-color: #f8fafc !important; caret-color: #fff !important; opacity: 1 !important; }
        .assistant-input-field input::placeholder { color: #9da4af !important; opacity: 1; }
        .assistant-send { min-height: 50px; min-width: 108px; padding: 0 1.1rem; border-radius: 10px; color: #fff !important; font-weight: 700; letter-spacing: .01em; }
        .voice-input-button { min-height: 50px; min-width: 50px; color: #cdd2da; }
        .voice-input-button.voice-recording { color: #ff5963; background: rgba(209,32,44,.18); }
        .knowledge-base { gap: .7rem; }
        .knowledge-upload-zone { width: 100%; padding: .55rem; border: 1px solid rgba(209,32,44,.18); border-radius: 7px; background: rgba(209,32,44,.035); }
        .knowledge-upload { width: 100%; border: 1px dashed rgba(209,32,44,.5); border-radius: 10px; background: rgba(209,32,44,.06); }
        .knowledge-upload .q-uploader__header { background: linear-gradient(135deg, #9e1822, #6d1018); }
        .knowledge-file-list { width: 100%; gap: .35rem; padding: .6rem; border: 1px solid rgba(255,255,255,.07); border-radius: 7px; background: rgba(0,0,0,.13); }
        .knowledge-file { width: 100%; min-width: 0; padding: .38rem .45rem; border-radius: 6px; background: rgba(255,255,255,.045); color: #d9dde4; font-size: .75rem; }
        .knowledge-file .q-icon { color: #65d993; font-size: 1rem; }
        .knowledge-file-name-wrap { min-width: 0; flex: 1 1 auto; }
        .knowledge-file-name { display: block; max-width: 100%; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .knowledge-file-tooltip { max-width: 260px; white-space: normal; overflow-wrap: anywhere; }
        .knowledge-reindex { width: 100%; margin-top: .7rem; }
        .productivity-card { gap: .55rem; }
        .productivity-copy { color: var(--muted); font-size: .7rem; line-height: 1.4; }
        .productivity-list { width: 100%; gap: .3rem; max-height: 10rem; overflow-y: auto; }
        .productivity-item { width: 100%; min-width: 0; padding: .38rem .45rem; border-radius: 6px; background: rgba(255,255,255,.045); }
        .productivity-item-icon { color: #d96a72; font-size: .85rem; }
        .productivity-item-delivered { color: #65d993; }
        .productivity-item-title { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #d9dde4; font-size: .72rem; }
        .productivity-item-meta { color: #8e96a2; font-size: .62rem; white-space: nowrap; }
        .productivity-empty { color: #8e96a2; font-size: .68rem; }
        .compose-helper { padding-left: 2rem; }
        .activity-header { width: 100%; padding: .35rem 0 .9rem; border-bottom: 1px solid var(--line); }
        .activity-title { color: #fff; font-size: .93rem; font-weight: 750; }
        .activity-copy { margin-top: .24rem; color: var(--muted); font-size: .73rem; line-height: 1.42; }
        .activity-live { padding: .25rem .45rem; border: 1px solid rgba(105,223,153,.22); border-radius: 999px; color: #9ce3ba; background: rgba(74,222,128,.08); font-size: .62rem; font-weight: 750; letter-spacing: .07em; }
        .image-setup-card { width: 100%; margin: .9rem 0 .15rem; padding: .75rem; border: 1px solid var(--line); border-radius: 11px; background: rgba(255,255,255,.025); gap: .45rem; }
        .image-setup-card.image-setup-ready { border-color: rgba(101,217,147,.28); background: rgba(74,222,128,.045); }
        .image-setup-card.image-setup-processing, .image-setup-card.image-setup-starting { border-color: rgba(242,201,76,.32); background: rgba(242,201,76,.055); }
        .image-setup-card.image-setup-failed { border-color: rgba(255,89,99,.3); background: rgba(209,32,44,.065); }
        .image-setup-card.image-setup-not_installed { border-color: rgba(168,175,185,.23); }
        .image-setup-icon { font-size: 1rem; }
        .image-setup-ready .image-setup-icon { color: #65d993; }
        .image-setup-processing .image-setup-icon, .image-setup-starting .image-setup-icon { color: #f2c94c; }
        .image-setup-failed .image-setup-icon { color: #ff6670; }
        .image-setup-state { padding: .18rem .38rem; border-radius: 999px; font-size: .58rem; font-weight: 800; letter-spacing: .06em; background: rgba(255,255,255,.07); color: #c7cdd6; }
        .image-setup-ready .image-setup-state { color: #9ce3ba; background: rgba(74,222,128,.12); }
        .image-setup-processing .image-setup-state, .image-setup-starting .image-setup-state { color: #f7db7c; background: rgba(242,201,76,.12); }
        .image-setup-failed .image-setup-state { color: #ff9aa1; background: rgba(255,89,99,.12); }
        .image-setup-title { color: #f1f3f5; font-size: .77rem; font-weight: 750; }
        .image-setup-detail, .image-setup-meta { color: #aeb5c0; font-size: .69rem; line-height: 1.42; overflow-wrap: anywhere; }
        .image-setup-meta { color: #777f8b; font-size: .62rem; }
        .image-setup-log { width: 100%; max-height: 7.4rem; overflow-y: auto; margin: .1rem 0 0; padding: .48rem; border-radius: 7px; background: rgba(0,0,0,.24); color: #b8c0cb; font-family: Consolas, monospace; font-size: .59rem; line-height: 1.45; white-space: pre-wrap; overflow-wrap: anywhere; }
        .image-setup-retry { width: 100%; margin-top: .1rem; color: #fff; background: rgba(209,32,44,.82); font-size: .67rem; }
        .activity-feed { width: 100%; gap: .65rem; padding: 1rem 0; overflow-y: auto; }
        .activity-empty { padding: 1rem .2rem; color: var(--muted); font-size: .78rem; line-height: 1.5; }
        .activity-event { width: 100%; gap: .65rem; padding: .7rem; border: 1px solid var(--line); border-radius: 11px; background: rgba(255,255,255,.025); }
        .activity-event-icon { margin-top: .05rem; font-size: 1rem; }
        .activity-event-active .activity-event-icon { color: #f2c94c; }
        .activity-event-waiting { border-color: rgba(242,201,76,.34); background: rgba(242,201,76,.06); }
        .activity-event-waiting .activity-event-icon { color: #f2c94c; }
        .activity-event-done .activity-event-icon { color: #65d993; }
        .activity-event-error { border-color: rgba(255,89,99,.26); background: rgba(209,32,44,.06); }
        .activity-event-error .activity-event-icon { color: #ff6670; }
        .activity-event-voice { border-color: rgba(161,116,232,.34); background: rgba(111,70,170,.13); }
        .activity-event-voice .activity-event-icon { color: #c39aff; }
        .activity-event-voice.activity-event-active { border-color: rgba(196,154,255,.42); background: rgba(111,70,170,.18); }
        .activity-event-voice.activity-event-waiting { border-color: rgba(196,154,255,.34); background: rgba(111,70,170,.14); }
        .activity-event-voice.activity-event-done { border-color: rgba(108,190,255,.32); background: rgba(47,116,164,.14); }
        .activity-event-voice.activity-event-done .activity-event-icon { color: #79c8ff; }
        .activity-event-voice.activity-event-error { border-color: rgba(255,126,174,.38); background: rgba(139,52,105,.16); }
        .activity-event-voice.activity-event-error .activity-event-icon { color: #ff8fbd; }
        .activity-event-title { color: #f1f3f5; font-size: .77rem; font-weight: 700; line-height: 1.35; }
        .activity-event-detail { margin-top: .18rem; color: #aeb5c0; font-size: .7rem; line-height: 1.42; overflow-wrap: anywhere; }
        .activity-event-time { color: #747b86; font-size: .62rem; white-space: nowrap; }
        .activity-clear { margin-top: auto; width: 100%; color: #b8bec7; }
        @media (max-width: 900px) {
            .assistant-header, .assistant-chat { width: calc(100% - 2rem); }
            .assistant-header { margin-top: 1rem; }
            .assistant-status { display: none; }
            .assistant-input { left: 0; padding: .75rem 1rem; }
            .assistant-message .q-message-text, .message-assistant .q-message-text { max-width: 88% !important; }
        }
        @media (max-width: 1200px) and (min-width: 901px) {
            .assistant-header, .assistant-chat { width: min(760px, calc(100% - 2rem)); }
        }
    """)
    ui.add_css("""
        :root {
            --space-1: 4px;
            --space-2: 8px;
            --space-3: 16px;
            --space-4: 24px;
            --radius: 10px;
            --elevation: 0 2px 8px rgba(0,0,0,.40);
        }
        body, .q-page, .q-page-container, .q-layout {
            background:
                radial-gradient(circle at 50% -12%, rgba(126, 23, 33, .16), transparent 34rem),
                linear-gradient(145deg, #0d0d0d 0%, #111010 52%, #161010 100%);
        }
        .q-drawer { background: linear-gradient(180deg, rgba(31,32,35,.97), rgba(22,22,24,.98)); }
        .sidebar-content { gap: var(--space-3); }
        .sidebar-card, .image-setup-card, .activity-event, .assistant-empty,
        .chat-bubble, .assistant-input {
            border-radius: var(--radius);
            box-shadow: var(--elevation);
        }
        .sidebar-card { padding: var(--space-3); background: rgba(255,255,255,.032); }
        .sidebar-card-title, .sidebar-section-label {
            margin-bottom: var(--space-2);
            font-size: .76rem;
            letter-spacing: .09em;
        }
        .sidebar-subsection-label { margin: var(--space-3) 0 var(--space-1); }
        .assistant-brand { margin: var(--space-1) 0 var(--space-3); }
        .assistant-brand-mark, .sidebar-toggle { border-radius: var(--radius); }
        .assistant-tier .q-field__control, .assistant-input-field,
        .assistant-input-field .q-field__control, .knowledge-upload-zone,
        .knowledge-upload, .knowledge-file-list, .knowledge-file,
        .productivity-item, .chat-image, .chat-markdown pre,
        .chat-markdown table, .approval-toggle, .image-setup-log {
            border-radius: var(--radius);
        }
        .assistant-header, .assistant-chat { width: min(1180px, calc(100% - 3rem)); }
        .assistant-header { margin-top: var(--space-4); margin-bottom: var(--space-2); }
        .assistant-chat { gap: var(--space-3); padding-top: var(--space-3); }
        .assistant-input { right: 360px; border-radius: 0; }
        .assistant-input.left-sidebar-open { left: 280px; }
        .assistant-compose-inner { width: min(1180px, 100%); }
        .assistant-send, .knowledge-reindex, .image-setup-retry,
        .q-btn:not(.q-btn--round) { border-radius: var(--radius); }
        .knowledge-upload-zone { padding: var(--space-2); background: rgba(209,32,44,.025); }
        .knowledge-file-list, .productivity-list {
            gap: var(--space-1);
            padding: var(--space-2);
            border: 1px solid rgba(255,255,255,.07);
            background: rgba(0,0,0,.14);
            border-radius: var(--radius);
        }
        .knowledge-file, .productivity-item { padding: var(--space-2); border-radius: var(--radius); }
        .productivity-card { gap: var(--space-2); }
        .activity-feed { gap: var(--space-2); padding: var(--space-3) 0; }
        .activity-event { padding: var(--space-3); border-radius: var(--radius); }
        .image-setup-card { padding: var(--space-3); border-radius: var(--radius); }
        .assistant-status, .activity-live { box-shadow: 0 0 0 1px rgba(105,223,153,.05); }
        .voice-output-toggle { color: #8e96a2; }
        .voice-output-toggle.is-active { color: #ff5661; background: rgba(209,32,44,.14); }
        .voice-output-toggle .q-icon { font-size: 1.12rem; }
        .voice-play-button, .voice-stop-button, .hands-free-button, .voice-auto-send-toggle { color: #9ca3af; }
        .voice-play-button:hover, .hands-free-button:hover, .voice-auto-send-toggle.is-active { color: #ff6872; background: rgba(209,32,44,.14); }
        .hands-free-button.hands-free-active { color: #f7db7c; background: rgba(242,201,76,.14); }
        .speech-indicator { padding: .2rem .42rem; border-radius: 999px; color: #ff9aa1; background: rgba(209,32,44,.12); font-size: .58rem; font-weight: 800; letter-spacing: .07em; }
        .voice-input-button.voice-recording { color: #ff6670; background: rgba(209,32,44,.18); }
        .q-tooltip { max-width: min(360px, calc(100vw - 2rem)) !important; white-space: normal !important; overflow-wrap: anywhere; line-height: 1.35; }
        .q-notification { max-width: min(440px, calc(100vw - 2rem)) !important; }
        .q-notification__message { white-space: normal !important; overflow-wrap: anywhere; line-height: 1.35; }
        .assistant-header { flex-wrap: wrap !important; column-gap: var(--space-3); row-gap: var(--space-2); }
        .sidebar-toggle { position: relative; z-index: 2; margin-right: var(--space-2); pointer-events: auto; }
        .neural-logo { margin-left: var(--space-1); color: #ff4d57; pointer-events: none; filter: drop-shadow(0 0 8px rgba(209,32,44,.26)); }
        .header-behavior-controls { min-width: 0; gap: var(--space-2); }
        .header-approval-toggle { flex: 0 0 auto; max-width: 136px; overflow: hidden; }
        .header-approval-toggle .q-btn { min-height: 34px; padding: 0 .55rem; font-size: .68rem; }
        .header-tier { width: 160px; min-width: 0; }
        .header-tier .q-field__control { min-height: 38px; background: rgba(36,37,41,.94); border-radius: var(--radius); }
        .header-tier .q-field__control-container { padding-top: 0 !important; }
        .header-tier .q-field__native, .header-tier .q-field__native > span {
            min-height: 38px;
            display: flex;
            align-items: center;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .assistant-status.service-ready { color: #9ce3ba; border-color: rgba(105,223,153,.24); background: rgba(74,222,128,.08); }
        .assistant-status.service-starting, .assistant-status.service-processing { color: #f7db7c; border-color: rgba(242,201,76,.34); background: rgba(242,201,76,.08); }
        .assistant-status.service-failed, .assistant-status.service-not_installed, .assistant-status.service-unknown { color: #ff9aa1; border-color: rgba(255,89,99,.32); background: rgba(209,32,44,.08); }
        .assistant-status.service-starting .assistant-status-dot, .assistant-status.service-processing .assistant-status-dot { color: #f2c94c; }
        .assistant-status.service-failed .assistant-status-dot, .assistant-status.service-not_installed .assistant-status-dot, .assistant-status.service-unknown .assistant-status-dot { color: #ff6670; }
        .assistant-message { width: 100%; gap: var(--space-1); }
        .assistant-message.message-user { align-items: flex-end; }
        .assistant-message.message-assistant { align-items: flex-start; }
        .chat-role-label { margin: 0 .3rem; color: #aeb4be; font-size: .7rem; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; }
        .chat-bubble { width: fit-content; max-width: min(90%, 1040px); gap: var(--space-2); padding: .95rem 1rem; border: 1px solid rgba(255,255,255,.06); }
        .message-assistant .chat-bubble { background: linear-gradient(145deg, #282a2e, #202226); color: #f5f6f8; }
        .message-user .chat-bubble { background: linear-gradient(145deg, #681e25, #401217); border-color: rgba(255,101,112,.18); color: #fff; }
        .model-used-caption { width: 100%; margin: var(--space-1) 0 0; padding-top: var(--space-2); border-top: 1px solid rgba(255,255,255,.08); color: #9299a4; font-size: .66rem; letter-spacing: .015em; }
        .chat-image-link { max-width: 620px; margin: var(--space-2) 0 0; }
        .chat-image { max-width: 620px; }
        .knowledge-base { gap: var(--space-2); }
        .knowledge-upload-compact { width: auto; min-width: 54px; border: 1px solid rgba(209,32,44,.38); background: rgba(209,32,44,.08); }
        .knowledge-upload-compact .q-uploader__header { min-height: 34px; padding: 0 .15rem; background: transparent; }
        .knowledge-upload-compact .q-uploader__title,
        .knowledge-upload-compact .q-uploader__subtitle { display: none; }
        .knowledge-upload-compact .q-btn { min-height: 34px; min-width: 34px; color: #fff; }
        .knowledge-reindex { width: auto; min-height: 34px; margin-top: 0; padding: 0 .7rem; font-size: .7rem; }
        .image-setup-detail { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
        .productivity-item { align-items: flex-start; }
        .productivity-item-content { flex: 1 1 auto; min-width: 0; }
        .productivity-item-title {
            width: 100%;
            display: -webkit-box;
            -webkit-box-orient: vertical;
            -webkit-line-clamp: 2;
            overflow: hidden;
            white-space: normal;
            line-height: 1.35;
        }
        .productivity-item-meta { margin-top: 2px; }
        .approval-dialog-card { border: 1px solid rgba(209,32,44,.34); border-radius: var(--radius); box-shadow: 0 20px 48px rgba(0,0,0,.48); }
        .q-dialog__backdrop { backdrop-filter: blur(3px); }
        .q-spinner { color: var(--crimson) !important; }
        @media (prefers-reduced-motion: no-preference) {
            .q-page-container { transition: padding-left 220ms ease-out, padding-right 220ms ease-out; }
            .assistant-sidebar, .assistant-activity-drawer { transition: transform 220ms ease-out, width 220ms ease-out; }
            .assistant-input { transition: left 220ms ease-out, right 220ms ease-out, box-shadow 180ms ease-out; }
            .assistant-input-field .q-field__control { transition: border-color 180ms ease-out, box-shadow 180ms ease-out; }
            .q-btn { transition: transform 120ms ease-out, filter 120ms ease-out, box-shadow 120ms ease-out; }
            .q-btn:hover { filter: brightness(1.08); transform: translateY(-1px); }
            .q-btn:active { filter: brightness(.94); transform: translateY(0) scale(.98); }
            .message-enter { animation: message-enter 180ms ease-out both; }
            .activity-enter { animation: activity-enter 170ms ease-out both; }
            .approval-dialog-card { animation: approval-enter 150ms ease-out both; }
            .assistant-status.service-ready { animation: live-pulse 2s ease-in-out infinite; }
            @keyframes message-enter { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
            @keyframes activity-enter { from { opacity: 0; transform: translateY(-7px); } to { opacity: 1; transform: translateY(0); } }
            @keyframes approval-enter { from { opacity: 0; transform: scale(.95); } to { opacity: 1; transform: scale(1); } }
            @keyframes live-pulse { 0%, 100% { box-shadow: 0 0 0 0 rgba(105,223,153,.07); } 50% { box-shadow: 0 0 14px 2px rgba(105,223,153,.16); } }
        }
        @media (max-width: 1200px) and (min-width: 901px) {
            .assistant-header, .assistant-chat { width: calc(100% - 2rem); }
            .assistant-header { flex-wrap: wrap !important; row-gap: var(--space-2); }
        }
        @media (max-width: 900px) {
            .assistant-input { right: 0; }
        }
        @media (max-width: 700px) {
            .assistant-header { width: calc(100% - 1.5rem); }
            .header-behavior-controls { width: 100%; flex-wrap: wrap !important; justify-content: flex-end; }
            .header-tier { flex: 1 1 145px; }
        }
    """)

    ingest_tool = getattr(orchestrator.tools, "_tools", {}).get("ingest_knowledge_base")
    knowledge_base_dir = getattr(ingest_tool, "knowledge_base_dir", _ui_settings.workspace_root() / "knowledge_base")
    knowledge_base_dir.mkdir(parents=True, exist_ok=True)

    def indexed_knowledge_files() -> list[str]:
        state = orchestrator.store.get_state("kb_file_hashes", {})
        return sorted(state.keys()) if isinstance(state, dict) else []

    knowledge_upload: Any | None = None

    async def save_knowledge_file(event: Any) -> None:
        original_name = str(getattr(event.file, "name", "document"))
        filename = Path(original_name).name
        try:
            if Path(filename).suffix.lower() not in {".pdf", ".txt", ".md", ".docx", ".xlsx", ".pptx"}:
                ui.notify("Only PDF, TXT, Markdown, DOCX, XLSX, and PPTX files are supported.", type="negative")
                return
            await event.file.save(str(knowledge_base_dir / filename))
            ui.notify(
                f"Saved {filename} — click Re-index to add it to the knowledge base.",
                type="positive",
            )
        except Exception as exc:
            traceback.print_exc()
            ui.notify(f"Could not save {filename}: {exc}", type="negative")
        finally:
            # QUploader keeps completed files in its queue by default. Clear
            # that client-side state so the drop zone returns to idle instead
            # of displaying the previous upload as permanently active.
            if knowledge_upload is not None:
                knowledge_upload.reset()

    reindex_state = {"busy": False}

    async def reindex_knowledge_base() -> None:
        if reindex_state["busy"]:
            return
        reindex_state["busy"] = True
        reindex_button.disable()
        reindex_spinner.set_visibility(True)
        try:
            result = await run.io_bound(orchestrator.tools.call, "ingest_knowledge_base")
            if not isinstance(result, dict) or result.get("error"):
                ui.notify(str(result.get("error", result)), type="negative")
            else:
                ingested = len(result.get("ingested_files", []))
                chunks = result.get("total_chunks_added", 0)
                skipped = len(result.get("skipped_unchanged", []))
                duplicates = len(result.get("duplicates", []))
                failed = len(result.get("failed_files", []))
                extra = f", {duplicates} duplicate(s)" if duplicates else ""
                extra += f", {failed} failed" if failed else ""
                ui.notify(
                    f"Ingested {ingested} new files, {chunks} chunks added, {skipped} unchanged skipped{extra}.",
                    type="positive",
                )
                refresh_knowledge_file_list()
        except Exception as exc:
            traceback.print_exc()
            ui.notify(f"Knowledge-base re-index failed: {exc}", type="negative")
        finally:
            reindex_state["busy"] = False
            reindex_spinner.set_visibility(False)
            reindex_button.enable()

    def refresh_knowledge_file_list() -> None:
        # The document list remains available through the knowledge-base tools,
        # but is intentionally not rendered in the compact sidebar treatment.
        # Keep this hook so upload, re-index, and folder-watch flows do not
        # need any functional changes when they refresh the presentation.
        return

    watch_notification_state = {"seen": 0}

    def refresh_knowledge_watch_notifications() -> None:
        with _kb_watch_lock:
            messages = list(_kb_watch_notifications)
        if watch_notification_state["seen"] > len(messages):
            watch_notification_state["seen"] = 0
        unseen = messages[watch_notification_state["seen"]:]
        watch_notification_state["seen"] = len(messages)
        for message in unseen:
            ui.notify(message, type="positive" if "failed" not in message.lower() else "negative")
            refresh_knowledge_file_list()

    task_panel: Any | None = None
    productivity_state = {
        "signature": "",
        "initialized": False,
        "notified_delivered_ids": set(),
        "session_started_at": time.time(),
    }

    def _local_time(timestamp: Any) -> str:
        try:
            return time.strftime("%b %d, %H:%M", time.localtime(float(timestamp)))
        except (TypeError, ValueError, OverflowError):
            return ""

    def refresh_productivity_panel() -> None:
        if task_panel is None:
            return
        try:
            # Claiming is atomic, so this works when the scheduler is not
            # running. If the scheduler wins the race, the delivered row is
            # still read below and surfaced in this browser session.
            claimed_reminders = orchestrator.store.claim_due_reminders()
            claimed_ids = {int(reminder["id"]) for reminder in claimed_reminders}
            delivered = orchestrator.store.list_reminders(status="delivered")
            delivered_ids = {int(reminder["id"]) for reminder in delivered}
            notified_ids = productivity_state["notified_delivered_ids"]
            session_started_at = productivity_state["session_started_at"]

            for reminder in delivered:
                reminder_id = int(reminder["id"])
                updated_at = reminder.get("updated_at")
                is_new_for_session = (
                    reminder_id in claimed_ids
                    or (
                        isinstance(updated_at, (int, float))
                        and float(updated_at) >= session_started_at
                    )
                )
                if reminder_id not in notified_ids and is_new_for_session:
                    ui.notify(
                        f"Reminder: {reminder['message']}",
                        type="warning",
                        position="top",
                        timeout=20000,
                    )
                    notified_ids.add(reminder_id)

            # Existing delivered reminders are useful history, but should not
            # produce a burst of notifications just because the page opened.
            if not productivity_state["initialized"]:
                notified_ids.update(
                    reminder_id
                    for reminder_id in delivered_ids
                    if reminder_id not in claimed_ids
                    and reminder_id not in notified_ids
                )
                productivity_state["initialized"] = True

            tasks = orchestrator.store.list_tasks(status="open")[:5]
            reminders = orchestrator.store.list_reminders(status="upcoming")[:5]
            recent_delivered = delivered[-5:]
            signature = repr((tasks, reminders, recent_delivered))
            if signature == productivity_state["signature"] and not claimed_reminders:
                return
            productivity_state["signature"] = signature
            task_panel.clear()
            with task_panel:
                task_title = ui.label("Tasks & Reminders").classes("sidebar-card-title")
                task_title.tooltip("Ask the assistant to create, update, or remind you.")
                ui.label("OPEN TASKS").classes("sidebar-subsection-label")
                with ui.column().classes("productivity-list"):
                    if not tasks:
                        ui.label("No open tasks.").classes("productivity-empty")
                    for task in tasks:
                        with ui.row().classes("productivity-item items-start no-wrap"):
                            ui.icon("check_box_outline_blank").classes("productivity-item-icon")
                            with ui.column().classes("productivity-item-content gap-0"):
                                ui.label(str(task.get("title") or task.get("description", ""))).classes("productivity-item-title")
                                if task.get("due_at"):
                                    ui.label(_local_time(task["due_at"])).classes("productivity-item-meta")
                ui.label("UPCOMING REMINDERS").classes("sidebar-subsection-label")
                with ui.column().classes("productivity-list"):
                    if not reminders:
                        ui.label("No upcoming reminders.").classes("productivity-empty")
                    for reminder in reminders:
                        with ui.row().classes("productivity-item items-start no-wrap"):
                            ui.icon("notifications_none").classes("productivity-item-icon")
                            with ui.column().classes("productivity-item-content gap-0"):
                                ui.label(str(reminder.get("message", ""))).classes("productivity-item-title")
                                ui.label(_local_time(reminder.get("remind_at"))).classes("productivity-item-meta")
                ui.label("RECENTLY DELIVERED").classes("sidebar-subsection-label")
                with ui.column().classes("productivity-list"):
                    if not recent_delivered:
                        ui.label("No reminders delivered yet.").classes("productivity-empty")
                    for reminder in reversed(recent_delivered):
                        with ui.row().classes("productivity-item items-start no-wrap"):
                            ui.icon("check_circle").classes("productivity-item-icon productivity-item-delivered")
                            with ui.column().classes("productivity-item-content gap-0"):
                                ui.label(str(reminder.get("message", ""))).classes("productivity-item-title")
                                ui.label(_local_time(reminder.get("remind_at"))).classes("productivity-item-meta")
        except Exception as exc:
            logger.warning("Could not refresh tasks and reminders: %s", exc)

    read_aloud_toggle: Any | None = None

    def toggle_read_replies_aloud() -> None:
        enabled = not bool(client["read_replies_aloud"])
        client["read_replies_aloud"] = enabled
        if read_aloud_toggle is not None:
            read_aloud_toggle.set_icon("volume_up" if enabled else "volume_off")
            read_aloud_toggle.classes(
                add="is-active" if enabled else "",
                remove="" if enabled else "is-active",
            )

    left_sidebar = ui.left_drawer(value=bool(client["left_sidebar_visible"])).props("behavior=desktop width=280").classes("p-4 assistant-sidebar")
    with left_sidebar:
        with ui.column().classes("sidebar-content"):
            task_panel = ui.column().classes("sidebar-card productivity-card")
            refresh_productivity_panel()

            with ui.column().classes("sidebar-card knowledge-base"):
                with ui.row().classes("items-center no-wrap w-full"):
                    knowledge_title = ui.label("Knowledge Base").classes("sidebar-card-title")
                    ui.space()
                    knowledge_upload = ui.upload(
                        on_upload=save_knowledge_file,
                        auto_upload=True,
                        label="Add",
                    ).props('accept=".pdf,.txt,.md,.docx,.xlsx,.pptx"').classes("knowledge-upload knowledge-upload-compact")
                knowledge_title.tooltip("Upload documents for local semantic search.")
                knowledge_upload.tooltip("Add PDF, TXT, Markdown, DOCX, XLSX, or PPTX documents.")
                with ui.row().classes("items-center no-wrap w-full"):
                    reindex_button = ui.button(
                        "Re-index",
                        icon="sync",
                        color="primary",
                        on_click=reindex_knowledge_base,
                    ).props("dense no-caps").classes("knowledge-reindex")
                    reindex_spinner = ui.spinner("dots", size="sm", color="primary")
                    reindex_spinner.set_visibility(False)

    activity_render_state = {"signature": ""}

    def render_activity() -> None:
        """Refresh only when the background worker has recorded a new event."""
        events = _activity_snapshot(session_id)
        signature = repr(events)
        if signature == activity_render_state["signature"]:
            return
        activity_render_state["signature"] = signature
        activity_feed.clear()
        with activity_feed:
            if not events:
                ui.label("No activity yet. Assistant decisions, tool approvals, and request completion will appear here.").classes("activity-empty")
                return
            icon_for_status = {
                "active": "hourglass_top",
                "waiting": "pending_actions",
                "done": "check_circle",
                "error": "error_outline",
            }
            for index, event in enumerate(reversed(events)):
                status = event["status"]
                entry_class = " activity-enter" if index == 0 else ""
                voice_class = " activity-event-voice" if event.get("kind") == "voice" else ""
                with ui.row().classes(f"activity-event activity-event-{status}{voice_class} no-wrap{entry_class}"):
                    ui.icon(icon_for_status.get(status, "info")).classes("activity-event-icon")
                    with ui.column().classes("gap-0 flex-grow"):
                        ui.label(event["title"]).classes("activity-event-title")
                        if event["detail"]:
                            ui.label(event["detail"][:240]).classes("activity-event-detail")
                    ui.label(event["time"]).classes("activity-event-time")

    def clear_activity_feed() -> None:
        _clear_activity(session_id)
        render_activity()

    image_setup_render_state = {"signature": "", "busy": False}
    header_status: Any | None = None
    header_status_label: Any | None = None

    def update_header_service_status(state: str) -> None:
        """Reflect the live image-service probe in the header, not a fixed badge."""
        if header_status is None or header_status_label is None:
            return
        labels = {
            "ready": "READY",
            "starting": "STARTING",
            "processing": "WORKING",
            "failed": "DEGRADED",
            "not_installed": "SETUP REQUIRED",
            "unknown": "CHECK STATUS",
        }
        header_status_label.set_text(labels.get(state, "CHECK STATUS"))
        header_status.classes(
            remove=(
                "service-ready service-starting service-processing service-failed "
                "service-not_installed service-unknown"
            ),
            add=f"service-{state}",
        )

    def render_image_setup(snapshot: dict[str, Any]) -> None:
        signature = repr(snapshot)
        if signature == image_setup_render_state["signature"]:
            return
        image_setup_render_state["signature"] = signature
        state = str(snapshot.get("state", "unknown")).lower()
        update_header_service_status(state)
        icon = {
            "ready": "check_circle",
            "processing": "downloading",
            "starting": "hourglass_top",
            "failed": "error_outline",
            "not_installed": "cloud_download",
        }.get(state, "help_outline")
        image_setup_card.clear()
        with image_setup_card.classes(remove="image-setup-ready image-setup-processing image-setup-starting image-setup-failed image-setup-not_installed", add=f"image-setup-{state}"):
            with ui.row().classes("items-center no-wrap w-full"):
                ui.icon(icon).classes("image-setup-icon")
                ui.label("Image generation").classes("image-setup-title")
                ui.space()
                ui.label(state.replace("_", " ").upper()).classes("image-setup-state")
            phase = str(snapshot.get("phase", "")).replace("_", " ").strip()
            if phase:
                ui.label(f"Phase: {phase.title()}").classes("image-setup-meta")
            ui.label(str(snapshot.get("detail", "Status unavailable."))).classes("image-setup-detail")
            if snapshot.get("can_retry"):
                ui.button("Retry image setup", icon="refresh", color="primary", on_click=retry_image_setup).props("no-caps").classes("image-setup-retry")

    async def refresh_image_setup() -> None:
        if image_setup_render_state["busy"]:
            return
        image_setup_render_state["busy"] = True
        try:
            status_fn = getattr(_image_generation_tool, "get_setup_status", None)
            if callable(status_fn):
                snapshot = await run.io_bound(status_fn)
            else:
                snapshot = {"state": "unknown", "detail": "The image-generation plugin is unavailable."}
            render_image_setup(snapshot)
        except Exception as exc:
            logger.warning("Could not refresh image setup status: %s", exc)
            render_image_setup({"state": "failed", "detail": f"Could not read image setup status: {exc}"})
        finally:
            image_setup_render_state["busy"] = False

    async def retry_image_setup() -> None:
        retry_fn = getattr(_image_generation_tool, "retry_setup", None)
        if not callable(retry_fn):
            ui.notify("Image setup is unavailable.", type="negative")
            return
        try:
            snapshot = await run.io_bound(retry_fn)
            render_image_setup(snapshot)
            ui.notify("Image setup has been started in the background.", type="positive")
        except Exception as exc:
            logger.exception("Could not retry image setup")
            ui.notify(f"Could not retry image setup: {exc}", type="negative")

    with ui.right_drawer(value=True).props("behavior=desktop width=360").classes("p-4 assistant-activity-drawer"):
        with ui.row().classes("activity-header items-start no-wrap"):
            with ui.column().classes("gap-0"):
                activity_title = ui.label("Live activity").classes("activity-title")
                activity_title.tooltip("Real-time assistant and tool progress")
        image_setup_card = ui.column().classes("image-setup-card")
        activity_feed = ui.column().classes("activity-feed")
        ui.button("Clear activity", icon="cleaning_services", on_click=clear_activity_feed).props("flat no-caps").classes("activity-clear")
        render_activity()
        render_image_setup({"state": "starting", "detail": "Checking local ComfyUI status…"})

    def toggle_left_sidebar() -> None:
        visible = not bool(client.get("left_sidebar_visible", False))
        client["left_sidebar_visible"] = visible
        if tab_storage is not None:
            tab_storage["left_sidebar_visible"] = visible
        if visible:
            left_sidebar.show()
        else:
            left_sidebar.hide()
        left_sidebar_toggle.set_icon("menu_open" if visible else "menu")
        assistant_input_bar.classes(
            add="left-sidebar-open" if visible else "",
            remove="" if visible else "left-sidebar-open",
        )

    speech_state: dict[str, Any] = {"task": None, "active": False, "latest_reply": ""}
    voice_state: dict[str, Any] = {"listening": False, "hands_free": False}
    speech_indicator: Any | None = None
    stop_speech_button: Any | None = None
    mic_button: Any | None = None
    hands_free_button: Any | None = None
    auto_send_voice_button: Any | None = None

    def record_voice_activity(status: str, title: str, detail: str = "") -> None:
        """Record microphone/model progress in the distinct voice activity style."""
        _record_activity(session_id, status, title, detail, kind="voice")

    async def dispatch_audio_event(event: Any) -> None:
        """Keep browser voice events in NiceGUI's client slot.

        Do not wrap this in ``asyncio.create_task``: detached tasks lose the
        client slot, so notifications and transcript/UI updates raise the
        runtime error shown in the terminal instead of reaching the chat.
        """
        await handle_audio_event(event)

    with ui.row().classes("assistant-header items-center no-wrap"):
        left_sidebar_toggle = ui.button(
            icon="menu_open" if client["left_sidebar_visible"] else "menu",
            on_click=toggle_left_sidebar,
        ).props("flat round dense").classes("sidebar-toggle")
        left_sidebar_toggle.tooltip("Show or hide settings sidebar")
        ui.icon("rocket_launch").classes("assistant-header-icon neural-logo")
        with ui.column().classes("gap-0"):
            ui.label("Neural").classes("assistant-title")
        ui.space()
        with ui.row().classes("header-behavior-controls items-center no-wrap"):
            approval_toggle = ui.toggle(
                APPROVAL_MODES,
                value=client["approval_mode"],
                on_change=lambda e: (client.__setitem__("approval_mode", e.value),
                                     _approval_modes.__setitem__(session_id, e.value)),
            ).props("inline dense").classes("approval-toggle header-approval-toggle")
            approval_toggle.tooltip("Choose when assistant actions need your review.")
            model_tier = ui.select(
                MODEL_OPTIONS,
                value=client["selected_tier"],
                on_change=lambda e: client.__setitem__("selected_tier", e.value),
            ).props("dense options-dense outlined aria-label='Model tier'").classes("header-tier")
            model_tier.tooltip("Choose which model tier handles new requests.")
            read_aloud_toggle = ui.button(
                icon="volume_up" if client["read_replies_aloud"] else "volume_off",
                on_click=toggle_read_replies_aloud,
            ).props("flat round dense aria-label='Read replies aloud'").classes(
                "voice-output-toggle" + (" is-active" if client["read_replies_aloud"] else "")
            )
            read_aloud_toggle.tooltip("Read replies aloud")
            ui.button(icon="play_arrow", on_click=lambda: play_latest_reply()).props(
                "flat round dense aria-label='Read latest reply aloud'"
            ).classes("voice-play-button").tooltip("Read the latest assistant reply")
            stop_speech_button = ui.button(icon="stop", on_click=lambda: stop_reply_speech()).props(
                "flat round dense aria-label='Stop reading aloud'"
            ).classes("voice-stop-button")
            stop_speech_button.disable()
            stop_speech_button.tooltip("Stop reading aloud")
            hands_free_button = ui.button(icon="hearing_disabled", on_click=None).props(
                "flat round dense aria-label='Hands-free mode: off'"
            ).classes("hands-free-button").on(
                "click",
                dispatch_audio_event,
                js_handler=VOICE_HANDSFREE_JS,
            )
            hands_free_button.tooltip("Enable hands-free listening for ‘Hey Neural’")
            speech_indicator = ui.label("SPEAKING").classes("speech-indicator")
            speech_indicator.set_visibility(False)
        with ui.row().classes("assistant-status service-unknown items-center no-wrap") as header_status:
            ui.icon("circle").classes("assistant-status-dot")
            header_status_label = ui.label("CHECKING")
        header_status.tooltip("Local image-generation service status. If the web UI itself stops, the browser disconnects instead of showing a stale status.")
    with ui.column().classes("assistant-chat") as chat_log:
        _render_history(chat_log, client["chat_history"])

    dialog_holder: dict[str, Any] = {}
    # Voice and typed prompts use independent UI channels. A voice model call
    # must not prevent a normal typed prompt from being submitted concurrently.
    sending = {"voice": False, "text": False}

    def refresh_speech_controls() -> None:
        active = bool(speech_state["active"])
        if speech_indicator is not None:
            speech_indicator.set_visibility(active)
        if stop_speech_button is not None:
            if active:
                stop_speech_button.enable()
            else:
                stop_speech_button.disable()

    def stop_reply_speech() -> None:
        stop_speaking()
        speech_state["active"] = False
        refresh_speech_controls()

    def speak_reply(reply: str) -> None:
        clean_reply = speech_text(reply)
        if not clean_reply:
            return
        stop_reply_speech()
        speech_state["latest_reply"] = reply
        speech_state["active"] = True
        speech_state["task"] = asyncio.create_task(run.io_bound(speak_text, clean_reply))
        refresh_speech_controls()

    def play_latest_reply() -> None:
        latest = speech_state["latest_reply"]
        if not latest:
            latest = next(
                (
                    str(entry.get("content", ""))
                    for entry in reversed(client["chat_history"])
                    if entry.get("role") == "assistant" and not str(entry.get("content", "")).startswith("[error]")
                ),
                "",
            )
        if latest:
            speak_reply(latest)
        else:
            ui.notify("There is no Neural reply to read yet.", type="warning")

    def refresh_speech_playback() -> None:
        task = speech_state.get("task")
        if task is None or not task.done():
            return
        try:
            task.result()
        except Exception as exc:
            logger.warning("Voice playback task failed: %s", exc)
            ui.notify("Neural could not play that reply aloud.", type="warning")
        finally:
            speech_state["task"] = None
            speech_state["active"] = False
            refresh_speech_controls()

    def toggle_auto_send_voice() -> None:
        enabled = not bool(client["auto_send_voice_commands"])
        client["auto_send_voice_commands"] = enabled
        if auto_send_voice_button is not None:
            auto_send_voice_button.set_icon("send_to_mobile" if enabled else "send_to_mobile_off")
            auto_send_voice_button.classes(add="is-active" if enabled else "", remove="" if enabled else "is-active")
            auto_send_voice_button.tooltip(
                "Auto-send voice prompts" if enabled else "Voice transcript only"
            )

    async def handle_audio_event(event: Any) -> None:
        payload = event.args[0] if isinstance(event.args, list) and event.args else event.args
        if not isinstance(payload, dict):
            return
        phase = str(payload.get("phase", ""))
        transcript = str(payload.get("text", "")).strip()
        if phase == "listening":
            if voice_state["listening"]:
                # Browser recognition can emit a second listening event when
                # it falls back to the local recorder after a network error.
                return
            voice_state["listening"] = True
            if mic_button is not None:
                mic_button.set_icon("graphic_eq")
            record_voice_activity("active", "Voice input started", "Listening for a microphone prompt.")
            return
        if phase == "partial":
            if transcript:
                message_input.value = transcript
                message_input.update()
            return
        if phase in {"stopped", "cancelled"}:
            voice_state["listening"] = False
            if mic_button is not None:
                mic_button.set_icon("mic")
            if phase == "cancelled":
                record_voice_activity(
                    "error",
                    "Voice input cancelled",
                    "The microphone recording was stopped before a prompt was submitted.",
                )
            else:
                record_voice_activity("error", "Voice input stopped", "No speech was detected.")
            return
        if phase == "hands_free_started":
            already_listening = bool(voice_state["hands_free"])
            voice_state["hands_free"] = True
            client["hands_free_enabled"] = True
            # Hands-free is an explicit conversational opt-in, so reply audio
            # is enabled for this session. The speaker control can still mute
            # it again without disabling wake-word listening.
            client["read_replies_aloud"] = True
            read_aloud_toggle.set_icon("volume_up")
            read_aloud_toggle.classes(add="is-active", remove="")
            if hands_free_button is not None:
                hands_free_button.set_icon("hearing")
                hands_free_button.classes(add="hands-free-active", remove="")
            if not already_listening:
                record_voice_activity(
                    "active",
                    "Hands-free listening started",
                    "Waiting for the wake phrase ‘Hey Neural’.",
                )
                ui.notify("Hands-free mode is listening for ‘Hey Neural’.", type="positive")
            return
        if phase == "hands_free_stopped":
            voice_state["hands_free"] = False
            client["hands_free_enabled"] = False
            if hands_free_button is not None:
                hands_free_button.set_icon("hearing_disabled")
                hands_free_button.classes(add="", remove="hands-free-active")
            record_voice_activity("done", "Hands-free listening stopped", "Wake-word listening is off.")
            return
        if phase == "wake_armed":
            record_voice_activity(
                "active",
                "Voice wake phrase detected",
                "Listening for the command that follows ‘Hey Neural’.",
            )
            ui.notify("Neural is listening for your command.", type="positive")
            return
        if phase == "wake":
            if not transcript:
                return
            await send_message(transcript, voice_request=True)
            return
        if phase == "error":
            voice_state["listening"] = False
            if mic_button is not None:
                mic_button.set_icon("mic")
            error = str(payload.get("error") or "Voice input is unavailable.")
            if voice_state["hands_free"]:
                voice_state["hands_free"] = False
                client["hands_free_enabled"] = False
                if hands_free_button is not None:
                    hands_free_button.set_icon("hearing_disabled")
                    hands_free_button.classes(add="", remove="hands-free-active")
                if error == "network":
                    error = (
                        "Hands-free listening cannot reach your browser's speech-recognition service. "
                        "Use the microphone button for local Whisper transcription instead."
                    )
            record_voice_activity("error", "Voice input failed", error)
            ui.notify(error, type="warning")
            return
        if phase == "audio":
            encoded_audio = str(payload.get("payload", ""))
            record_voice_activity(
                "active",
                "Voice transcription started",
                "Transcribing the recording locally with Whisper.",
            )
            try:
                audio_bytes = base64.b64decode(encoded_audio)
            except Exception:
                record_voice_activity("error", "Voice transcription failed", "The recorded audio could not be decoded.")
                ui.notify("The recorded audio could not be decoded.", type="negative")
                return
            transcript = await run.io_bound(transcribe_audio, audio_bytes)
        if phase not in {"final", "audio"}:
            return
        voice_state["listening"] = False
        if mic_button is not None:
            mic_button.set_icon("mic")
        if transcript:
            message_input.value = transcript
            message_input.update()
            record_voice_activity("done", "Voice transcription completed", f"Transcript: {transcript[:180]}")
            if client["auto_send_voice_commands"]:
                await send_message(transcript, voice_request=True)
            else:
                record_voice_activity(
                    "done",
                    "Voice transcript ready",
                    "The transcript is in the message box. Enable auto-send or press Send to ask Neural.",
                )
        else:
            record_voice_activity("error", "Voice transcription failed", "No speech was detected in the recording.")
            ui.notify("I could not transcribe that recording.", type="warning")

    async def send_message(user_message: str | None = None, *, voice_request: bool = False) -> None:
        global _active_request_task, _active_session_id
        request_channel = "voice" if voice_request else "text"
        request_key = (session_id, request_channel)
        user_message = (user_message or message_input.value).strip()
        if not user_message:
            if voice_request:
                record_voice_activity("error", "Voice request failed", "The transcript was empty, so nothing was sent to Neural.")
            return
        if sending[request_channel]:
            if voice_request:
                record_voice_activity("error", "Voice request failed", "Neural is already handling another request.")
                ui.notify("Neural is already handling the previous voice request.", type="warning")
            else:
                ui.notify("The previous typed request is still running. Please wait before sending another message.", type="warning")
            return
        previous_task = _request_tasks.get(request_key)
        if previous_task is not None and not previous_task.done():
            if voice_request:
                record_voice_activity("error", "Voice request failed", "The previous request is still running.")
                ui.notify("Neural is already handling the previous voice request.", type="warning")
            else:
                ui.notify("The previous request is still running. Please wait before sending another message.", type="warning")
            return
        sending[request_channel] = True
        message_input.value = ""
        history = client["chat_history"]
        history.append({"role": "user", "content": user_message})
        _render_history(chat_log, history)
        if voice_request:
            record_voice_activity(
                "active",
                "Voice request started",
                "Sending the transcribed prompt to Neural; the voice model has an OpenRouter fallback chain.",
            )
        if not voice_request:
            _clear_activity(session_id, preserve_voice=True)
            _record_activity(session_id, "active", "Request started", "Sending your message to Neural.")
            _record_activity(session_id, "active", "Neural is thinking", "Selecting a model and deciding whether tools are needed.")
        try:
            _active_session_id = session_id
            requested_tier, requested_chain_index = (
                _voice_selection_args() if voice_request else _selection_args(client["selected_tier"])
            )
            task = asyncio.create_task(
                run.io_bound(
                    orchestrator.handle_message,
                    session_id,
                    user_message,
                    force_tier=requested_tier,
                    force_chain_start_index=requested_chain_index,
                )
            )
            _active_request_task = task
            _request_tasks[request_key] = task
            task.add_done_callback(lambda completed_task: _clear_finished_request(completed_task, request_key))
            try:
                reply = await asyncio.wait_for(asyncio.shield(task), timeout=REQUEST_UI_TIMEOUT_SECONDS)
            except TimeoutError:
                _record_activity(
                    session_id,
                    "waiting",
                    "Voice request continues" if voice_request else "Long-running request continues",
                    "The voice worker is still running; its final reply will appear here automatically."
                    if voice_request
                    else "The worker is still running in the background. Its final reply and any generated image will appear here automatically.",
                    kind="voice" if voice_request else "assistant",
                )
                pending_entry = {
                    "role": "assistant",
                    "content": "⏳ This request is still running. The completed reply and any generated image will replace this message automatically.",
                    "pending": True,
                    "model_used": None,
                    "images": [],
                }
                history.append(pending_entry)
                client["chat_history"] = history
                _render_history(chat_log, history)
                await ui.run_javascript(
                    "const log = document.querySelector('.assistant-chat'); "
                    "if (log) log.scrollTop = log.scrollHeight;"
                )

                # The worker may finish several minutes later.  Only capture
                # its result in the done callback; the client-scoped timer
                # below performs all NiceGUI updates in the page context.
                task.add_done_callback(
                    lambda completed_task: _store_background_result(
                        session_id,
                        completed_task,
                        voice_request=voice_request,
                    )
                )
                sending[request_channel] = False
                return
            if (
                not _last_confirmation_results.get(session_id, True)
                and reply.startswith("Waiting for your approval on:")
            ):
                reply = "Command denied by user."
        except Exception as exc:
            traceback.print_exc()
            if voice_request:
                record_voice_activity("error", "Voice request failed", str(exc))
            else:
                _record_activity(session_id, "error", "Request failed", str(exc))
            reply = f"[error] {exc}"
        model_used = orchestrator.get_last_model_used(session_id)
        voice_failed = reply.startswith("[error]") or "task may be incomplete" in reply.casefold()
        if voice_request:
            record_voice_activity(
                "error" if voice_failed else "done",
                "Voice request failed" if voice_failed else "Voice request completed",
                str(reply)[:240] if voice_failed else f"Completed via {model_used or 'the configured voice model chain'}.",
            )
        elif not reply.startswith("[error]"):
            _record_activity(
                session_id,
                "done",
                "Neural reply received",
                f"Completed via {model_used}." if model_used else "Completed without a model response label.",
            )
        history.append({
            "role": "assistant",
            "content": reply,
            "model_used": model_used,
            "images": orchestrator.get_last_images(session_id),
        })
        speech_state["latest_reply"] = reply
        _render_history(chat_log, history)
        # A reminder can be created by the assistant during this turn. Refresh
        # immediately so it is visible without waiting for the periodic poll.
        refresh_productivity_panel()
        if client["read_replies_aloud"] and not reply.startswith("[error]"):
            speak_reply(reply)
        if voice_request:
            ui.notify(
                "Voice request completed." if not voice_failed else "Voice request failed.",
                type="negative" if voice_failed else "positive",
            )
        await ui.run_javascript(
            "const log = document.querySelector('.assistant-chat'); "
            "if (log) log.scrollTop = log.scrollHeight;"
        )
        sending[request_channel] = False

    assistant_input_bar = ui.row().classes(
        "assistant-input" + (" left-sidebar-open" if client["left_sidebar_visible"] else "")
    )
    with assistant_input_bar:
        with ui.column().classes("assistant-compose-inner"):
            with ui.row().classes("assistant-compose-row items-center no-wrap"):
                ui.icon("chat_bubble_outline").classes("assistant-compose-icon")
                message_input = ui.input(placeholder="Message Neural").props("outlined") \
                    .classes("assistant-input-field flex-grow").style("background-color: #292a2e; color: #f8fafc;") \
                    .on("keydown.enter", send_message)
                mic_button = ui.button(icon="mic", on_click=None).props("flat round aria-label='Start voice input'").classes("voice-input-button").on(
                    "click", handle_audio_event, js_handler=VOICE_CAPTURE_JS
                )
                mic_button.tooltip("Speak a message; it stops automatically after you finish, or click again to stop")
                auto_send_voice_button = ui.button(
                    icon="send_to_mobile" if client["auto_send_voice_commands"] else "send_to_mobile_off",
                    on_click=lambda: toggle_auto_send_voice(),
                ).props("flat round dense aria-label='Auto-send voice commands'").classes(
                    "voice-auto-send-toggle" + (" is-active" if client["auto_send_voice_commands"] else "")
                )
                auto_send_voice_button.tooltip("Auto-send voice prompts" if client["auto_send_voice_commands"] else "Voice transcript only")
                ui.button("Send", on_click=send_message, icon="send", color="primary").classes("assistant-send")
            ui.label("Enter to send · Tool approvals appear here when needed").classes("compose-helper")

    async def refresh_live_ui() -> None:
        _show_confirmation(session_id, dialog_holder)
        refresh_knowledge_watch_notifications()
        render_activity()
        background_result = _take_background_result(session_id)
        if background_result is None:
            return

        history = client["chat_history"]
        pending_entry = next(
            (entry for entry in reversed(history) if entry.get("role") == "assistant" and entry.get("pending")),
            None,
        )
        completed_reply = str(background_result["reply"])
        completed_model = background_result.get("model_used")
        if pending_entry is None:
            # The page may have been refreshed while the worker was running;
            # never lose a completed answer or its generated image in that case.
            history.append(
                {
                    "role": "assistant",
                    "content": completed_reply,
                    "pending": False,
                    "model_used": completed_model,
                    "images": list(background_result.get("images", [])),
                }
            )
        else:
            pending_entry.update(
                {
                    "content": completed_reply,
                    "pending": False,
                    "model_used": completed_model,
                    "images": list(background_result.get("images", [])),
                }
            )
        client["chat_history"] = history
        speech_state["latest_reply"] = completed_reply
        voice_request = bool(background_result.get("voice_request"))
        if background_result.get("error"):
            if voice_request:
                record_voice_activity("error", "Voice request failed", str(background_result["error"])[:240])
            else:
                _record_activity(session_id, "error", "Long-running request failed", str(background_result["error"]))
        else:
            voice_failed = completed_reply.startswith("[error]") or "task may be incomplete" in completed_reply.casefold()
            if voice_request:
                record_voice_activity(
                    "error" if voice_failed else "done",
                    "Voice request failed" if voice_failed else "Voice request completed",
                    completed_reply[:240]
                    if voice_failed
                    else f"Completed via {completed_model or 'the configured voice model chain'}.",
                )
            else:
                _record_activity(
                    session_id,
                    "done",
                    "Long-running reply received",
                    f"Completed via {completed_model}." if completed_model else "Completed successfully.",
                )
                if background_result.get("images"):
                    _record_activity(
                        session_id,
                        "done",
                        "Generated image ready",
                        f"{len(background_result['images'])} image file(s) are available in the chat.",
                    )
        _render_history(chat_log, history)
        # The completed background turn may have created a reminder; update
        # the sidebar as soon as its final result arrives.
        refresh_productivity_panel()
        if client["read_replies_aloud"] and not completed_reply.startswith("[error]"):
            speak_reply(completed_reply)
        if voice_request:
            ui.notify(
                "Voice request completed." if not background_result.get("error") else "Voice request failed.",
                type="positive" if not background_result.get("error") else "negative",
            )
        await ui.run_javascript(
            "const log = document.querySelector('.assistant-chat'); "
            "if (log) log.scrollTop = log.scrollHeight;"
        )

    ui.timer(0.3, refresh_live_ui)
    ui.timer(0.2, refresh_speech_playback)
    ui.timer(2.0, refresh_image_setup)
    ui.timer(10.0, refresh_productivity_panel)


if __name__ in {"__main__", "__mp_main__"}:
    try:
        ui.run(
            title=PAGE_TITLE,
            port=_available_port(),
            storage_secret="personal-ai-assistant",
            reload=False,
        )
    except KeyboardInterrupt:
        # Ctrl+C is an intentional, clean stop—not an application failure.
        logger.info("Neural stopped by user.")
    finally:
        _close_browser_session()
