from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
import base64
from io import BytesIO
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pplan.ai import CodexInterpreter, context_for, validate_result
from pplan.calendar import CalendarSync
from pplan.calendar_image import assignments_for_day, calendar_bounds, calendar_pages, default_calendar_range, render_calendar
from pplan.core import Config, Plan, PlanError, Store
from pplan.parser import parse_request
from pplan.telegram import Bot, TelegramClient


class Fixture(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 22, 20, tzinfo=timezone.utc)
        self.config = Config("America/Los_Angeles", 42, {"p1": "First", "p2": "Second"},
                             {"c1": "Older", "c2": "Younger"}, date(2026, 11, 23),
                             tuple(["p1"] * 7 + ["p2"] * 7), "p1")
        self.store = Store(":memory:")
        self.plan = Plan(self.config, self.store, lambda: self.now)
        self.start = date(2026, 11, 23)

    def tearDown(self):
        self.store.close()

    def draft(self, children=("c1",), day=None, parent="p2"):
        day = day or self.start
        return self.plan.draft(42, day, day, parent, children)


class PlanTests(Fixture):
    def test_weekly_baseline_and_no_pre_plan_history(self):
        self.assertEqual(self.plan.assignment("c1", self.start)["parent"], "p1")
        self.assertEqual(self.plan.assignment("c2", date(2026, 11, 30))["parent"], "p2")
        with self.assertRaises(PlanError):
            self.plan.assignment("c1", self.start - timedelta(days=1))

    def test_split_children_only_after_confirmation(self):
        request = self.draft()
        self.assertEqual(self.plan.assignment("c1", self.start)["parent"], "p1")
        self.plan.confirm(42, request["id"])
        self.assertEqual(self.plan.assignment("c1", self.start)["parent"], "p2")
        self.assertEqual(self.plan.assignment("c2", self.start)["parent"], "p1")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM audit").fetchone()[0], 2)

    def test_both_children_change_atomically(self):
        request = self.draft(("c1", "c2"))
        self.plan.confirm(42, request["id"])
        self.assertEqual({p for _, _, p in self.plan.schedule(self.start, self.start)}, {"p2"})

    def test_authorization_and_duplicate_confirmation(self):
        request = self.draft()
        with self.assertRaises(PlanError):
            self.plan.confirm(99, request["id"])
        self.plan.confirm(42, request["id"])
        with self.assertRaises(PlanError):
            self.plan.confirm(42, request["id"])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM audit WHERE action='confirmed'").fetchone()[0], 1)

    def test_overlapping_preview_becomes_stale(self):
        first, stale = self.draft(), self.draft()
        self.plan.confirm(42, first["id"])
        with self.assertRaisesRegex(PlanError, "schedule changed"):
            self.plan.confirm(42, stale["id"])

    def test_other_child_does_not_invalidate_preview(self):
        first, second = self.draft(), self.draft(("c2",))
        self.plan.confirm(42, first["id"])
        self.plan.confirm(42, second["id"])

    def test_reverted_schedule_still_invalidates_old_preview(self):
        stale, first = self.draft(), self.draft()
        self.plan.confirm(42, first["id"])
        revert = self.draft(parent="p1")
        self.plan.confirm(42, revert["id"])
        with self.assertRaisesRegex(PlanError, "schedule changed"):
            self.plan.confirm(42, stale["id"])

    def test_expired_and_cancelled_requests_do_not_apply(self):
        request = self.draft()
        self.now += timedelta(hours=24)
        with self.assertRaisesRegex(PlanError, "expired"):
            self.plan.confirm(42, request["id"])
        request2 = self.draft(day=self.start + timedelta(days=1))
        self.plan.cancel(42, request2["id"])
        with self.assertRaises(PlanError):
            self.plan.confirm(42, request2["id"])

    def test_handover_passed_after_preview(self):
        self.now = datetime(2026, 11, 23, 22, tzinfo=timezone.utc)  # 14:00 Seattle
        request = self.draft()
        self.now += timedelta(hours=1)
        with self.assertRaisesRegex(PlanError, "past"):
            self.plan.confirm(42, request["id"])

    def test_invalid_ranges_and_children(self):
        for children in ((), ("unknown",), ("c1", "c1")):
            with self.assertRaises(PlanError):
                self.draft(children)
        with self.assertRaises(PlanError):
            self.plan.draft(42, self.start, self.start + timedelta(days=31), "p2", ["c1"])

    def test_handover_school_weekend_holiday_and_dst(self):
        self.assertEqual(self.config.boundary(self.start).hour, 15)
        self.assertEqual(self.config.boundary(date(2026, 11, 28)).hour, 10)
        holidays = replace(self.config, non_school_dates=("2026-11-23",))
        self.assertEqual(holidays.boundary(self.start).hour, 10)
        a = self.config.boundary(date(2027, 3, 13)).astimezone(timezone.utc)
        b = self.config.boundary(date(2027, 3, 14)).astimezone(timezone.utc)
        self.assertEqual(b - a, timedelta(hours=23))

    def test_changed_configuration_requires_explicit_new_plan(self):
        self.draft()
        with self.assertRaisesRegex(PlanError, "Configuration differs"):
            Plan(replace(self.config, cycle=("p2", "p1")), self.store)

    def test_initial_setup_can_be_completed_before_first_request(self):
        Plan(replace(self.config, operator_parent_id="p2"), self.store)

    def test_unconfirmed_example_times_block_changes(self):
        with StoreContext() as store:
            plan = Plan(replace(self.config, handover_confirmed=False), store, lambda: self.now)
            with self.assertRaisesRegex(PlanError, "not been configured"):
                plan.draft(42, self.start, self.start, "p2", ["c1"])


