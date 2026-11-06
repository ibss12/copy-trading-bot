"""Run one bot check and exit: for GitHub Actions or any other scheduler (see README, "Free: GitHub + Discord").

Usage: python run_scheduled.py [--plan-next]
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from notify.discord import DiscordNotifier
from scheduled.check import STATE_PATH, ScheduledCheck, next_check_at
from trading212.accounts import AccountStore


def _plan_next() -> int:
    now = datetime.now(timezone.utc)
    planned = now + timedelta(minutes=15)
    try:
        try:
            state = json.loads(Path(STATE_PATH).read_text())
            if not isinstance(state, dict):
                state = {}
        except (OSError, ValueError):
            state = {}
        planned = next_check_at(now, state)
    except Exception:
        logging.exception("Could not plan the next bot check")

    epoch = int(planned.timestamp())
    print(planned.isoformat())
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        try:
            with Path(output).open("a") as github_output:
                github_output.write(f"next_check_epoch={epoch}\n")
        except OSError:
            logging.exception("Could not write the next check time to GITHUB_OUTPUT")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan-next", action="store_true")
    args = parser.parse_args()
    if args.plan_next:
        return _plan_next()
    config.validate()
    on_github = os.environ.get("GITHUB_ACTIONS") == "true"
    if not config.DISCORD_WEBHOOK_URL:
        logging.warning("DISCORD_WEBHOOK_URL is not set, so no Discord messages will be sent")
    # GitHub logs of public repos are public: don't print account details there.
    notifier = DiscordNotifier(config.DISCORD_WEBHOOK_URL, echo=not on_github)
    profile = AccountStore().env_profile()
    try:
        ScheduledCheck(notifier, profile, repo_pushed_at=os.environ.get("STOCKBOT_REPO_PUSHED_AT", "")).run()
    except Exception:
        logging.exception("Bot check failed")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
