# Personal AI Assistant / Agent Architecture

A blueprint for a personal, multi-plugin AI system: free-first, premium-when-needed, extensible to whatever use cases you add later (automation on your laptop, research, scheduling, whatever).

---

## 1. The mental model — 6 layers

Think of the system as layers, not one big app. This is what lets you add "use cases" later without rebuilding anything.

```
┌─────────────────────────────────────────────┐
│  1. INTERFACE      — how you talk to it       │
│     (chat window, CLI, Telegram bot, voice)   │
├─────────────────────────────────────────────┤
│  2. ORCHESTRATOR   — the "brain" / router     │
│     (decides which model + which tools to use)│
├─────────────────────────────────────────────┤
│  3. MODEL LAYER    — the actual LLMs          │
│     (free local models + premium API models)  │
├─────────────────────────────────────────────┤
│  4. TOOL / PLUGIN LAYER — what it can DO      │
│     (MCP servers: files, browser, apps, APIs) │
├─────────────────────────────────────────────┤
│  5. MEMORY LAYER   — what it remembers        │
│     (vector DB + structured state)            │
├─────────────────────────────────────────────┤
│  6. TRIGGER LAYER  — what wakes it up         │
│     (cron jobs, file watchers, hotkeys, events)│
└─────────────────────────────────────────────┘
```

Every "plugin" or "agent" you add later just slots into layer 4. Every new automation just slots into layer 6. You never touch the others.

---

## 2. Three ways to build the foundation

Pick based on how much you want to code vs. configure. You can start with the easiest and graduate later — they're not mutually exclusive.

### Path A — Fastest, least code: **Goose** (recommended starting point)
Goose is a free, open-source desktop AI agent (Mac/Linux/Windows) plus a CLI, built by Block. It's the closest thing to "buy, don't build" for exactly what you described:
- Works with 15+ model providers — Anthropic, OpenAI, Google, **and Ollama** (local/free) — so you can flip between free local models and premium GPT/Claude per task.
- Connects to 70+ extensions through **MCP** (Model Context Protocol) — file system, browser, GitHub, Slack, Google stuff, and more, with new community servers appearing constantly.
- Runs actual tasks on your machine (files, terminal, code), not just chat.
- You configure it, you barely code it.

Start here if you want something working *this week*.

### Path B — No-code automation glue: **n8n**
Self-hosted, free, visual workflow builder with native AI/agent nodes and huge integration library (Gmail, calendar, Notion, webhooks, RSS, etc.). Great for the "trigger → do a sequence of steps → call an LLM somewhere in the middle" pattern — daily briefings, inbox triage, file processing pipelines.
- Pairs *with* Path A/C rather than replacing them — use n8n for scheduled/event-driven automations, and Goose or your own agent for interactive/on-demand work.

### Path C — Full custom control: **LangGraph** (or CrewAI for speed)
If you eventually want a genuinely custom multi-agent system (specialized sub-agents handing off tasks, custom memory logic, your own UI):
- **LangGraph** — most control, best for production-grade reliability (durable state, branching, human-in-the-loop). Steeper learning curve.
- **CrewAI** — role-based agents ("researcher", "coder", "planner"), much faster to prototype, slightly less rigorous under complex multi-step tasks.
- Both speak MCP natively now, so they reuse the same plugin layer as Path A.

**My recommendation for you specifically:** start with **Goose + MCP servers** (Path A) to get a solid, working foundation fast. Add **n8n** once you know which automations you actually want scheduled/triggered. Only drop into **LangGraph/CrewAI** (Path C) if you hit something Goose genuinely can't do — a fully custom multi-agent workflow.

---

## 3. Model layer — free + premium together

Don't route every request to a premium model; it's wasteful and unnecessary.

| Tier | Tool | Use for |
|---|---|---|
| **Free/local** | Ollama, LM Studio, or Jan.ai running open-weight models (Llama, Qwen, Gemma, DeepSeek) | Quick queries, drafting, simple classification, anything repetitive, privacy-sensitive local file stuff |
| **Premium (pay-per-use)** | Claude API / OpenAI API | Complex reasoning, coding, multi-step planning, anything where quality really matters |
| **Router** | LiteLLM (free, open-source proxy) | Sits in front of both — one unified API, lets your orchestrator or Goose pick the right model per task without rewriting code |

Rule of thumb: local model as default, premium model as an escalation when the task is hard or the stakes are higher.

---

## 4. Tool/plugin layer — MCP is the standard to build on

**MCP (Model Context Protocol)** is now the common language most of these frameworks speak — Goose, LangGraph, CrewAI, Claude Desktop, and others all support it. This matters because it means:
- Plugins you set up once (a filesystem server, a browser-control server, a Gmail/calendar server) work across whichever orchestrator/agent you're running.
- There's a growing library of free community MCP servers you can just plug in rather than write from scratch — for browsing, git, databases, cloud services, Slack, docs, etc.

Practical starting set for "laptop automation":
- **Filesystem MCP server** — read/write/organize files
- **Browser automation** (Playwright-based MCP server, or Claude/Chrome computer-use style tools) — web tasks
- **Terminal/code execution** (Open Interpreter, or a shell MCP server) — run scripts, install things, automate repetitive dev tasks
- **Calendar/email MCP servers** — scheduling and inbox stuff once you're ready for it

---

## 5. Memory layer

Two kinds of memory, don't conflate them:
- **Structured state** (what you're working on, preferences, ongoing tasks) — a simple local SQLite file is genuinely enough at personal scale.
- **Semantic/long-term memory** (things it should recall later, notes, past conversations) — a local vector DB: **Chroma** or **Qdrant** (both free, self-hostable, run fine on a laptop).

Goose and most agent frameworks can be pointed at these directly; you don't need to build a memory system from scratch.

---

## 6. Trigger layer — what makes it "automation" and not just chat

This is the difference between an assistant you talk to and one that actually works for you:
- **Cron / scheduled tasks** — n8n schedules, or OS-level (cron on Mac/Linux, Task Scheduler on Windows)
- **File/folder watchers** — "when a new file lands here, do X"
- **Webhooks** — "when this API event fires, do X" (n8n handles this natively)
- **Hotkey/manual trigger** — for on-demand runs

---

## 7. Build order (don't build it all at once)

**Phase 1 — Foundation (week 1-2)**
- Install Goose, connect it to Ollama (free local model) and your Claude/OpenAI API key (premium)
- Add 2-3 MCP servers: filesystem, browser, terminal
- Get comfortable giving it real tasks on your laptop

**Phase 2 — Memory (week 3)**
- Add a local vector DB (Chroma) for long-term recall
- Add SQLite for tracking ongoing tasks/state

**Phase 3 — Automation (week 4+)**
- Stand up n8n alongside it
- Pick your first 2-3 real automations (the ones you already know you want) and build those as triggered workflows, calling into Goose/your model layer where reasoning is needed

**Phase 4 — Scale out**
- Add MCP servers / plugins one at a time as new use cases show up
- Only move to LangGraph/CrewAI if you hit a workflow Goose genuinely can't express

---

## 8. Cost control checklist
- Default every task to a free local model; escalate to premium only when needed
- Cache/reuse results where possible (don't re-call an LLM for the same repeated check)
- Use LiteLLM's routing/cost tracking to see where your premium spend is actually going before you scale up automations