class StoreContext:
    def __enter__(self):
        self.store = Store(":memory:")
        return self.store

    def __exit__(self, *args):
        self.store.close()


class BotTests(Fixture):
    def setUp(self):
        super().setUp()
        self.sent = []
        self.client = SimpleNamespace(call=lambda method, payload: self.sent.append((method, payload)))
        self.bot = Bot(self.plan, self.client)

    def message(self, update_id=1, actor=42, text="Assign 2026-11-23 to Second for Older", chat_type="private"):
        return {"update_id": update_id, "message": {"from": {"id": actor}, "chat": {"id": actor, "type": chat_type}, "text": text}}

    def test_replayed_update_does_not_create_another_draft(self):
        self.bot.handle_update(self.message())
        self.bot.handle_update(self.message())
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM requests").fetchone()[0], 1)
        self.assertEqual(self.plan.assignment("c1", self.start)["parent"], "p1")
        self.bot.flush()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Confirm", json.dumps(self.sent))

    def test_stranger_and_group_do_not_invoke_ai_or_create_drafts(self):
        self.bot.ai = SimpleNamespace(interpret=lambda *args: self.fail("Unauthorized AI invocation"))
        self.bot.handle_update(self.message(actor=99))
        self.bot.handle_update(self.message(update_id=2, chat_type="group"))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM requests").fetchone()[0], 0)

    def test_transaction_rolls_back_change_and_offset_when_reply_cannot_be_saved(self):
        with patch.object(self.store, "enqueue", side_effect=RuntimeError("disk failure")):
            with self.assertRaises(RuntimeError):
                self.bot.handle_update(self.message())
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM requests").fetchone()[0], 0)
        self.assertIsNone(self.store.get("telegram_offset"))

    def test_confirmation_callback_applies_once(self):
        request = self.draft()
        update = {"update_id": 1, "callback_query": {"id": "button1", "from": {"id": 42},
                  "data": "confirm:" + request["id"], "message": {"chat": {"id": 42, "type": "private"}}}}
        self.bot.handle_update(update)
        self.bot.handle_update(update)
        self.assertEqual(self.plan.request(request["id"])["status"], "confirmed")

    def test_clarification_history_is_passed_to_next_message(self):
        contexts = []
        def interpret(text, context):
            contexts.append(context)
            return {"action": "clarify", "start": None, "end": None, "parent": None,
                    "children": [], "question": "Which child?"}
        self.bot.ai = SimpleNamespace(interpret=interpret)
        self.bot.handle_update(self.message(text="Please change next week"))
        self.bot.handle_update(self.message(update_id=2, text="The older child"))
        self.assertEqual(contexts[1]["conversation"][-1]["text"], "Which child?")

    def test_improvement_is_saved_once_without_changing_schedule(self):
        update = self.message(text="/improve\nMake the calendar date numbers larger.")
        self.bot.handle_update(update)
        self.bot.handle_update(update)
        row = self.store.db.execute("SELECT * FROM improvements").fetchone()
        self.assertEqual(row["message"], "Make the calendar date numbers larger.")
        self.assertEqual(row["status"], "queued")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM improvements").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM requests").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM overrides").fetchone()[0], 0)

    def test_improvement_is_restricted_to_operator(self):
        self.bot.handle_update(self.message(actor=99, text="/improve Change the app."))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM improvements").fetchone()[0], 0)


