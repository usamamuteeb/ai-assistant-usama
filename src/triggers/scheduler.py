"""Trigger layer: runs the orchestrator headlessly on a cron schedule defined
in config.yaml under `scheduled_tasks`. This is what turns the assistant from
"something you chat with" into "something that works for you in the background."

Run with: python -m src.triggers.scheduler
"""
from __future__ import annotations

import sys

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import logging
import uuid

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ..config import load_settings
from ..memory.store import SqliteStore
from ..orchestrator import Orchestrator
from plugins.task_manager.plugin import deliver_due_reminders

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("scheduler")


def unattended_confirm(command: str) -> bool:
    # Unattended runs fail closed on shell commands by default. Flip config.yaml's
    # shell_tool.require_confirmation to false, and change this to `return True`,
    # only for automations you've already reviewed and trust.
    log.warning("Shell command requested by a scheduled task but confirmation is required: %s", command)
    return False


def run_task(orchestrator: Orchestrator, name: str, prompt: str, tier: str | None) -> None:
    log.info("Running scheduled task '%s'", name)
    session_id = f"scheduled:{name}:{uuid.uuid4()}"
    try:
        result = orchestrator.handle_message(session_id, prompt, force_tier=tier)
        log.info("Task '%s' finished: %s", name, result[:200])
    except Exception:
        log.exception("Task '%s' failed", name)


def run_due_reminders(store: SqliteStore) -> None:
    """Deliver reminders without invoking the model or consuming API tokens."""
    try:
        deliver_due_reminders(store, logger=log)
    except Exception:
        log.exception("Reminder dispatch failed")


def main() -> None:
    settings = load_settings()
    orchestrator = Orchestrator(settings, confirm_fn=unattended_confirm)
    reminder_store = SqliteStore(settings.sqlite_path())

    scheduler = BlockingScheduler()
    tasks = settings.scheduled_tasks
    active = [t for t in tasks if t.get("enabled", False)]

    if not active:
        log.warning(
            "No scheduled tasks are enabled in config.yaml. Set enabled: true on a task to activate it."
        )

    for task in active:
        scheduler.add_job(
            run_task,
            CronTrigger.from_crontab(task["cron"]),
            args=[orchestrator, task["name"], task["prompt"], task.get("tier")],
            id=task["name"],
        )
        log.info("Scheduled '%s' with cron '%s'", task["name"], task["cron"])

    task_config = settings.raw.get("task_manager", {})
    poll_seconds = 15
    if isinstance(task_config, dict):
        try:
            poll_seconds = max(5, int(task_config.get("reminder_poll_seconds", 15)))
        except (TypeError, ValueError):
            pass
    scheduler.add_job(
        run_due_reminders,
        IntervalTrigger(seconds=poll_seconds),
        args=[reminder_store],
        id="reminder_dispatch",
        coalesce=True,
        max_instances=1,
    )
    log.info("Reminder dispatcher polling every %ss", poll_seconds)

    log.info("Scheduler running. Press Ctrl+C to stop.")
    scheduler.start()


if __name__ == "__main__":
    main()
