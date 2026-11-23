from datetime import date
from io import BytesIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

from pplan.calendar_image import SPECIAL_COLOR, calendar_pages, image_layout, render_calendar
from pplan.core import Plan, PlanError, Store
from pplan.telegram import Bot
from tests.test_plan import Fixture


class SpecialEventTests(Fixture):
    def test_range_is_one_event_and_does_not_change_parenting_assignments(self):
        before = self.plan.schedule(self.start, date(2026, 12, 6))
        event = self.plan.add_special(42, date(2026, 11, 24), date(2026, 12, 4), "Long event")
        same = self.plan.add_special(42, date(2026, 11, 24), date(2026, 12, 4), "Long event")
        self.assertEqual(event["id"], same["id"])
        self.assertEqual(len(self.plan.special_events()), 1)
        self.assertEqual(self.plan.schedule(self.start, date(2026, 12, 6)), before)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM requests").fetchone()[0], 0)

    def test_multi_day_frame_connects_adjacent_date_cells_and_stops_at_end(self):
        from PIL import Image, ImageColor
        self.plan.add_special(42, date(2026, 11, 24), date(2026, 11, 26))
        pages = calendar_pages(self.start, date(2026, 11, 29))
        panel_x, panel_y, _, _ = image_layout(pages)["panels"][0]
        top = panel_y + 88
        with Image.open(BytesIO(render_calendar(self.plan, self.start, date(2026, 11, 29)))) as image:
            purple = ImageColor.getrgb(SPECIAL_COLOR)
            self.assertEqual(image.getpixel((panel_x + 2 * 148 - 2, top - 1)), purple)
            self.assertNotEqual(image.getpixel((panel_x + 4 * 148 + 70, top - 1)), purple)

    def test_special_command_is_deduplicated_and_returns_calendar(self):
        bot = Bot(self.plan, SimpleNamespace())
        update = {"update_id": 1, "message": {"from": {"id": 42}, "chat": {"id": 42, "type": "private"},
                                             "text": "/special 2027-03-05 2027-03-08 Vacation"}}
        bot.handle_update(update)
        bot.handle_update(update)
        events = self.plan.special_events()
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["start"], events[0]["end"], events[0]["title"]), ("2027-03-05", "2027-03-08", "Vacation"))
        jobs = [json.loads(row[0]) for row in self.store.db.execute("SELECT payload FROM outbox")]
        self.assertEqual([job["method"] for job in jobs], ["sendMessage", "sendPhoto"])

    def test_single_day_removal_and_authorization(self):
        event = self.plan.add_special(42, self.start, self.start, "One day")
        with self.assertRaises(PlanError):
            self.plan.add_special(99, self.start, self.start)
        with self.assertRaises(PlanError):
            self.plan.remove_special(99, event["id"])
        self.assertEqual(len(self.plan.special_events()), 1)
        self.plan.remove_special(42, event["id"])
        self.assertEqual(self.plan.special_events(), [])
        self.assertIsNotNone(self.store.db.execute("SELECT removed FROM special_events").fetchone()[0])

    def test_events_survive_database_reopen(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "plan.sqlite3"
            store = Store(path)
            try:
                Plan(self.config, store, lambda: self.now).add_special(42, self.start, date(2026, 11, 30), "Trip")
            finally:
                store.close()
            reopened = Store(path)
            try:
                events = Plan(self.config, reopened, lambda: self.now).special_events()
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["title"], "Trip")
            finally:
                reopened.close()

    def test_invalid_range_cannot_create_event(self):
        with self.assertRaises(PlanError):
            self.plan.add_special(42, date(2027, 1, 2), date(2027, 1, 1))
        self.assertEqual(self.plan.special_events(), [])