class ImageTests(Fixture):
    def test_default_calendar_fills_all_visible_dates_and_starts_at_current_week(self):
        start, end = default_calendar_range(self.plan.today())
        self.assertEqual(calendar_bounds(start, end), (date(2026, 11, 16), date(2027, 1, 3)))
        visible = [day for _, weeks in calendar_pages(start, end) for week in weeks for day in week]
        self.assertEqual(len(visible), len(set(visible)))
        with patch.object(self.plan, "assignment", wraps=self.plan.assignment) as assignment:
            render_calendar(self.plan, start, end)
        actual = {(call.args[0], call.args[1]) for call in assignment.call_args_list}
        expected = {(child, day) for child in self.config.children for day in visible if day >= self.config.anchor_date}
        self.assertEqual(actual, expected)
        self.assertIn(("c1", date(2026, 12, 31)), actual)

    def test_shared_days_draw_only_parent_and_split_days_draw_children(self):
        from PIL import ImageDraw
        original, labels = ImageDraw.ImageDraw.text, []
        in_footer = False
        def record(draw, xy, text, *args, **kwargs):
            nonlocal in_footer
            if text.startswith("Scheduled nights"):
                in_footer = True
            if not in_footer:
                labels.append(text)
            return original(draw, xy, text, *args, **kwargs)
        with patch.object(ImageDraw.ImageDraw, "text", record):
            render_calendar(self.plan, self.start, self.start)
        self.assertFalse(any("both children" in text.casefold() for text in labels))
        self.assertFalse(any("Older" in text or "Younger" in text for text in labels))
        request = self.draft()
        self.plan.confirm(42, request["id"])
        labels.clear()
        in_footer = False
        with patch.object(ImageDraw.ImageDraw, "text", record):
            render_calendar(self.plan, self.start, self.start)
        self.assertIn("Older", labels)
        self.assertIn("Younger", labels)

    def test_same_parent_is_grouped_and_split_day_has_two_groups(self):
        groups = assignments_for_day(self.plan, self.start)
        self.assertEqual(groups, [{"parent": "p1", "children": ["c1", "c2"]}])
        request = self.draft()
        self.plan.confirm(42, request["id"])
        self.assertEqual(len(assignments_for_day(self.plan, self.start)), 2)

    def test_cross_month_calendar_is_a_valid_png(self):
        from PIL import Image
        data = render_calendar(self.plan, self.start, date(2026, 12, 6))
        with Image.open(BytesIO(data)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.width, 1120)
            self.assertGreater(image.height, 1000)
            image.verify()

    def test_schedule_command_queues_photo_instead_of_text(self):
        bot = Bot(self.plan, SimpleNamespace())
        bot.handle_update({"update_id": 1, "message": {"from": {"id": 42},
                          "chat": {"id": 42, "type": "private"},
                          "text": "/schedule 2026-11-24 2026-11-25"}})
        jobs = [json.loads(row[0]) for row in self.store.db.execute("SELECT payload FROM outbox")]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["method"], "sendPhoto")
        caption = jobs[0]["payload"]["caption"]
        self.assertIn("Scheduled nights · Requested period\nNovember 24 through November 25, 2026\n"
                      "Older: First 2, Second 0\nYounger: First 2, Second 0", caption)
        self.assertIn("Scheduled nights · Since January 1\nJanuary 1 through November 25, 2026\n"
                      "Older: First 3, Second 0\nYounger: First 3, Second 0", caption)
        self.assertTrue(base64.b64decode(jobs[0]["payload"]["photo_base64"]).startswith(b"\x89PNG"))

    def test_photo_upload_contains_binary_png_and_caption(self):
        png = b"\x89PNG\r\n\x1a\nexample"
        def transport(request, timeout):
            self.assertIn("multipart/form-data", request.get_header("Content-type"))
            self.assertIn(png, request.data)
            self.assertIn(b'name="photo"; filename="schedule.png"', request.data)
            self.assertIn(b"Calendar caption", request.data)
            return BytesIO(b'{"ok":true,"result":{"message_id":1}}')
        with patch("pplan.telegram.urlopen", transport):
            result = TelegramClient("123:test").call("sendPhoto", {
                "chat_id": 42, "caption": "Calendar caption", "photo_base64": base64.b64encode(png).decode(),
            })
        self.assertEqual(result["message_id"], 1)


