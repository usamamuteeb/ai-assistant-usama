"""NiceGUI web interface for the personal assistant."""

from __future__ import annotations

import asyncio
import socket
import sys
import threading
import traceback
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nicegui import app, run, ui

from src.config import load_settings
from src.orchestrator import Orchestrator

TIER_OPTIONS = ["auto", "local", "premium", "free_api"]
APPROVAL_MODES = ["manual", "auto"]
_pending_confirmations: dict[str, dict[str, Any]] = {}
_pending_lock = threading.Lock()
_session_confirm_fns: dict[str, Any] = {}
_approval_modes: dict[str, str] = {}
_last_confirmation_results: dict[str, bool] = {}
_active_session_id: str | None = None
_orchestrator_lock = asyncio.Lock()


def _confirm_for_session(session_id: str, command: str) -> bool:
    if _approval_modes.get(session_id, "auto") == "auto":
        return True
    event = threading.Event()
    pending = {"command": command, "event": event, "result": None, "shown": False}
    with _pending_lock:
        _pending_confirmations[session_id] = pending
    event.wait()
    with _pending_lock:
        result = _pending_confirmations.get(session_id, pending).get("result")
        _pending_confirmations.pop(session_id, None)
        _last_confirmation_results[session_id] = bool(result)
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


# Keep one long-lived orchestrator for its SQLite/vector/tool resources.
orchestrator = Orchestrator(load_settings(), confirm_fn=_dispatch_confirm)


def _force_tier(selection: str) -> str | None:
    return None if selection == "auto" else selection


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


def _render_history(chat_log: ui.column, history: list[dict[str, str]]) -> None:
    chat_log.clear()
    with chat_log:
        if not history:
            with ui.column().classes("assistant-empty"):
                ui.icon("auto_awesome").classes("assistant-empty-icon")
                ui.label("Your workspace assistant is ready").classes("assistant-empty-title")
                ui.label("Ask a question, plan a task, or request an action.").classes("assistant-empty-copy")
            return
        for entry in history:
            sent = entry["role"] == "user"
            message_class = "message-user" if sent else "message-assistant"
            with ui.chat_message(
                name="You" if sent else "Assistant",
                sent=sent,
            ).classes(f"assistant-message {message_class}"):
                ui.markdown(entry["content"]).classes("chat-markdown")


