"""Durable requests implemented in the operator's active Codex chat."""

import argparse
import json
from pathlib import Path
import sys

from pplan.core import Config, Plan, PlanError, Store


def enqueue_improvement(plan, actor, message):
    plan.authorize(actor)
    message = message.strip()
    if not 1 <= len(message) <= 12000:
        raise PlanError("Describe the improvement in 1–12000 characters.")
    now = plan.clock().isoformat()
    cursor = plan.store.db.execute(
        "INSERT INTO improvements(actor, message, created, updated) VALUES (?, ?, ?, ?)",
        (actor, message, now, now),
    )
    return cursor.lastrowid


def list_improvements(plan, include_completed=False):
    sql = "SELECT * FROM improvements WHERE actor=?"
    if not include_completed:
        sql += " AND status!='completed'"
    return [dict(row) for row in plan.store.db.execute(sql + " ORDER BY id", (plan.config.operator_telegram_id,))]


def update_improvement(plan, actor, request_id, status, result=None):
    plan.authorize(actor)
    allowed = {"in_progress": {"queued"}, "completed": {"in_progress"},
               "needs_input": {"in_progress"}, "queued": {"in_progress", "needs_input"}}
    if status not in allowed:
        raise PlanError("Unsupported improvement status.")
    if status in ("completed", "needs_input") and (not isinstance(result, str) or not 1 <= len(result.strip()) <= 2000):
        raise PlanError("Provide a result or question in 1–2000 characters.")
    with plan.store.transaction():
        row = plan.store.db.execute("SELECT * FROM improvements WHERE id=? AND actor=?", (request_id, actor)).fetchone()
        if not row:
            raise PlanError("Improvement request not found.")
        if status in ("completed", "needs_input") and row["status"] == status and row["result"] == result:
            return dict(row)
        if row["status"] not in allowed[status]:
            raise PlanError(f"Cannot move improvement #{request_id} from {row['status']} to {status}.")
        plan.store.db.execute("UPDATE improvements SET status=?, updated=?, result=? WHERE id=?",
                              (status, plan.clock().isoformat(), result, request_id))
        if status in ("completed", "needs_input"):
            label = "completed" if status == "completed" else "needs your input"
            plan.store.enqueue({"method": "sendMessage", "payload": {
                "chat_id": actor, "text": f"Improvement #{request_id} {label}.\n\n{result}",
            }})
        return dict(plan.store.db.execute("SELECT * FROM improvements WHERE id=?", (request_id,)).fetchone())


def main():
    parser = argparse.ArgumentParser(description="Manage app improvements from the active Codex chat")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--database", default="data/plan.sqlite3")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--all", action="store_true")
    for name in ("start", "complete", "needs-input", "requeue"):
        command = commands.add_parser(name)
        command.add_argument("id", type=int)
        if name in ("complete", "needs-input"):
            command.add_argument("--result-file", required=True)
    args = parser.parse_args()
    store = None
    try:
        if not Path(args.database).is_file():
            raise PlanError("The plan database does not exist; refusing to create another queue.")
        store = Store(args.database)
        plan = Plan(Config.load(args.config), store)
        if args.command == "list":
            result = list_improvements(plan, args.all)
        else:
            statuses = {"start": "in_progress", "complete": "completed", "needs-input": "needs_input", "requeue": "queued"}
            summary = Path(args.result_file).read_text().strip() if hasattr(args, "result_file") else None
            result = update_improvement(plan, plan.config.operator_telegram_id, args.id, statuses[args.command], summary)
        print(json.dumps(result, ensure_ascii=False))
    except (PlanError, OSError, ValueError) as exc:
        print(str(exc) if isinstance(exc, PlanError) else f"Queue command failed ({type(exc).__name__}).", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    main()
