"""Interface layer: a simple REPL. Swap this out for a Telegram bot, GUI, or
anything else later — it should only ever call orchestrator.handle_message().
"""
from __future__ import annotations

import argparse
import uuid

from .config import load_settings
from .orchestrator import Orchestrator


def cli_confirm(command: str) -> bool:
    answer = input(f"\n[shell] About to run: {command}\nProceed? [y/N] ").strip().lower()
    return answer == "y"


def main() -> None:
    parser = argparse.ArgumentParser(description="Personal AI assistant chat loop.")
    parser.add_argument(
                "--tier",
        choices=["local", "premium", "free_api"],
        default=None,
        help="Force a model tier for this session.",
    )
    parser.add_argument("--config", default=None, help="Path to a config.yaml override.")
    args = parser.parse_args()

    settings = load_settings(args.config)
    orchestrator = Orchestrator(settings, confirm_fn=cli_confirm)

    session_id = str(uuid.uuid4())
    print("Personal AI assistant ready. Type 'exit' to quit.")
    if args.tier:
        print(f"(tier forced to: {args.tier})")

    while True:
        try:
            user_input = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye.")
            break

        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            print("bye.")
            break

        try:
            reply = orchestrator.handle_message(session_id, user_input, force_tier=args.tier)
        except Exception as e:
            reply = f"[error] {e}"

        print(f"\nassistant> {reply}")


if __name__ == "__main__":
    main()
