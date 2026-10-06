"""Run one bot check and exit: for GitHub Actions or any other scheduler (see README, "Free: GitHub + Discord").

Usage: python run_scheduled.py
"""

import logging
import os
import sys

import config
from notify.discord import DiscordNotifier
from scheduled.check import ScheduledCheck
from trading212.accounts import AccountStore


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
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
