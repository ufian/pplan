from datetime import date
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile

from pplan.calendar_image import calendar_pages, image_layout, render_calendar
from pplan.core import Plan, PlanError, SpecialEventConflict, Store
from pplan.miniapp import event_write_authorizer, update_event
from tests.test_plan import Fixture


class SpecialEditingTests(Fixture):
    def edit(self, event, **changes):
        values = {"start": date.fromisoformat(event["start"]), "end": date.fromisoformat(event["end"]),
                  "title": event["title"], "description": event["description"], "color": event["color"]}
        values.update(changes)
        return self.plan.update_special(42, event["id"], **values, expected_revision=event["revision"])

    def test_edit_metadata_without_changing_schedule_or_pending_confirmation(self):
        event = self.plan.add_special(42, self.start, self.start, "Original")
        pending = self.draft()
        before = self.plan.schedule(self.start, date(2026,12,6))
        edited = self.edit(event, start=date(2026,12,1), end=date(2026,12,4), title="Trip", description="First line\nSecond line", color="#2563EB")
        self.assertEqual((edited["id"], edited["created"]), (event["id"], event["created"]))
        self.assertEqual((edited["color"],edited["description"],edited["revision"]),("#2563eb","First line\nSecond line",2))
        self.assertEqual(self.plan.schedule(self.start,date(2026,12,6)),before)
        self.assertEqual(self.plan.special_events(self.start,self.start),[])
        self.assertEqual(self.store.db.execute("SELECT status FROM requests WHERE id=?",(pending["id"],)).fetchone()[0],"draft")
        self.plan.confirm(42,pending["id"])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM audit WHERE action='special_updated'").fetchone()[0],1)

    def test_conflicting_edits_fail_but_retrying_identical_save_is_idempotent(self):
        event=self.plan.add_special(42,self.start,self.start,"One")
        first=self.edit(event,title="Two",color="#2563eb")
        retry=self.edit(event,title="Two",color="#2563eb")
        self.assertEqual(first,retry)
        with self.assertRaises(SpecialEventConflict):
            self.edit(event,title="Three")
        self.assertEqual(self.plan.special_events()[0]["title"],"Two")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM audit WHERE action='special_updated'").fetchone()[0],1)

    def test_invalid_metadata_and_removed_or_foreign_events_are_rejected(self):
        event=self.plan.add_special(42,self.start,self.start,"Original")
        for changes in ({"end":date(2026,11,22)},{"end":date(2027,11,24)},{"title":"x"*121},
                        {"description":"x"*2001},{"description":"bad\x00value"},{"color":"red"},
                        {"color":"#12345g"},{"start":date(1900,1,1)},{"title":"bad\nvalue"}):
            with self.subTest(changes=changes),self.assertRaises(PlanError):self.edit(event,**changes)
        with self.assertRaises(PlanError):
            self.plan.update_special(99,event["id"],self.start,self.start,"Other","","#2563eb",expected_revision=1)
        self.assertEqual(self.plan.special_events()[0]["revision"],1)
        self.plan.remove_special(42,event["id"])
        with self.assertRaises(PlanError):self.edit(event,title="Restore")

    def test_legacy_migration_preserves_events_pending_requests_and_confirmations(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"legacy.sqlite3"
            store=Store(path);plan=Plan(self.config,store,lambda:self.now)
            pending=plan.draft(42,self.start,self.start,"p2",["c1"])
            store.db.executescript("""
                DROP TABLE special_events;
                CREATE TABLE special_events(id INTEGER PRIMARY KEY, actor INTEGER NOT NULL, start TEXT NOT NULL,
                  end TEXT NOT NULL, title TEXT NOT NULL, created TEXT NOT NULL, removed TEXT);
                INSERT INTO special_events VALUES(7,42,'2026-11-23','2026-11-25','Legacy','2026-11-22',NULL);
                INSERT INTO special_events VALUES(8,42,'2026-11-23','2026-11-23','Removed','2026-11-22','2026-11-22');
            """);store.close()
            for attempt in range(2):
                migrated=Store(path)
                try:
                    current=Plan(self.config,migrated,lambda:self.now)
                    event=current.special_events()[0]
                    self.assertEqual((event["id"],event["title"],event["color"],event["description"],event["revision"]),(7,"Legacy","#7845a6","",1))
                    self.assertEqual(migrated.db.execute("SELECT removed FROM special_events WHERE id=8").fetchone()[0],"2026-11-22")
                    if attempt==0:self.assertEqual(migrated.db.execute("SELECT status FROM requests WHERE id=?",(pending["id"],)).fetchone()[0],"draft")
                    else:current.confirm(42,pending["id"])
                finally:migrated.close()

    def test_image_uses_edited_color(self):
        from PIL import Image, ImageColor
        event=self.plan.add_special(42,date(2026,11,24),date(2026,11,26))
        self.edit(event,color="#2563eb")
        pages=calendar_pages(self.start,date(2026,11,29))
        x,y,_,_=image_layout(pages)["panels"][0]
        with Image.open(BytesIO(render_calendar(self.plan,self.start,date(2026,11,29)))) as image:
            self.assertEqual(image.getpixel((x+2*148-2,y+88-1)),ImageColor.getrgb("#2563eb"))

    def test_editor_connection_cannot_write_assignments_rules_settings_or_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"plan.sqlite3";store=Store(path);plan=Plan(self.config,store,lambda:self.now)
            event=plan.add_special(42,self.start,self.start,"Fixture");store.close()
            body={key:event[key] for key in ("id","start","end","title","description","color","revision")}
            body.update(title="Updated",color="#2563eb")
            self.assertEqual(update_event(self.config,path,body)["title"],"Updated")
            restricted=Store(path,initialize=False);restricted.db.set_authorizer(event_write_authorizer)
            try:
                for sql in ("DELETE FROM special_events", "UPDATE settings SET value='bad'", "DELETE FROM audit",
                            "UPDATE overrides SET parent='p2'", "UPDATE cycles SET cycle='[]'", "UPDATE requests SET status='confirmed'",
                            "INSERT INTO outbox(payload) VALUES('{}')", "ALTER TABLE special_events ADD COLUMN bad TEXT"):
                    with self.subTest(sql=sql),self.assertRaises(sqlite3.DatabaseError):restricted.db.execute(sql)
            finally:restricted.close()
