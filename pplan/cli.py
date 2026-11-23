"""Command-line setup, offline demo, bot runner, and Calendar administration."""

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import sys

from pplan.ai import AIClient
from pplan.core import Config, Plan, PlanError, Store


def main():
    parser = argparse.ArgumentParser(description="Telegram-first parenting schedule")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--database", default="data/plan.sqlite3")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sub.add_parser("demo")
    sub.add_parser("bot")
    sub.add_parser("health")
    cal_login = sub.add_parser("calendar-login")
    cal_login.add_argument("--credentials", default="credentials.json")
    cal_login.add_argument("--token", default="data/google-token.json")
    sub.add_parser("calendar-connect")
    sync = sub.add_parser("calendar-sync")
    sync.add_argument("--force", action="store_true")
    sync.add_argument("--days", type=int, default=60)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.umask(0o077)
    store = None
    try:
        if args.command == "calendar-login":
            from pplan.calendar import login
            login(args.credentials, args.token)
            print("Google authorization saved. Next run calendar-connect.")
            return
        config = Config.load(args.config)
        if args.command == "demo":
            from dataclasses import replace
            config = replace(config, handover_confirmed=True)
        store = Store(":memory:" if args.command in ("demo", "check") else args.database)
        plan = Plan(config, store)
        if args.command == "check":
            print("Configuration valid. Names, schedule, and handover times are loaded from configuration.")
        elif args.command == "demo":
            start = max(plan.today() + timedelta(days=1), config.anchor_date)
            plan.clock = lambda: datetime.combine(start - timedelta(days=1), datetime.min.time(), timezone.utc)
            child = next(iter(config.children))
            old = plan.assignment(child, start)["parent"]
            other = next(p for p in config.parents if p != old)
            request = plan.draft(config.operator_telegram_id, start, start, other, [child])
            print(f"Before confirmation: {config.children[child]} with {config.parents[old]}")
            plan.confirm(config.operator_telegram_id, request["id"])
            for day, c, p in plan.schedule(start, start):
                print(f"{day}: {config.children[c]} with {config.parents[p]}")
            print("Demo used an in-memory database and made no external requests.")
        elif args.command == "health":
            import time
            if time.time() - float(store.get("heartbeat", "0")) > 600:
                raise PlanError("Bot heartbeat is stale.")
        elif args.command == "bot":
            from pplan.telegram import Bot, TelegramClient
            token_file = os.environ.get("TELEGRAM_BOT_TOKEN_FILE")
            token = Path(token_file).read_text().strip() if token_file else os.environ.get("TELEGRAM_BOT_TOKEN", "")
            ai_url = os.environ.get("PPLAN_AI_URL")
            if os.environ.get("PPLAN_AI_ENABLED", "false").lower() != "true":
                ai_url = None
            calendar = None
            token_path = Path(os.environ.get("GOOGLE_TOKEN_PATH", "data/google-token.json"))
            if token_path.exists() and store.get("google_calendar_id"):
                from pplan.calendar import CalendarSync, GoogleClient
                calendar = CalendarSync(plan, GoogleClient(token_path))
            Bot(plan, TelegramClient(token), AIClient(ai_url) if ai_url else None).run(calendar)
        else:
            from pplan.calendar import CalendarSync, GoogleClient
            token_path = os.environ.get("GOOGLE_TOKEN_PATH", "data/google-token.json")
            calendar = CalendarSync(plan, GoogleClient(token_path))
            if args.command == "calendar-connect":
                print("Connected calendar: " + calendar.connect())
            else:
                print(f"Synced {calendar.sync(args.days, args.force)} changed events.")
    except (PlanError, OSError, ValueError, KeyError) as exc:
        # Provider exceptions can contain token-bearing URLs; only PlanError messages are public.
        print(str(exc) if isinstance(exc, PlanError) else f"Setup failed ({type(exc).__name__}). Check configuration and credentials.", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        pass
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    main()
