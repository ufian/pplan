from datetime import date
import json
from pathlib import Path
import tempfile

from pplan.calendar import CalendarSync
from pplan.colors import BY_ID, DEFAULT_SPECIAL, PALETTE, align_special_colors, google_color
from pplan.core import Plan, Store
from pplan.history import prepare_restore, confirm_restore
from pplan.miniapp import calendar_data, update_event
from tests.test_plan import Fixture, FakeGoogle


class GoogleColorTests(Fixture):
    def setUp(self):
        super().setUp()
        self.client = FakeGoogle()
        self.sync = CalendarSync(self.plan, self.client)
        self.sync.connect()

    def specials(self):
        return {key: event for key,event in self.client.events.items() if event.get("extendedProperties",{}).get("private",{}).get("format") == "specials-v1"}

    def test_parents_and_app_share_google_palette(self):
        self.sync.sync(horizon=14)
        self.assertEqual({event["colorId"] for event in self.client.events.values()}, {"10","6"})
        data = calendar_data(self.plan,"2026-11")
        self.assertEqual(data["parent_colors"], {"p1":BY_ID["10"]["color"],"p2":BY_ID["6"]["color"]})
        self.assertEqual(data["palette"], PALETTE)
        for item in PALETTE:
            self.assertEqual(google_color(item["color"]), item)
        self.assertEqual(google_color("#7845a6")["id"], "3")
        self.assertEqual(google_color("#2563eb")["id"], "9")
        self.assertEqual(google_color("#123456")["color"] in {i["color"] for i in PALETTE}, True)

    def test_special_full_inclusive_range_color_description_and_edit_stable_identity(self):
        event = self.plan.add_special(42,date(2026,12,31),date(2027,1,2),"Trip")
        edited = self.plan.update_special(42,event["id"],date(2026,12,31),date(2027,1,2),"Trip","First line\nSecond line","#2563eb",expected_revision=1)
        self.sync.sync(horizon=2)
        key, projected = next(iter(self.specials().items()))
        self.assertEqual((projected["summary"],projected["description"],projected["colorId"]),("Trip","First line\nSecond line","9"))
        self.assertEqual(projected["start"],{"date":"2026-12-31"})
        self.assertEqual(projected["end"],{"date":"2027-01-03"})
        self.assertEqual(self.sync.sync(horizon=2),0)
        self.plan.update_special(42,event["id"],date(2027,1,1),date(2027,1,3),"Changed","Details",BY_ID["5"]["color"],expected_revision=edited["revision"])
        self.assertEqual(self.store.get("calendar_dirty"),"1")
        self.sync.sync(horizon=2)
        self.assertEqual(set(self.specials()),{key})
        self.assertEqual(self.specials()[key]["colorId"],"5")
        self.assertEqual(self.specials()[key]["end"],{"date":"2027-01-04"})

    def test_delete_and_restore_special_uses_fresh_id_and_preserves_foreign_events(self):
        event = self.plan.add_special(42,self.start,self.start,"Fixture")
        version = self.store.db.execute("SELECT max(id) FROM plan_versions").fetchone()[0]
        self.sync.sync(horizon=2)
        old = next(iter(self.specials()))
        foreign="f"*32
        self.client.events[foreign] = {"id":foreign,"extendedProperties":{"private":{"pplan":"another-plan","format":"specials-v1","special_id":"1"}}}
        malformed="e"*32
        self.client.events[malformed] = {"id":malformed,"extendedProperties":{"private":{"pplan":self.store.get("plan_id"),"format":"specials-v1"}}}
        self.plan.remove_special(42,event["id"])
        self.sync.sync(horizon=2)
        self.assertNotIn(old,self.client.events)
        confirm_restore(self.plan,42,prepare_restore(self.plan,42,version)["id"])
        self.sync.sync(horizon=2)
        own = {k:v for k,v in self.specials().items() if k not in {foreign,malformed}}
        self.assertEqual(len(own),1)
        self.assertNotIn(old,own)
        self.assertIn(foreign,self.client.events)
        self.assertIn(malformed,self.client.events)
        self.assertEqual(self.sync.sync(horizon=2),0)

    def test_lost_upload_response_reuses_reserved_special_identity(self):
        self.plan.add_special(42,self.start,self.start,"Fixture")
        request=self.client.request
        def lost(method,path,body=None):
            status,result=request(method,path,body)
            if method=="POST" and body.get("extendedProperties",{}).get("private",{}).get("format")=="specials-v1":
                raise TimeoutError("Fixture lost response")
            return status,result
        self.client.request=lost
        with self.assertRaises(TimeoutError):self.sync.sync(horizon=2)
        keys=set(self.specials())
        self.client.request=request
        self.sync.sync(horizon=2)
        self.assertEqual(set(self.specials()),keys)
        self.assertEqual(len(keys),1)

    def test_alignment_preserves_schedule_previews_and_old_history(self):
        event=self.plan.add_special(42,self.start,self.start,"Legacy")
        self.store.db.execute("UPDATE special_events SET color='#7845a6' WHERE id=?",(event["id"],))
        with self.store.transaction():
            self.plan._record_version(42,"legacy_fixture")
        version=self.store.db.execute("SELECT max(id) FROM plan_versions").fetchone()[0]
        old=self.store.db.execute("SELECT state FROM plan_versions WHERE id=?",(version,)).fetchone()[0]
        pending=self.draft()
        schedule=self.plan.schedule(self.start,date(2026,12,6))
        self.assertEqual(align_special_colors(self.plan,42),1)
        self.assertEqual(align_special_colors(self.plan,42),0)
        self.assertEqual(self.plan.special_events()[0]["color"],DEFAULT_SPECIAL)
        self.assertEqual(self.plan.schedule(self.start,date(2026,12,6)),schedule)
        self.assertEqual(self.store.db.execute("SELECT status FROM requests WHERE id=?",(pending["id"],)).fetchone()[0],"draft")
        self.assertEqual(self.store.db.execute("SELECT state FROM plan_versions WHERE id=?",(version,)).fetchone()[0],old)
        self.assertEqual(json.loads(old)["special_events"][0]["color"],"#7845a6")
        confirm_restore(self.plan,42,prepare_restore(self.plan,42,version)["id"])
        self.assertEqual(self.plan.special_events()[0]["color"],DEFAULT_SPECIAL)

    def test_miniapp_edit_marks_projection_dirty_through_restricted_trigger(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"fixture.sqlite3"
            store=Store(path);plan=Plan(self.config,store,lambda:self.now)
            event=plan.add_special(42,self.start,self.start,"Fixture")
            store.set("calendar_dirty","0");store.close()
            body={key:event[key] for key in ("id","start","end","title","description","color","revision")}
            body["color"]=BY_ID["11"]["color"]
            update_event(self.config,path,body)
            store=Store(path)
            try:self.assertEqual(store.get("calendar_dirty"),"1")
            finally:store.close()
