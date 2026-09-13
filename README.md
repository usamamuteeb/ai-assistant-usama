# Personal AI Assistant — Foundation

This is a real, running implementation of the architecture in `personal-ai-assistant-architecture.md`
(interface → orchestrator → model layer → tool/plugin layer → memory → triggers).
It's deliberately small and readable so you can keep extending it with Codex / Copilot / your own hands.

It is NOT a wrapper around Goose or n8n — it's a minimal orchestrator you own outright, so you can bend
it into whatever shape your use cases need. You can still run Goose/n8n alongside it later if you want.

## What actually works right now

- **Model router** that calls a **free local model via Ollama** or a **premium model via the Anthropic API**,
  with a simple heuristic to pick one automatically (or you can force it).
- **Tool-use loop** (ReAct-style): the model can call tools, see results, call more tools, then answer —
  using Anthropic's native tool-use format.
- **Three real tools**: filesystem (read/write/list across the configured workspace and user folders),
  shell command execution (with a safety confirmation switch), and a memory-search tool.
- **Clipboard tools**: read and write the system clipboard through the local clipboard plugin.
- **Memory**: SQLite for structured state + conversation log, Chroma (local vector DB) for semantic
  long-term memory. Both are real, working stores, not stubs.
- **Plugin system**: drop a folder in `plugins/`, export a `register()` function, restart — it's now a tool
  the model can call. One working example plugin included.
- **Scheduler**: cron-style scheduled tasks defined in `config.yaml`, run via APScheduler, that fire the
  orchestrator headlessly (this is your "automation" layer / trigger layer).
- **CLI chat loop** as the interface layer — swap this for a Telegram bot or GUI later without touching
  anything else.

## Setup

```bash
cd personal-ai-assistant
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env: add your ANTHROPIC_API_KEY (only needed for premium-tier calls)

# IMPORTANT: browser automation requires the browser binary to be installed once after pip install.
# `pip install -r requirements.txt` installs the Python package only; it does NOT download Chromium.
playwright install chromium

# optional, for the free local tier:
# install Ollama from https://ollama.com, then:
ollama pull llama3.1
```

## OpenRouter fallback setup

