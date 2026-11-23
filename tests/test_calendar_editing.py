from dataclasses import replace
from datetime import date, timedelta
from io import BytesIO
import json
from types import SimpleNamespace
from unittest.mock import patch

from pplan.ai import validate_result
from pplan.calendar_image import COLORS, calendar_pages, day_segments, image_layout, render_calendar
from pplan.core import Plan, PlanError
from pplan.parser import parse_preview_overwrite, parse_request
from pplan.telegram import Bot
from tests.test_actions import assign, cycle
from tests.test_plan import Fixture


def cell_position(pages, target):
    for panel_x, y, _, weeks in image_layout(pages)["panels"]:
        for row, week in enumerate(weeks):
            if target in week:
                return panel_x + week.index(target) * 148, y + 88 + row * 98
    raise AssertionError("Date not rendered")


class CalendarEditingTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config = replace(self.config, school_handover_times={str(i): "17:00" for i in range(5)},
                              non_school_handover_time="17:00")
        self.plan = Plan(self.config, self.store, lambda: self.now)

    def test_long_calendar_uses_columns_and_never_exceeds_two_by_three(self):
        from PIL import Image
        for count in range(1, 25):
            month_index = 2027 * 12 + count - 1
            end = date(month_index // 12, month_index % 12 + 1, 1)
            start = max(date(2027, 1, 1), end - timedelta(days=699))
            pages = calendar_pages(start, end)
            for header in (178, 216):
                layout = image_layout(pages, header)
                self.assertLessEqual(layout["height"] * 2, layout["width"] * 3)
                self.assertLess(layout["width"] + layout["height"], 10000)
                panels = layout["panels"]
                self.assertEqual([month for _, _, month, _ in panels], [month for month, _ in pages])
                coordinates = [(x, y) for x, y, _, _ in panels]
                self.assertEqual(coordinates, sorted(coordinates))
        start = date(2027, 1, 1)
        end = start + timedelta(days=699)
        with self.assertRaisesRegex(PlanError, "700 days"):
            calendar_pages(start, end + timedelta(days=1))
        self.assertGreater(image_layout(calendar_pages(start, end))["columns"], 1)
        png = render_calendar(self.plan, start, end)
        self.assertLess(len(png), 10_000_000)
        with Image.open(BytesIO(png)) as image:
            self.assertLessEqual(image.height * 2, image.width * 3)
            self.assertLessEqual(image.width + image.height, 10000)
            image.verify()

    def test_transition_has_equal_color_halves_and_no_parent_labels(self):
        from PIL import Image, ImageColor, ImageDraw
        start, end, target = date(2026, 11, 23), date(2026, 11, 30), date(2026, 11, 30)
        x, y = cell_position(calendar_pages(start, end), target)
        labels, original = [], ImageDraw.ImageDraw.text
        def record(draw, xy, text, *args, **kwargs):
            if x <= xy[0] < x + 148 and y <= xy[1] < y + 91:
                labels.append(text)
            return original(draw, xy, text, *args, **kwargs)
        with patch.object(ImageDraw.ImageDraw, "text", record):
            data = render_calendar(self.plan, start, end)
        self.assertEqual(labels, ["30"])
        with Image.open(BytesIO(data)) as image:
            self.assertEqual(image.getpixel((x + 20, y + 55)), ImageColor.getrgb(COLORS[0][1]))
            self.assertEqual(image.getpixel((x + 120, y + 55)), ImageColor.getrgb(COLORS[1][1]))

    def test_children_remain_distinct_when_only_morning_assignments_differ(self):
        request = self.plan.draft(42, date(2026, 11, 29), date(2026, 11, 29), "p2", ["c1"])
        self.plan.confirm(42, request["id"])
        groups = day_segments(self.plan, date(2026, 11, 30))
        self.assertEqual(groups, [{"before": "p2", "after": "p2", "children": ["c1"]},
                                  {"before": "p1", "after": "p2", "children": ["c2"]}])

    def test_night_totals_exclude_padding_and_use_both_requested_ranges(self):
        from PIL import ImageDraw

        january_1 = date(2027, 1, 1)
        december_31 = date(2026, 12, 31)
        january_3 = date(2027, 1, 3)
        request = self.plan.draft(42, january_3, january_3, "p1", ["c1"])
        self.plan.confirm(42, request["id"])
        totals = self.plan.overnight_totals(january_1, january_3)
        self.assertEqual(totals, {"c1": {"p1": 1, "p2": 2}, "c2": {"p1": 0, "p2": 3}})
        self.assertEqual(self.plan.overnight_totals(december_31, january_3),
                         {"c1": {"p1": 1, "p2": 3}, "c2": {"p1": 0, "p2": 4}})
        self.assertEqual(self.plan.overnight_totals(date(2026, 11, 1), date(2026, 11, 23)),
                         {"c1": {"p1": 1, "p2": 0}, "c2": {"p1": 1, "p2": 0}})
        with patch.object(ImageDraw.ImageDraw, "text", autospec=True) as draw_text:
            render_calendar(self.plan, december_31, january_3)
        labels = [call.args[2] for call in draw_text.call_args_list]
        requested = labels.index("Scheduled nights · Requested period")
        year = labels.index("Scheduled nights · Since January 1")
        self.assertLess(requested, year)
        self.assertEqual(labels[requested + 1], "December 31, 2026 through January 3, 2027")
        self.assertEqual(labels[year + 1], "January 1 through January 3, 2027")
        self.assertIn("4 nights", labels[requested:year])
        self.assertNotIn("4 nights", labels[year:])
        self.assertIn("2 nights", labels[year:])

    def test_overwrite_pins_matching_day_and_survives_new_cycle(self):
        action = dict(assign("2026-11-24", "2026-11-24", "p1", ["c1"]), type="overwrite")
        request = self.plan.draft_actions(42, [action])
        self.plan.confirm(42, request["id"])
        rotation = self.plan.draft_actions(42, [cycle("2026-11-23", "p2")])
        self.plan.confirm(42, rotation["id"])
        self.assertEqual(self.plan.assignment("c1", date(2026, 11, 24))["parent"], "p1")
        self.assertEqual(self.plan.assignment("c2", date(2026, 11, 24))["parent"], "p2")

    def test_overwrite_replaces_existing_exception_only_for_selected_days_and_child(self):
        old = self.plan.draft(42, date(2026, 11, 24), date(2026, 11, 25), "p2", ["c1"])
        self.plan.confirm(42, old["id"])
        new = self.plan.draft_actions(42, [dict(assign("2026-11-24", "2026-11-24", "p1", ["c1"]), type="overwrite")])
        self.assertEqual(self.plan.assignment("c1", date(2026, 11, 24))["parent"], "p2")
        self.plan.confirm(42, new["id"])
        self.assertEqual(self.plan.assignment("c1", date(2026, 11, 24))["parent"], "p1")
        self.assertEqual(self.plan.assignment("c1", date(2026, 11, 25))["revision"], old["id"])
        self.assertEqual(self.plan.assignment("c2", date(2026, 11, 24))["revision"], "base")

    def test_copied_six_periods_keep_the_last_day_before_each_until(self):
        periods = [("p2", "2027-03-05", "2027-03-08"), ("p1", "2027-03-12", "2027-03-15"),
                   ("p1", "2027-04-05", "2027-04-16"), ("p2", "2027-04-16", "2027-04-23"),
                   ("p1", "2027-04-23", "2027-04-26"), ("p2", "2027-04-26", "2027-05-03")]
        blocks = [f"{i}. Older, Younger with {self.config.parents[parent]}\nFrom: {start} 17:00\nUntil: {until} 17:00"
                  for i, (parent, start, until) in enumerate(periods, 1)]
        text = "\n\n".join(blocks)
        escaped = text.replace(".", "\\.").replace("\n", "\\\n")
        actions = parse_preview_overwrite(escaped, self.config)
        self.assertEqual(actions[4]["end"], "2027-04-25")
        self.assertEqual(len(actions), 6)
        request = self.plan.draft_actions(42, actions)
        self.plan.confirm(42, request["id"])
        for parent, start, until in periods:
            for child in self.config.children:
                for offset in range((date.fromisoformat(until) - date.fromisoformat(start)).days):
                    day = date.fromisoformat(start) + timedelta(days=offset)
                    self.assertEqual(self.plan.assignment(child, day)["parent"], parent, (child, day))

    def test_pasted_overwrite_rejects_partial_day_or_unparsed_extra_action(self):
        text = "1. Older with Second\nFrom: 2027-04-23 17:00\nUntil: 2027-04-26 17:00"
        with self.assertRaises(PlanError):
            parse_preview_overwrite(text.replace("26 17:00", "26 12:00"), self.config)
        with self.assertRaises(PlanError):
            parse_preview_overwrite(text + "\nAlso change the weekly rotation", self.config)

    def test_overwrite_command_and_model_action_queue_confirmation(self):
        parsed = parse_request("Overwrite 2026-11-24 to First for Older", self.config)
        self.assertEqual(validate_result(parsed, self.config.parents, self.config.children), parsed)
        self.assertEqual(parsed["actions"][0]["type"], "overwrite")
        bot = Bot(self.plan, SimpleNamespace())
        bot.handle_update({"update_id": 1, "message": {"from": {"id": 42}, "chat": {"id": 42, "type": "private"},
                          "text": "/overwrite 2026-11-24 to First for Older"}})
        self.assertEqual(self.store.db.execute("SELECT status FROM requests").fetchone()[0], "draft")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM overrides").fetchone()[0], 0)
        job = json.loads(self.store.db.execute("SELECT payload FROM outbox").fetchone()[0])
        self.assertIn("Overwrites", job["payload"]["text"])
