"""Private-chat Telegram adapter with durable replies and update deduplication."""

from datetime import date, datetime, timedelta
import base64
import json
import logging
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from pplan.ai import context_for
from pplan.core import PlanError
from pplan.parser import HELP, parse_request

log = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    def __init__(self, status=0):
        super().__init__(f"Telegram request failed (status {status}).")
        self.status = status


class TelegramClient:
    def __init__(self, token):
        if not token or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:_-" for c in token):
            raise PlanError("A valid Telegram bot token is required.")
        self.base = "https://api.telegram.org/bot" + token

    def call(self, method, payload):
        try:
            if method == "sendPhoto" and "photo_base64" in payload:
                boundary = "pplan" + uuid4().hex
                parts = []
                for key, value in payload.items():
                    if key != "photo_base64":
                        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode())
                parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="photo"; filename="schedule.png"\r\nContent-Type: image/png\r\n\r\n').encode())
                parts.append(base64.b64decode(payload["photo_base64"]))
                parts.append(f"\r\n--{boundary}--\r\n".encode())
                data, content_type = b"".join(parts), "multipart/form-data; boundary=" + boundary
            else:
                data, content_type = json.dumps(payload).encode(), "application/json"
            request = Request(self.base + "/" + method, data=data, headers={"Content-Type": content_type})
            with urlopen(request, timeout=55) as response:
                result = json.load(response)
            if not result.get("ok"):
                raise TelegramError(result.get("error_code", 0))
            return result["result"]
        except HTTPError as exc:
            raise TelegramError(exc.code) from None
        except (URLError, OSError, ValueError):
            raise TelegramError() from None


