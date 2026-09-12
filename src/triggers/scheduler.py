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

from ..config import load_settings
from ..orchestrator import Orchestrator

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


def main() -> None:
    settings = load_settings()
    orchestrator = Orchestrator(settings, confirm_fn=unattended_confirm)

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

    log.info("Scheduler running. Press Ctrl+C to stop.")
    scheduler.start()


if __name__ == "__main__":
    main()