class AITests(Fixture):
    def test_explicit_parser_uses_configured_names_and_child_selection(self):
        result = parse_request("Assign 2026-11-23 to Second for Younger", self.config)
        self.assertEqual(result["children"], ["c2"])
        self.assertEqual(parse_request("Assign 2026-11-23 to First for both", self.config)["children"], ["c1", "c2"])

    def test_invalid_model_actions_and_unknown_children_are_rejected(self):
        result = {"action": "change", "start": "2026-11-23", "end": "2026-11-23", "parent": "p2", "children": ["c1"], "question": None}
        for change in ({"action": "confirm"}, {"children": ["other"]}, {"start": "tomorrow"}, {"tool": "run"}, {"parent": []}):
            with self.assertRaises(PlanError):
                validate_result(result | change, self.config.parents, self.config.children)

    def test_cli_uses_subscription_without_inheriting_api_keys_or_tools(self):
        result = {"action": "change", "start": "2026-11-23", "end": "2026-11-23", "parent": "p2", "children": ["c1"], "question": None}
        def runner(args, **kwargs):
            self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
            self.assertNotIn("TELEGRAM_BOT_TOKEN", kwargs["env"])
            self.assertIn('forced_login_method="chatgpt"', args)
            self.assertIn("features.shell_tool=false", args)
            self.assertIn("features.apps=false", args)
            self.assertEqual(args[-1], "-")
            Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps(result))
            return SimpleNamespace(returncode=0)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "never-forward", "TELEGRAM_BOT_TOKEN": "never-forward"}):
            actual = CodexInterpreter(runner=runner).interpret("untrusted message", context_for(self.plan, []))
        self.assertEqual(actual, result)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM overrides").fetchone()[0], 0)


class FakeGoogle:
    def __init__(self):
        self.events, self.calls, self.fail = {}, [], False

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if self.fail:
            return 503, None
        if path == "/calendars":
            return 200, {"id": "test@example.com"}
        event_id = body["id"]
        if method == "PUT" and event_id not in self.events:
            return 404, None
        self.events[event_id] = body
        return (201 if method == "POST" else 200), body


class CalendarTests(Fixture):
    def setUp(self):
        super().setUp()
        self.client = FakeGoogle()
        self.sync = CalendarSync(self.plan, self.client)
        self.sync.connect()

    def test_sync_is_idempotent_and_uses_after_school_times(self):
        self.assertEqual(self.sync.sync(horizon=2), 4)
        self.assertEqual(self.sync.sync(horizon=2), 0)
        event = self.sync.event("c1", self.start)
        self.assertEqual(event["start"]["dateTime"], "2026-11-23T15:00:00-08:00")
        self.assertEqual(event["end"]["dateTime"], "2026-11-24T15:00:00-08:00")

    def test_confirmed_change_updates_same_event_only_for_selected_child(self):
        self.sync.sync(horizon=2)
        before = self.sync.event("c1", self.start)["id"]
        request = self.draft()
        self.plan.confirm(42, request["id"])
        self.assertEqual(self.sync.sync(horizon=2), 1)
        self.assertEqual(self.sync.event("c1", self.start)["id"], before)
        self.assertEqual(len(self.client.events), 4)

    def test_calendar_failure_preserves_confirmation_and_retries(self):
        request = self.draft()
        self.plan.confirm(42, request["id"])
        self.client.fail = True
        with self.assertRaises(PlanError):
            self.sync.sync(horizon=1)
        self.assertEqual(self.plan.request(request["id"])["status"], "confirmed")
        self.client.fail = False
        self.assertEqual(self.sync.sync(horizon=1), 2)

    def test_changes_beyond_horizon_are_included(self):
        request = self.draft(day=self.start + timedelta(days=100))
        self.plan.confirm(42, request["id"])
        self.assertEqual(self.sync.sync(horizon=1), 3)


if __name__ == "__main__":
    unittest.main()