class Bot:
    def __init__(self, plan, client, ai=None):
        self.plan, self.store, self.client, self.ai = plan, plan.store, client, ai

    def say(self, chat, text, buttons=None):
        # Keep below Telegram's 4096-character limit even with long configured names.
        chunks = [text[i:i + 3500] for i in range(0, len(text), 3500)] or [""]
        for index, chunk in enumerate(chunks):
            payload = {"chat_id": chat, "text": chunk}
            if buttons and index == len(chunks) - 1:
                payload["reply_markup"] = {"inline_keyboard": [buttons]}
            self.store.enqueue({"method": "sendMessage", "payload": payload})

    def show_schedule(self, chat, start, end):
        from pplan.calendar_image import WEEKDAYS, calendar_bounds, date_range_label, night_count_periods, render_calendar
        png = render_calendar(self.plan, start, end)
        visible_start, visible_end = calendar_bounds(start, end)
        config = self.plan.config
        times = {*config.school_handover_times.values(), config.non_school_handover_time}
        if len(times) == 1:
            handovers = f"Handover: {next(iter(times))} every day."
        else:
            handovers = "School days: " + ", ".join(f"{WEEKDAYS[int(day)]} {value}" for day, value in sorted(config.school_handover_times.items()))
            handovers += f"; non-school days: {config.non_school_handover_time}."
        caption = (f"{visible_start} through {visible_end} · {config.timezone}\n{handovers}\n"
                   "Split colors show the parent before handover on the left and after handover on the right. Children are listed only when their assignments differ.")
        if not config.handover_confirmed:
            caption += "\nSetup pending: displayed times are examples, not confirmed handover times."
        periods = night_count_periods(start, end)
        totals = [self.plan.overnight_totals(period_start, period_end) for _, period_start, period_end in periods]
        summary = ["Scheduled nights by child"]
        summary += [f"{name} · {date_range_label(period_start, period_end)}"
                    for name, period_start, period_end in periods]
        for child, child_name in config.children.items():
            summary.append(f"\n{child_name}")
            for parent, parent_name in config.parents.items():
                year_count, range_count = (period_totals[child][parent] for period_totals in totals)
                summary.append(f"{parent_name}: {year_count} since January 1 · {range_count} in requested period")
        summary_text = "\n".join(summary)
        full_caption = f"{caption}\n\n{summary_text}"
        summary_fits = len(full_caption.encode("utf-16-le")) <= 2048
        self.store.enqueue({"method": "sendPhoto", "payload": {
            "chat_id": chat, "caption": full_caption if summary_fits else caption,
            "photo_base64": base64.b64encode(png).decode(),
        }})
        if not summary_fits:
            self.say(chat, summary_text)

    def preview(self, chat, request):
        config = self.plan.config
        if request.get("actions"):
            from itertools import groupby
            actions = json.loads(request["actions"])
            lines = [f"Change preview · {len(actions)} action(s)", f"Timezone: {config.timezone}"]
            for index, action in enumerate(actions, 1):
                children = ", ".join(config.children[c] for c in action["children"])
                start = date.fromisoformat(action["start"])
                if action["type"] in ("assign", "overwrite"):
                    end = date.fromisoformat(action["end"]) + timedelta(days=1)
                    lines += [f"\n{index}. {children} with {config.parents[action['parent']]}",
                              f"From: {config.boundary(start):%Y-%m-%d %H:%M}",
                              f"Until: {config.boundary(end):%Y-%m-%d %H:%M}"]
                    if action["type"] == "overwrite":
                        lines.append("Overwrites the selected days' existing assignments, including matching ones.")
                else:
                    runs = [f"{sum(1 for _ in group)} day(s) with {config.parents[parent]}"
                            for parent, group in groupby(action["cycle"])]
                    lines += [f"\n{index}. Repeating schedule for {children}",
                              f"From: {config.boundary(start):%Y-%m-%d %H:%M}, ongoing",
                              " → ".join(runs), f"Repeat every {len(action['cycle'])} days."]
            if any(action["type"] == "set_cycle" for action in actions):
                lines.append("\nNew repeating schedules replace later repeating rules. Previously confirmed date-specific exceptions are kept.")
            lines.append("\nNo changes applied. Confirm applies ALL actions together. Preview expires after 24 hours or its first handover.")
            self.say(chat, "\n".join(lines), [
                {"text": "Confirm all", "callback_data": "confirm:" + request["id"]},
                {"text": "Cancel", "callback_data": "cancel:" + request["id"]},
            ])
            return
        start, end = date.fromisoformat(request["start"]), date.fromisoformat(request["end"])
        children = ", ".join(config.children[c] for c in json.loads(request["children"]))
        text = (f"Change preview\nChildren: {children}\nParent: {config.parents[request['parent']]}\n"
                f"From: {config.boundary(start):%Y-%m-%d %H:%M}\n"
                f"Until: {config.boundary(end + timedelta(days=1)):%Y-%m-%d %H:%M}\n"
                f"Timezone: {config.timezone}\n"
                f"Handover dates: {start} through {end} (inclusive).\n"
                "No change has been applied. Preview expires after 24 hours or when its first handover passes.")
        self.say(chat, text, [
            {"text": "Confirm", "callback_data": "confirm:" + request["id"]},
            {"text": "Cancel", "callback_data": "cancel:" + request["id"]},
        ])

    def handle_update(self, update):
        update_id = update.get("update_id")
        if type(update_id) is not int or update_id < int(self.store.get("telegram_offset", "0")):
            return
        callback = update.get("callback_query")
        message = (callback or {}).get("message") if callback else update.get("message")
        sender = (callback or message or {}).get("from", {})
        chat = (message or {}).get("chat", {})
        actor = sender.get("id")
        text = (message or {}).get("text", "")
        private = chat.get("type") == "private" and chat.get("id") == actor
        result, interpretation_error = None, None
        # Interpret outside the write transaction. Only the allowlisted private user can use AI.
        if private and actor == self.plan.config.operator_telegram_id and not callback and isinstance(text, str) and not text.startswith("/"):
            try:
                if not 0 < len(text) <= 2000:
                    raise PlanError("Please keep requests between 1 and 2000 characters.")
                try:
                    result = parse_request(text, self.plan.config)
                except PlanError:
                    if not self.ai:
                        raise
                    history = json.loads(self.store.get("conversation", "[]"))
                    result = self.ai.interpret(text, context_for(self.plan, history))
            except PlanError as exc:
                interpretation_error = str(exc)
        with self.store.transaction():
            try:
                if private:
                    if not callback and text == "/whoami":
                        self.say(actor, f"Your Telegram user ID: {actor}")
                    else:
                        self.plan.authorize(actor)
                        if callback:
                            action, request_id = callback.get("data", "").split(":", 1)
                            if action == "confirm":
                                self.plan.confirm(actor, request_id)
                                self.say(actor, "Schedule updated. Use /schedule for a fresh calendar. Google Calendar will sync when connected; use /status to check.")
                            elif action == "cancel":
                                self.plan.cancel(actor, request_id)
                                self.say(actor, "Request cancelled. The schedule is unchanged.")
                            else:
                                raise PlanError("Unknown action.")
                        elif interpretation_error:
                            self.say(actor, interpretation_error)
                        elif text.startswith("/"):
                            self.command(actor, text)
                        elif result:
                            self.apply_interpretation(actor, text, result)
            except (PlanError, ValueError) as exc:
                if private:
                    self.say(actor, str(exc) if isinstance(exc, PlanError) else "Invalid command or button. Use /help.")
            if callback:
                self.store.enqueue({"method": "answerCallbackQuery", "payload": {"callback_query_id": callback["id"]}})
            self.store.set("telegram_offset", update_id + 1)

    def apply_interpretation(self, actor, text, result):
        if result["action"] == "clarify":
            history = json.loads(self.store.get("conversation", "[]"))
            history += [{"role": "user", "text": text}, {"role": "assistant", "text": result["question"]}]
            self.store.set("conversation", json.dumps(history[-6:]))
            self.say(actor, result["question"])
            return
        if result["action"] == "changes":
            request = self.plan.draft_actions(actor, result["actions"])
            self.preview(actor, request)
        else:
            start, end = date.fromisoformat(result["start"]), date.fromisoformat(result["end"])
            if result["action"] == "schedule":
                self.show_schedule(actor, start, end)
            else:
                request = self.plan.draft(actor, start, end, result["parent"], result["children"])
                self.preview(actor, request)
        self.store.set("conversation", "[]")

    def command(self, actor, text):
        parts = text.split()
        command = parts[0].split("@")[0]
        if command in ("/start", "/help"):
            config = self.plan.config
            members = f"Parents: {', '.join(config.parents.values())}\nChildren: {', '.join(config.children.values())}"
            self.say(actor, "Parenting Plan · solo mode\n" + members + "\n\n" + HELP)
        elif command == "/schedule":
            from pplan.calendar_image import default_calendar_range
            start, end = default_calendar_range(self.plan.today())
            if len(parts) == 3:
                start, end = date.fromisoformat(parts[1]), date.fromisoformat(parts[2])
            elif len(parts) != 1:
                raise PlanError("Use /schedule or /schedule YYYY-MM-DD YYYY-MM-DD.")
            self.show_schedule(actor, start, end)
        elif command == "/requests":
            rows = self.store.db.execute("SELECT * FROM requests ORDER BY rowid DESC LIMIT 5").fetchall()
            if not rows:
                self.say(actor, "No change requests yet.")
            for row in rows:
                current = dict(row)
                if current["status"] == "draft" and self.plan.clock() < datetime.fromisoformat(current["expires"]) and self.plan.config.boundary(date.fromisoformat(current["start"])) > self.plan.clock():
                    self.preview(actor, current)
                else:
                    status = "expired" if current["status"] == "draft" else current["status"]
                    self.say(actor, f"{current['start']} through {current['end']}: {status}")
        elif command == "/overwrite":
            from pplan.parser import parse_preview_overwrite
            body = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) == 2 else ""
            if not body:
                raise PlanError("Use /overwrite YYYY-MM-DD through YYYY-MM-DD to <parent> for both, or paste numbered From/Until blocks after /overwrite.")
            if "from:" in body.casefold() and "until:" in body.casefold():
                actions = parse_preview_overwrite(body, self.plan.config)
            else:
                actions = parse_request("Overwrite " + body, self.plan.config)["actions"]
            self.preview(actor, self.plan.draft_actions(actor, actions))
        elif command == "/special":
            if len(parts) == 2 and parts[1].casefold() == "list":
                events = self.plan.special_events()
                lines = [f"#{event['id']}: {event['start']} through {event['end']} — {event['title'] or 'Special event'}" for event in events]
                self.say(actor, "\n".join(lines) if lines else "No special events marked yet.")
            elif len(parts) == 3 and parts[1].casefold() == "remove":
                event = self.plan.remove_special(actor, int(parts[2]))
                self.say(actor, f"Special event #{event['id']} removed.")
                self.show_schedule(actor, date.fromisoformat(event["start"]), date.fromisoformat(event["end"]))
            elif len(parts) >= 2:
                import re
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[1]):
                    raise PlanError("Use /special YYYY-MM-DD [YYYY-MM-DD] [title]. Event dates are inclusive.")
                start = date.fromisoformat(parts[1])
                end, title_index = start, 2
                if len(parts) > 2 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[2]):
                    end, title_index = date.fromisoformat(parts[2]), 3
                event = self.plan.add_special(actor, start, end, " ".join(parts[title_index:]))
                self.say(actor, f"Special event #{event['id']} marked: {start} through {end}, inclusive.\n"
                         "The parenting schedule is unchanged. Use /special remove " + str(event["id"]) + " to remove the frame.")
                self.show_schedule(actor, start, end)
            else:
                raise PlanError("Use /special YYYY-MM-DD [YYYY-MM-DD] [title], /special list, or /special remove <id>. Event dates are inclusive.")
        elif command == "/improve":
            from pplan.feedback import enqueue_improvement
            message = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) == 2 else ""
            if not message:
                raise PlanError("Use /improve followed by the change you want to the app.")
            request_id = enqueue_improvement(self.plan, actor, message)
            self.say(actor, f"Improvement #{request_id} saved for your Codex chat. It will be picked up when work continues there. Use /improvements to check its status.")
        elif command == "/improvements":
            rows = self.store.db.execute(
                "SELECT id, message, status, result FROM improvements WHERE actor=? ORDER BY id DESC LIMIT 5", (actor,)
            ).fetchall()
            if not rows:
                self.say(actor, "No app improvements requested yet. Send /improve followed by your idea.")
            for row in rows:
                self.say(actor, f"Improvement #{row['id']}: {row['status']}\n{row['message'][:500]}"
                         + (f"\n{row['result']}" if row['result'] else ""))
        elif command == "/status":
            connected = bool(self.store.get("google_calendar_id"))
            self.say(actor, f"Google Calendar: {'connected' if connected else 'not connected'}\n"
                     f"Last successful sync: {self.store.get('calendar_last_success', 'never')}\n"
                     f"Sync issue: {self.store.get('calendar_error', 'none')}\n"
                     f"AI: {'configured' if self.ai else 'explicit requests only'}")
        else:
            raise PlanError("Unknown command. Use /help.")

    def flush(self):
        for row in self.store.db.execute("SELECT * FROM outbox ORDER BY id LIMIT 50").fetchall():
            job = json.loads(row["payload"])
            try:
                self.client.call(job["method"], job["payload"])
            except TelegramError as exc:
                if job["method"] != "answerCallbackQuery" or exc.status != 400:
                    log.warning("Reply delivery deferred: %s", exc)
                    break
            self.store.db.execute("DELETE FROM outbox WHERE id=?", (row["id"],))

    def run(self, calendar=None):
        next_sync = 0
        while True:
            self.flush()
            if calendar and (time.monotonic() >= next_sync or self.store.get("calendar_dirty") == "1"):
                try:
                    calendar.sync()
                except Exception:
                    # Provider errors can include credential-bearing request URLs; never log raw errors.
                    self.store.set("calendar_error", "Sync failed; retry scheduled. Check Google authorization.")
                    self.store.set("calendar_dirty", "0")
                    log.warning("Google Calendar sync failed; retry scheduled.")
                next_sync = time.monotonic() + 900
            try:
                updates = self.client.call("getUpdates", {
                    "offset": int(self.store.get("telegram_offset", "0")), "timeout": 30,
                    "allowed_updates": ["message", "callback_query"],
                })
                for update in updates:
                    self.handle_update(update)
                self.store.set("heartbeat", time.time())
            except TelegramError as exc:
                log.warning("Polling retry: %s", exc)
                time.sleep(5)