The free API chain keeps OpenRouter as its final fallback, after Gemini and all
configured Groq models. Add `OPENROUTER_API_KEY` to `.env`; OpenRouter offers
free keys without a card at [openrouter.ai/keys](https://openrouter.ai/keys).
The configured `qwen/qwen3-coder:free` ID should be checked against the live
[OpenRouter model catalog](https://openrouter.ai/models) before relying on it,
because free models can rotate or become temporarily unavailable without
notice. This provider is intentionally last rather than competing for normal
traffic: its free models have independent provider-side rate limits and other
reliability issues beyond this account's quota.

The current fallback snapshot includes tool-capable free models from Nex-AGI,
Cohere, Google, InclusionAI, Dots, Liquid, Thinking Machines, and Poolside.
Because this list is maintained by OpenRouter and can change, treat any stale
entry as disposable: the router falls through to the next configured model.

NVIDIA NIM, Cerebras, and Mistral were evaluated and intentionally excluded:
NVIDIA NIM had only a one-time credit signal; Cerebras had conflicting trial
reports; and Mistral had conflicting trial reports plus phone verification.
They should not be added to the fallback chain without revisiting those
constraints.

Run the chat loop:

```bash
python -m src.cli
```

## Web UI

Run the NiceGUI app from the project folder:

```bash
python -m src.web_ui
```

The app calls `ui.run()` itself, so no Streamlit wrapper is needed. It opens a
browser tab automatically by default and runs at [http://localhost:8080](http://localhost:8080).
If port `8080` is already occupied, the app automatically selects the next available
port and prints the actual URL in the terminal.
For development auto-reload, use `ui.run(reload=True)`; otherwise keep
`reload=False`.

Stop it with:

```powershell
Ctrl+C
```

## Voice input/output

The web UI includes local microphone transcription and optional spoken replies.
Voice input uses the offline `faster-whisper` base model, which downloads a model
file of a few hundred MB on first use; after that download completes, transcription
runs fully offline. Change the model in `src/voice.py` to `small` or `medium` if
you prefer higher accuracy over speed. Voice input requires microphone permission
in the browser. Enable **Read replies aloud** in the sidebar to use the system's
default audio output for assistant replies.

## Image generation

The `generate_image` plugin saves images under `workspace/generated_images/`, which the
web chat displays inline. It uses **local ComfyUI only**: no Gemini image model, cloud
image API, or image credits are used.

### Local ComfyUI setup (no cloud image credits)

The first run of `python -m src.web_ui` automatically starts a project-local installer. It
downloads official ComfyUI, a CPU-only Python runtime, and the default Stable Diffusion 1.5
checkpoint under `data/comfyui`. This is a multi-GB, one-time download. Its progress is
written to `data/comfyui/setup.log`; `data/comfyui/setup_status.json` records whether setup
is running, ready, or failed. Once ready, later web-UI launches start ComfyUI in the
background and connect to it at `http://127.0.0.1:8188`.

The right-side **Image generation** card is the authoritative live status: it shows
**Ready**, **Processing**, **Failed**, **Starting**, or **Not installed**, the current setup
phase, timestamp, and the latest installer-log lines. Use **Retry image setup** there after
a network failure; it starts the background installer and immediately returns control to the
chat instead of making an assistant request wait.

On this computer's Intel integrated graphics, generation runs in CPU mode. It is fully
local but can take several minutes per 512×512 image. Leave the web UI open while a request
is running. You can later install another compatible checkpoint in
`data/comfyui/ComfyUI/models/checkpoints/` and update
`image_generation.local_comfyui.checkpoint` in `config.yaml`.

The local ComfyUI backend uses only stock ComfyUI nodes (checkpoint loader, text encoders,
KSampler, VAE decode, and Save Image); no custom workflow or cloud account is required.

## Personal knowledge base

The knowledge base indexes `.pdf`, `.txt`, `.md`, `.docx`, `.xlsx`, and `.pptx`
files from the configured workspace folder. PDFs are chunked per page so the
citation search can report `filename, page N`; spreadsheet sheets and
presentation slides are retained as metadata. Existing chunks created before
page-aware ingestion do not have page numbers, so re-index existing documents
if page citations and content-hash duplicate detection are needed.

Available tools include `ingest_knowledge_base`, `search_knowledge_base`,
`search_knowledge_base_with_citations`, `list_knowledge_documents`,
`document_summary`, `ask_document`, `tag_document`, and
`remove_knowledge_document`. Searches support filename substring, category,
and ingestion-date filters. Removing a document only unindexes it by default;
deleting the source file requires the normal approval prompt.

When the NiceGUI web UI is running, a watchdog observes the configured
knowledge-base folder and automatically re-indexes changed documents after a
short debounce, with a toast showing the result. This watcher is deliberately
web-UI-only: the CLI and scheduler do not run a background observer and still
require an explicit `ingest_knowledge_base` call. The sidebar upload accepts all
six supported document types; direct file copies into the folder are picked up
by the web watcher too.

## Screen reading (OCR) setup

Screen reading uses Tesseract OCR to capture and read visible text. Download and
install Tesseract OCR for Windows from the UB-Mannheim build at
https://github.com/UB-Mannheim/tesseract/wiki. If Tesseract is not on PATH after
installation, the screen-reader plugin automatically checks the standard Windows
locations (`C:\Program Files\Tesseract-OCR\tesseract.exe` and the x86 equivalent).
For a custom install location, set a `TESSERACT_CMD` environment variable to the
full `tesseract.exe` path before starting the assistant.

Captures and reads text from whatever is currently visible on the ENTIRE screen,
not a specific app — the extracted text is sent to whichever model answers this request.

## Windows desktop control setup

On Windows, the `windows_control` plugin can inspect visible windows and read their
controls. It can also open applications, wait for them to become ready, focus or
move/resize windows, click controls by visible text or automation ID, close windows,
and type into a verified target on your actual desktop. `read_active_window` reports
the current foreground window and its visible controls. Those action tools are
powerful: a click or keystroke can affect whatever is open on the machine.

`config.yaml` sets `windows_control.force_manual_confirmation: true` by default.
It keeps two high-risk actions behind a real approval prompt even when the global
UI approval mode is `auto`: `close_window`, because it can discard unsaved work,
and `send_keystrokes_fallback`, because it types into an unverified currently
focused window. `open_application` and `click_window_control` follow the global
approval mode normally. Keep the override enabled unless you have a carefully
controlled environment.

`type_into_window` first matches and focuses the requested window, then sends text
through that verified pywinauto target. In contrast, `send_keystrokes_fallback` uses
PyAutoGUI as a blunt fallback and targets whichever window is currently focused;
it is intentionally the higher-risk option. PyAutoGUI's failsafe remains enabled:
drag the mouse pointer to any screen corner to abort an in-progress fallback
action immediately.

## Developer tools setup

The `dev_tools` plugin can read, write, and delete files throughout the configured
`dev_tools.allowed_root` (by default `C:\`, including Documents, Downloads, Pictures,
and folders outside this project) and run arbitrary Python, JavaScript, or shell snippets.
It rejects file and folder paths on other drives. The destructive actions (`write_file_anywhere`,
`delete_file_anywhere`, and `run_code`) always use the browser's real manual
approval dialog by default, even if the global approval mode is set to `auto`.

`config.yaml` also contains `dev_tools.protected_path_prefixes`. It blocks writes
and deletes below a small set of high-risk Windows paths before an approval prompt
is shown. Both this blocklist and the confirmation override are adjustable safety
rails, not a substitute for reviewing every requested action carefully.

`run_code` is intentionally powerful: an approved arbitrary code snippet could access
other drives on its own, so a path-root check cannot be a complete OS-level sandbox for
that one tool. Keep its manual-confirmation override enabled; disable the tool rather than
approving untrusted code if you need a strict machine-wide C:-only boundary.

Force a tier for a single message:

```bash
python -m src.cli --tier local
python -m src.cli --tier premium
```

Run the scheduler (executes tasks defined in `config.yaml` under `scheduled_tasks`):

```bash
python -m src.triggers.scheduler
```

## Tasks and one-time reminders

The `task_manager` plugin provides durable personal tasks and reminders in the same
SQLite database used by the assistant. You can ask the assistant to create, list,
update, complete, or delete tasks, and to set, list, cancel, or snooze one-time
reminders. Task deletion requires confirmation.

Examples:

- `Create a high-priority task called Prepare the project demo due 2026-09-20T09:00.`
- `Show my overdue tasks.`
- `Remind me in 30 minutes to check the deployment.`
- `Snooze reminder 4 for 20 minutes.`

Reminder times accept ISO date/time values, `tomorrow at 9:00 AM`, `today at 17:30`,
and relative values such as `in 10 seconds`, `in 10 sec`, `after 5 minutes`, or
`in 1 hour and 30 minutes`. Reminders are stored durably and are
delivered by the scheduler's reminder dispatcher. Start `python -m src.triggers.scheduler`
to receive terminal/log delivery while the scheduler is running; the NiceGUI app also
shows open tasks, upcoming reminders, and browser notifications while it is open.
The polling interval is configurable with `task_manager.reminder_poll_seconds`.

## Project layout

```
src/
  config.py            # loads .env + config.yaml into one Settings object
  model_router.py       # AnthropicBackend, OllamaBackend, ModelRouter (tier decision)
  orchestrator.py        # the "brain": message history, tool loop, memory read/write
  cli.py                 # interface layer — chat REPL
  tools/
    base.py             # Tool abstract base class + schema helper
    filesystem_tool.py    # sandboxed read/write/list
    shell_tool.py         # sandboxed shell execution
    memory_tool.py         # lets the model search its own long-term memory
    registry.py            # discovers built-in tools + plugins, dispatches calls
  memory/
    store.py             # SQLite: conversation log + key/value state + task queue
    vector_store.py        # Chroma wrapper: add_memory / search
  triggers/
    scheduler.py          # APScheduler: runs orchestrator on a cron schedule
plugins/
  example_web_search/     # example of a third-party-style plugin exposing a new tool
config.yaml               # models, workspace path, safety switches, scheduled tasks
.env.example
requirements.txt
data/                      # sqlite.db + chroma/ persist dir (gitignored)
```

## How to extend this (with Codex/Copilot or by hand)

1. **New tool** → copy `src/tools/filesystem_tool.py` as a template, implement `name`, `description`,
   `input_schema`, `run()`. Add it to `TOOLS` list in `src/tools/registry.py`.
2. **New plugin (external/optional capability)** → copy `plugins/example_web_search/`, implement
   `register()` returning a list of `Tool` instances. It's auto-loaded, no core code touched.
3. **New automation** → add an entry under `scheduled_tasks` in `config.yaml` with a cron expression and
   a prompt; the scheduler will run the orchestrator with that prompt on schedule.
4. **New interface** (Telegram bot, GUI, etc.) → build it against `Orchestrator.handle_message()` in
   `src/orchestrator.py` — that's the one method every interface should call. Don't duplicate the tool
   loop elsewhere.
5. **Swap/add a model backend** → implement the same two methods as `AnthropicBackend`/`OllamaBackend`
   (`chat()` and `supports_tools`) in `src/model_router.py`, register it in `ModelRouter`.

## Notes on the model routing heuristic

`ModelRouter.pick_tier()` currently uses a simple heuristic (message length + keyword signals like "plan",
"analyze", "write code", "refactor" push it to premium; short/simple asks stay local). This is intentionally
crude — replace it with something smarter (a cheap classifier call, or just your own rules) once you know
your real usage patterns. Track this in `data/sqlite.db` (`model_calls` table) — every call is logged with
which tier was used, so you can see if the heuristic is routing sensibly before you optimize it.

## Safety switches (check `config.yaml`)

- `shell_tool.require_confirmation`: if true (default), shell commands print what they'd run and require
  the CLI user to confirm before execution. Turn this off only for the scheduler running fully unattended
  tasks you already trust.
- `filesystem_tool.workspace_root`: relative filesystem paths resolve under this workspace. Absolute
  paths are also allowed only under the configured `filesystem_tool.allowed_roots` folders (Documents,
  Downloads, Pictures, Videos, Desktop, and Music by default). Reads/lists and new writes are allowed;
  replacing or deleting a file requires confirmation.

## Google Workspace setup

This project can auto-discover Gmail and Calendar tools from `plugins/google_workspace/plugin.py`. They are plugin tools, so the model decides when to call them, just like any other tool.

### 1) Create a Google Cloud project

1. Go to https://console.cloud.google.com/
2. Create a new project (or reuse an existing one)
3. Give it a name such as `personal-ai-assistant`
4. Keep the project selected for the next steps

### 2) Enable the required APIs

In the Google Cloud Console:

1. Open `APIs & Services` → `Library`
2. Enable `Gmail API`
3. Enable `Google Calendar API`

### 3) Configure the OAuth consent screen

1. Open `APIs & Services` → `OAuth consent screen`
2. Choose `External` user type for a personal project
3. Fill in the required app information
4. Add your Google account as a test user while you are still setting it up

### 4) Create the OAuth client ID

1. Open `APIs & Services` → `Credentials`
2. Click `Create credentials` → `OAuth client ID`
3. Application type: `Desktop app`
4. Give it a name like `personal-ai-assistant-desktop`
5. Click `Create`
6. Download the JSON file and save it as `credentials.json` in the project root

The file should live here:

```text
<project-root>/credentials.json
```

### 5) Set the encryption key for cached tokens

Add this to your `.env` file:

```bash
GOOGLE_TOKEN_KEY=<value from Fernet.generate_key()>
```

Generate it with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

After the first successful sign-in, the app writes an encrypted Google token cache to:

```text
data/google_token.json
```

### 6) First-run OAuth flow

When the model calls a Gmail/Calendar tool for the first time, it will attempt to use the cached token. If no valid cached token exists, it will launch the Google OAuth flow in your browser. Sign in and approve the requested scopes.

### Example tool-only prompts

You do not need to edit `cli.py` for this. The model can decide to use these tools naturally from chat:

- `What is on my calendar tomorrow?`
- `Search my inbox for invoices from this month.`
- `Create a calendar event for tomorrow at 3pm for a project review.`
- `Send an email to alice@example.com with the subject 'Project update' and a short summary body.`

### Google Workspace tools

In addition to search, send, list-events, and create-event, the plugin provides:

- Gmail: `gmail_get_message`, `gmail_reply`, `gmail_forward`,
  `gmail_archive_message`, and `gmail_download_attachment`.
- Calendar: `calendar_find_free_time`, `calendar_update_event`,
  `calendar_delete_event`, and `calendar_create_recurring_event`.
- Local productivity: `email_to_task` and `get_daily_agenda`.

Replies preserve the original Gmail thread. Forwarding quotes the original
message and accepts an optional note, but original attachments are intentionally
not automatically re-attached. Attachment downloads never overwrite an
existing file; they use a numbered filename instead. Archive/update/delete and
all message/event creation actions use the normal global confirmation mode.
Because archiving requires the Gmail modify scope, an existing OAuth token may
need to be authorized again after adding `gmail.modify` to `config.yaml`.

## Browser automation setup

After `pip install -r requirements.txt`, you must also run this once in the project environment:

```bash
playwright install chromium
```

This is required because the `playwright` Python package installs the automation API, but the actual Chromium binary is downloaded separately. If you skip this step, the browser tools will fail at runtime when they try to launch the browser.

The browser instance stays open for the lifetime of the running process (CLI session, NiceGUI app, or scheduler run) instead of closing after each tool call. That keeps page state available between browser actions and avoids the overhead of relaunching a browser for every single request.

Browser actions are visible on the desktop by default (`browser.headless: false` in `config.yaml`), so you can inspect a page while the assistant works. Set it to `true` only for intentionally unattended automation. Browser screenshots are also displayed directly in the local chat UI; no external upload is involved.

## Optional system automation

This project can also opt into an optional `plugins/system_monitor/plugin.py` toolset for local system health and process inspection. It exposes `system_stats`, `list_processes`, and `kill_process` tools. The destructive `kill_process` tool is gated behind the same explicit confirm callback pattern as the shell tool, so it will not terminate anything without user approval.

### WhatsApp Web (local, personal use)

The optional `plugins/whatsapp/plugin.py` uses the existing visible Playwright browser session and WhatsApp Web rather than the WhatsApp Cloud API. It provides `whatsapp_open`, `whatsapp_list_unread_chats`, `whatsapp_read_chat`, and `whatsapp_send_message`. On first use, scan the QR code in the browser with the phone's WhatsApp app. Reading is not confirmation-gated; sending always asks for approval with the resolved destination and exact message text. Denying the prompt leaves the message unsent so it can be edited or cancelled. This is local browser automation, so the assistant process and logged-in WhatsApp Web session must remain available and WhatsApp Web changes may require maintenance.

## File and document automation

The `file_automation` plugin can rename, move, copy, and organize files anywhere
on the C: drive, including Desktop, Downloads, Documents, Pictures, Videos, and
Music. It can also create Markdown, PDF, and Word reports, extract PDF tables,
and convert documents. Paths must be absolute. The protected prefixes from
`dev_tools.protected_path_prefixes` are always refused.

Install [Pandoc](https://pandoc.org/installing.html) separately for
`convert_document`; it is free and must be available on PATH. Every overwrite
performed by this plugin first creates a timestamped copy under
`workspace/file_backups/`. Pass `dry_run: true` to a destructive file action to
validate it and preview the exact operation without changing the filesystem.
When `file_automation.force_manual_confirmation` is enabled, rename/move and
overwrite actions that can replace existing files require real manual approval.
