from dataclasses import replace
from datetime import date, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from pplan.ai import context_for, validate_result
from pplan.core import Plan, PlanError, Store
from pplan.telegram import Bot
from tests.test_plan import Fixture, FakeGoogle
from pplan.calendar import CalendarSync


def assign(start, end, parent, children=None):
    return {"type": "assign", "start": start, "end": end, "parent": parent,
            "children": children or ["c1", "c2"], "cycle": []}


def cycle(start, first="p1", children=None):
    other = "p2" if first == "p1" else "p1"
    return {"type": "set_cycle", "start": start, "end": None, "parent": None,
            "children": children or ["c1", "c2"], "cycle": [first] * 7 + [other] * 7}


def vacation_actions():
    return [assign("2026-12-18", "2027-01-08", "p2"),
            assign("2027-01-09", "2027-01-15", "p1"),
            assign("2027-01-16", "2027-01-24", "p2"),
            cycle("2027-01-25")]


def response(actions):
    return {"action": "changes", "actions": actions, "start": None, "end": None,
            "parent": None, "children": [], "question": None}


class ActionTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config = replace(self.config, school_handover_times={str(i): "17:00" for i in range(5)},
                              non_school_handover_time="17:00")
        self.plan = Plan(self.config, self.store, lambda: self.now)

    def test_vacation_and_return_to_weekly_rotation_are_one_confirmation(self):
        request = self.plan.draft_actions(42, vacation_actions())
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM overrides").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM cycles").fetchone()[0], 0)
        self.plan.confirm(42, request["id"])
        expected = {"2026-12-18": "p2", "2027-01-08": "p2", "2027-01-09": "p1",
                    "2027-01-15": "p1", "2027-01-16": "p2", "2027-01-24": "p2",
                    "2027-01-25": "p1", "2027-01-31": "p1", "2027-02-01": "p2", "2027-02-08": "p1"}
        for value, parent in expected.items():
            for child in self.config.children:
                self.assertEqual(self.plan.assignment(child, date.fromisoformat(value))["parent"], parent, value)
        future = date(2027, 1, 25) + timedelta(days=14 * 80)
        self.assertEqual(self.plan.assignment("c1", future)["parent"], "p1")

    def test_conflicting_assignment_boundaries_are_rejected_without_partial_draft(self):
        actions = vacation_actions()
        actions[0]["end"] = "2027-01-09"
        with self.assertRaisesRegex(PlanError, "overlap"):
            self.plan.draft_actions(42, actions)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM requests").fetchone()[0], 0)

    def test_failure_in_last_write_rolls_back_every_action(self):
        request = self.plan.draft_actions(42, vacation_actions())
        self.store.db.executescript("""
            CREATE TRIGGER simulate_failure BEFORE INSERT ON cycles WHEN NEW.child='c2'
            BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END;
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.plan.confirm(42, request["id"])
        self.assertEqual(self.plan.request(request["id"])["status"], "draft")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM overrides").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM cycles").fetchone()[0], 0)

    def test_new_cycle_can_apply_to_one_child(self):
        request = self.plan.draft_actions(42, [cycle("2027-01-25", children=["c1"])])
        self.plan.confirm(42, request["id"])
        self.assertEqual(self.plan.assignment("c1", date(2027, 1, 25))["parent"], "p1")
        self.assertEqual(self.plan.assignment("c2", date(2027, 1, 25))["parent"], "p2")

    def test_new_cycle_preserves_specific_date_exceptions(self):
        exception = self.plan.draft(42, date(2027, 1, 30), date(2027, 2, 1), "p1", ["c1"])
        self.plan.confirm(42, exception["id"])
        request = self.plan.draft_actions(42, [cycle("2027-01-25")])
        self.plan.confirm(42, request["id"])
        self.assertEqual(self.plan.assignment("c1", date(2027, 2, 1))["parent"], "p1")
        self.assertEqual(self.plan.assignment("c2", date(2027, 2, 1))["parent"], "p2")

    def test_new_cycle_replaces_later_rules_but_preserves_history(self):
        first = self.plan.draft_actions(42, [cycle("2027-02-01")])
        self.plan.confirm(42, first["id"])
        before = self.plan.assignment("c1", date(2027, 1, 24))
        second = self.plan.draft_actions(42, [cycle("2027-01-25")])
        self.plan.confirm(42, second["id"])
        self.assertEqual(self.plan.assignment("c1", date(2027, 1, 24)), before)
        self.assertEqual(self.plan.assignment("c1", date(2027, 2, 1))["parent"], "p2")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM cycles").fetchone()[0], 2)

    def test_two_future_cycles_can_be_confirmed_in_one_batch(self):
        request = self.plan.draft_actions(42, [cycle("2027-01-25"), cycle("2027-02-15", "p2")])
        self.plan.confirm(42, request["id"])
        self.assertEqual(self.plan.assignment("c1", date(2027, 2, 15))["parent"], "p2")
        self.assertEqual(self.plan.assignment("c1", date(2027, 2, 22))["parent"], "p1")

    def test_change_during_cycle_preview_invalidates_entire_batch(self):
        request = self.plan.draft_actions(42, vacation_actions())
        day = date(2027, 3, 1)
        original = self.plan.assignment("c1", day)["parent"]
        other = "p2" if original == "p1" else "p1"
        concurrent = self.plan.draft(42, day, day, other, ["c1"])
        self.plan.confirm(42, concurrent["id"])
        with self.assertRaisesRegex(PlanError, "schedule changed"):
            self.plan.confirm(42, request["id"])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM cycles").fetchone()[0], 0)

    def test_invalid_late_action_does_not_store_earlier_valid_actions(self):
        actions = vacation_actions()
        actions[-1]["cycle"] = ["unknown"]
        with self.assertRaises(PlanError):
            self.plan.draft_actions(42, actions)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM requests").fetchone()[0], 0)

    def test_different_dates_child_and_two_day_cycle_work_without_special_cases(self):
        actions = [assign("2027-02-03", "2027-02-05", "p1", ["c1"]),
                   assign("2027-02-06", "2027-02-10", "p2", ["c1"]),
                   dict(cycle("2027-02-11", children=["c1"]), cycle=["p1", "p1", "p2", "p2"])]
        other_before = self.plan.assignment("c2", date(2027, 2, 13))
        request = self.plan.draft_actions(42, actions)
        self.plan.confirm(42, request["id"])
        for value, expected in (("2027-02-05", "p1"), ("2027-02-10", "p2"),
                                ("2027-02-11", "p1"), ("2027-02-13", "p2"), ("2027-02-15", "p1")):
            self.assertEqual(self.plan.assignment("c1", date.fromisoformat(value))["parent"], expected)
        self.assertEqual(self.plan.assignment("c2", date(2027, 2, 13)), other_before)

    def test_ai_context_keeps_normal_rotation_during_long_vacation(self):
        request = self.plan.draft_actions(42, [assign("2026-11-23", "2026-12-23", "p2"),
                                               assign("2026-12-24", "2027-01-03", "p2")])
        self.plan.confirm(42, request["id"])
        context = context_for(self.plan, [])
        self.assertTrue(all(item["parent"] == "p2" for item in context["schedule"]))
        self.assertEqual(context["repeating_cycles"]["c1"], list(self.config.cycle))

    def test_model_action_list_is_validated_and_unknown_actions_are_rejected(self):
        valid = response(vacation_actions())
        self.assertEqual(validate_result(valid, self.config.parents, self.config.children), valid)
        for bad in ({"type": "execute"}, {"children": ["unknown"]}, {"parent": []}, {"start": "Friday"}):
            invalid = vacation_actions()
            invalid[0].update(bad)
            with self.assertRaises(PlanError):
                validate_result(response(invalid), self.config.parents, self.config.children)

    def test_bot_shows_all_actions_with_single_confirm_button(self):
        bot = Bot(self.plan, SimpleNamespace(), SimpleNamespace(interpret=lambda *args: response(vacation_actions())))
        bot.handle_update({"update_id": 1, "message": {"from": {"id": 42}, "chat": {"id": 42, "type": "private"},
                                                      "text": "Set vacation periods and resume weekly rotation."}})
        jobs = [json.loads(row[0]) for row in self.store.db.execute("SELECT payload FROM outbox")]
        self.assertEqual(len(jobs), 1)
        text = jobs[0]["payload"]["text"]
        self.assertIn("4 action(s)", text)
        self.assertIn("2027-01-09 17:00", text)
        self.assertIn("2027-01-25 17:00", text)
        self.assertIn("Repeat every 14 days", text)
        self.assertEqual(jobs[0]["payload"]["reply_markup"]["inline_keyboard"][0][0]["text"], "Confirm all")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM overrides").fetchone()[0], 0)

    def test_old_database_migrates_and_old_confirmation_still_works(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.sqlite3"
            old = sqlite3.connect(path)
            old.execute("CREATE TABLE requests (id TEXT PRIMARY KEY, actor INTEGER, start TEXT, end TEXT, parent TEXT, children TEXT, snapshot TEXT, status TEXT, created TEXT, expires TEXT)")
            snapshot = json.dumps({"c1:2026-11-23": {"parent": "p1", "revision": "base"}})
            old.execute("INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        ("legacy", 42, "2026-11-23", "2026-11-23", "p2", '["c1"]', snapshot, "draft",
                         self.now.isoformat(), (self.now + timedelta(hours=24)).isoformat()))
            old.commit()
            old.close()
            migrated = Store(path)
            try:
                plan = Plan(self.config, migrated, lambda: self.now)
                plan.confirm(42, "legacy")
                self.assertEqual(plan.assignment("c1", self.start)["parent"], "p2")
                self.assertEqual(plan.request("legacy")["status"], "confirmed")
            finally:
                migrated.close()


class CycleCalendarTests(Fixture):
    def setUp(self):
        super().setUp()
        self.client = FakeGoogle()
        self.sync = CalendarSync(self.plan, self.client)
        self.sync.connect()

    def test_future_cycle_projects_beyond_rolling_horizon(self):
        request = self.plan.draft_actions(42, [cycle("2027-01-25")])
        self.plan.confirm(42, request["id"])
        self.assertEqual(self.sync.sync(horizon=2), 8)

    def test_new_cycle_updates_already_cached_future_events(self):
        self.sync.sync(horizon=90)
        request = self.plan.draft_actions(42, [cycle("2027-01-25")])
        self.plan.confirm(42, request["id"])
        self.sync.sync(horizon=1)
        event_id = self.sync.event("c1", date(2027, 2, 1))["id"]
        self.assertIn("Second", self.client.events[event_id]["summary"])
