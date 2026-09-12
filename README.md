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
- **Three real tools**: filesystem (read/write/list, sandboxed to a workspace folder), shell command
  execution (with a safety confirmation switch), and a memory-search tool.
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

The `generate_image` plugin uses Gemini's `gemini-3.1-flash-image` model to turn a
text prompt into an image saved under `workspace/generated_images/`. Image generation
depends on the model being enabled for your Google Cloud project and Gemini API key.
If the first request returns an access or quota error, check the model access for your
key at [Google AI Studio](https://aistudio.google.com/) before assuming the code is broken.

## Screen reading (OCR) setup

Screen reading uses Tesseract OCR to capture and read visible text. Download and
install Tesseract OCR for Windows from the UB-Mannheim build at
https://github.com/UB-Mannheim/tesseract/wiki. If Tesseract is not on PATH after
installation, set `pytesseract.pytesseract.tesseract_cmd` to the installed
`tesseract.exe` path; this is the most common setup failure with pytesseract on
Windows.

Captures and reads text from whatever is currently visible on the ENTIRE screen,
not a specific app — the extracted text is sent to whichever model answers this request.

## Windows desktop control setup

On Windows, the `windows_control` plugin can inspect visible windows and read their
controls. It can also open applications, click controls, close windows, and type
on your actual desktop. Those action tools are powerful: a click or keystroke can
affect whatever is open on the machine.

`config.yaml` sets `windows_control.force_manual_confirmation: true` by default.
It keeps two high-risk actions behind a real approval prompt even when the global
UI approval mode is `auto`: `close_window`, because it can discard unsaved work,
and `send_keystrokes_fallback`, because it types into an unverified currently
focused window. `open_application` and `click_window_control` follow the global
approval mode normally. Keep the override enabled unless you have a carefully
controlled environment.

The `send_keystrokes_fallback` tool uses PyAutoGUI only as a fallback and targets
whichever window is currently focused. PyAutoGUI's failsafe remains enabled:
drag the mouse pointer to any screen corner to abort an in-progress fallback
action immediately.

Force a tier for a single message:

```bash
python -m src.cli --tier local
python -m src.cli --tier premium
```

Run the scheduler (executes tasks defined in `config.yaml` under `scheduled_tasks`):

```bash
python -m src.triggers.scheduler
```

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
- `filesystem_tool.workspace_root`: all file tool operations are sandboxed under this path. The tool
  refuses to touch anything outside it.

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