def _show_confirmation(session_id: str, dialog_holder: dict[str, Any]) -> None:
    with _pending_lock:
        pending = _pending_confirmations.get(session_id)
        if not pending or pending.get("shown") or pending.get("result") is not None:
            return
        pending["shown"] = True

    dialog = ui.dialog()
    dialog_holder["dialog"] = dialog
    with dialog, ui.card().classes("min-w-[28rem]"):
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
    client.setdefault("session_id", str(uuid.uuid4()))
    client.setdefault("chat_history", [])
    client.setdefault("approval_mode", "auto")
    client.setdefault("selected_tier", "auto")
    session_id = client["session_id"]
    _session_confirm_fns.setdefault(session_id, _make_confirm_fn(session_id))
    _approval_modes.setdefault(session_id, client["approval_mode"])

    ui.dark_mode().enable()
    ui.colors(primary="#d1202c", secondary="#7f1d1d")
    ui.page_title("Personal AI Assistant")
    ui.add_css("""
        :root { color-scheme: dark; --ink: #f8fafc; --muted: #a8afb9; --panel: #1a1b1e; --panel-2: #222326; --line: rgba(255,255,255,.08); --crimson: #d1202c; }
        body, .q-page, .q-page-container, .q-layout { background: radial-gradient(circle at 78% -18%, rgba(120, 18, 28, .23), transparent 30rem), #111214; color: var(--ink); }
        .q-drawer { background: linear-gradient(180deg, #1f2023 0%, #18191c 100%); border-right: 1px solid rgba(209,32,44,.30); box-shadow: 16px 0 40px rgba(0,0,0,.26); }
        .assistant-sidebar { height: 100%; }
        .assistant-brand { gap: .8rem; margin: .35rem 0 1.75rem; }
        .assistant-brand-mark { width: 42px; height: 42px; border-radius: 13px; display: flex; align-items: center; justify-content: center; color: #fff; background: linear-gradient(145deg, #ef3340, #8f101b); box-shadow: 0 10px 24px rgba(209,32,44,.25); }
        .assistant-brand-name { font-size: 1.05rem; font-weight: 700; letter-spacing: -.01em; }
        .assistant-brand-copy, .setting-copy, .sidebar-footer-copy, .assistant-subtitle, .compose-helper, .assistant-empty-copy { color: var(--muted); font-size: .78rem; line-height: 1.45; }
        .sidebar-section-label { margin: 1.5rem 0 .42rem; color: #e7e9ed; font-size: .78rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; }
        .setting-copy { margin-bottom: .8rem; }
        .approval-toggle { display: inline-flex; padding: .25rem; border: 1px solid var(--line); border-radius: 10px; background: #151619; }
        .approval-toggle .q-btn { min-height: 34px; border-radius: 7px; color: #cbd1d9; font-size: .74rem; font-weight: 700; letter-spacing: .04em; }
        .approval-toggle .q-btn.bg-primary, .q-btn.bg-primary { background: linear-gradient(135deg, #e1323e, #b31320) !important; color: #fff !important; box-shadow: 0 7px 16px rgba(209,32,44,.22); }
        .assistant-tier { margin-top: .2rem; }
        .assistant-tier .q-field__control { min-height: 50px; background: #28292d; border: 1px solid rgba(255,255,255,.06); border-radius: 10px; }
        .assistant-tier .q-field__native, .assistant-tier .q-field__label, .assistant-tier .q-field__marginal { color: #f3f4f6 !important; }
        .sidebar-footer { margin-top: auto; padding: 1rem; border: 1px solid rgba(209,32,44,.18); border-radius: 12px; background: rgba(209,32,44,.06); }
        .sidebar-footer-title { margin-bottom: .28rem; color: #fff; font-size: .8rem; font-weight: 700; }
        .assistant-header { width: min(980px, calc(100vw - 23rem)); margin: 1.6rem auto .45rem; padding: 0 .25rem; }
        .assistant-header-icon { color: #ff4d57; font-size: 1.55rem; }
        .assistant-title { color: #fff; font-size: 1.55rem; font-weight: 750; letter-spacing: -.035em; line-height: 1.15; }
        .assistant-status { padding: .36rem .62rem; border: 1px solid rgba(105, 223, 153, .24); border-radius: 999px; color: #9ce3ba; background: rgba(74, 222, 128, .08); font-size: .68rem; font-weight: 700; letter-spacing: .07em; }
        .assistant-status-dot { color: #5ee28b; font-size: .62rem; }
        .assistant-chat { width: min(980px, calc(100vw - 23rem)); height: calc(100vh - 13.4rem); margin: 0 auto; padding: 1.2rem .25rem 8rem; overflow-y: auto; gap: 1.15rem; }
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
        .assistant-input { position: fixed; bottom: 0; left: 20rem; right: 0; z-index: 10; padding: .85rem 1.5rem .9rem; background: rgba(23,24,27,.96); border-top: 1px solid rgba(209,32,44,.3); box-shadow: 0 -12px 30px rgba(0,0,0,.22); backdrop-filter: blur(18px); }
        .assistant-compose-inner { width: min(980px, 100%); margin: 0 auto; gap: .38rem; }
        .assistant-compose-row { width: 100%; gap: .7rem; }
        .assistant-compose-icon { margin-left: .25rem; color: #d1202c; }
        .assistant-input-field { background: #292a2e; border-radius: 11px; }
        .assistant-input-field .q-field__control { background: #292a2e !important; min-height: 50px; border: 1px solid rgba(255,255,255,.09); border-radius: 11px; transition: border-color .2s, box-shadow .2s; }
        .assistant-input-field.q-field--focused .q-field__control { border-color: #d1202c; box-shadow: 0 0 0 3px rgba(209,32,44,.13); }
        .assistant-input-field input, .assistant-input-field .q-field__native { color: #f8fafc !important; -webkit-text-fill-color: #f8fafc !important; caret-color: #fff !important; opacity: 1 !important; }
        .assistant-input-field input::placeholder { color: #9da4af !important; opacity: 1; }
        .assistant-send { min-height: 50px; min-width: 108px; padding: 0 1.1rem; border-radius: 10px; color: #fff !important; font-weight: 700; letter-spacing: .01em; }
        .compose-helper { padding-left: 2rem; }
        @media (max-width: 900px) {
            .assistant-header, .assistant-chat { width: calc(100vw - 2rem); }
            .assistant-header { margin-top: 1rem; }
            .assistant-status { display: none; }
            .assistant-input { left: 0; padding: .75rem 1rem; }
            .assistant-message .q-message-text, .message-assistant .q-message-text { max-width: 88% !important; }
        }
    """)

    with ui.left_drawer(value=True).props("behavior=desktop width=320").classes("p-5 assistant-sidebar"):
        with ui.row().classes("assistant-brand items-center no-wrap"):
            with ui.element("div").classes("assistant-brand-mark"):
                ui.icon("auto_awesome")
            with ui.column().classes("gap-0"):
                ui.label("Personal AI").classes("assistant-brand-name")
                ui.label("Workspace assistant").classes("assistant-brand-copy")
        ui.separator().style("background: rgba(255,255,255,.08);")
        ui.label("Approval mode").classes("sidebar-section-label")
        ui.label("Choose when assistant actions need your review.").classes("setting-copy")
        ui.toggle(
            APPROVAL_MODES,
            value=client["approval_mode"],
            on_change=lambda e: (client.__setitem__("approval_mode", e.value),
                                 _approval_modes.__setitem__(session_id, e.value)),
        ).props("inline").classes("approval-toggle")
        ui.label("Model routing").classes("sidebar-section-label")
        ui.label("Select the model tier for this conversation.").classes("setting-copy")
        ui.select(TIER_OPTIONS, value=client["selected_tier"], label="Tier",
                  on_change=lambda e: client.__setitem__("selected_tier", e.value)).classes("w-full assistant-tier")
        with ui.column().classes("sidebar-footer"):
            ui.label("Private by design").classes("sidebar-footer-title")
            ui.label("Your chat stays in this local assistant session.").classes("sidebar-footer-copy")

    with ui.row().classes("assistant-header items-center no-wrap"):
        ui.icon("smart_toy").classes("assistant-header-icon")
        with ui.column().classes("gap-0"):
            ui.label("Personal AI Assistant").classes("assistant-title")
            ui.label("A focused workspace for your tasks and tools").classes("assistant-subtitle")
        ui.space()
        with ui.row().classes("assistant-status items-center no-wrap"):
            ui.icon("circle").classes("assistant-status-dot")
            ui.label("READY")
    with ui.column().classes("assistant-chat") as chat_log:
        _render_history(chat_log, client["chat_history"])

    dialog_holder: dict[str, Any] = {}
    sending = {"active": False}

    async def send_message() -> None:
        user_message = message_input.value.strip()
        if not user_message or sending["active"]:
            return
        sending["active"] = True
        message_input.value = ""
        history = client["chat_history"]
        history.append({"role": "user", "content": user_message})
        _render_history(chat_log, history)
        try:
            global _active_session_id
            async with _orchestrator_lock:
                _active_session_id = session_id
                try:
                    reply = await run.io_bound(
                        orchestrator.handle_message,
                        session_id,
                        user_message,
                        force_tier=_force_tier(client["selected_tier"]),
                    )
                    if (
                        not _last_confirmation_results.get(session_id, True)
                        and reply.startswith("Waiting for your approval on:")
                    ):
                        reply = "Command denied by user."
                finally:
                    _active_session_id = None
        except Exception as exc:
            traceback.print_exc()
            reply = f"[error] {exc}"
        history.append({"role": "assistant", "content": reply})
        _render_history(chat_log, history)
        await ui.run_javascript(
            "const log = document.querySelector('.assistant-chat'); "
            "if (log) log.scrollTop = log.scrollHeight;"
        )
        sending["active"] = False

    with ui.row().classes("assistant-input"):
        with ui.column().classes("assistant-compose-inner"):
            with ui.row().classes("assistant-compose-row items-center no-wrap"):
                ui.icon("chat_bubble_outline").classes("assistant-compose-icon")
                message_input = ui.input(placeholder="Message the assistant").props("outlined") \
                    .classes("assistant-input-field flex-grow").style("background-color: #292a2e; color: #f8fafc;") \
                    .on("keydown.enter", send_message)
                ui.button("Send", on_click=send_message, icon="send", color="primary").classes("assistant-send")
            ui.label("Enter to send · Tool approvals appear here when needed").classes("compose-helper")

    ui.timer(0.3, lambda: _show_confirmation(session_id, dialog_holder))


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        title="Personal AI Assistant",
        port=_available_port(),
        storage_secret="personal-ai-assistant",
        reload=False,
    )
