from dataclasses import replace
from datetime import date
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pplan.access import create_invite, parent_for_actor
from pplan.calendar import CalendarSync
from pplan.colors import own_color_data, update_own_color, plan_parent_palette
from pplan.core import Plan, PlanError, Store, SpecialEventConflict
from pplan.miniapp import color_write_authorizer, update_color, make_handler
from tests.test_miniapp import TOKEN, signed
from tests.test_plan import Fixture, FakeGoogle


class ParentColorTests(Fixture):
    def setUp(self):
        super().setUp()
        self.client=FakeGoogle();self.sync=CalendarSync(self.plan,self.client);self.sync.connect()
        self.store.db.execute("INSERT INTO family_members VALUES(?,?,?,NULL)",(99,"p2",self.now.isoformat()))

    def test_each_parent_changes_only_their_color_and_preserves_plan_and_previews(self):
        pending=self.draft();before=self.plan.schedule(self.start,date(2026,12,6))
        self.sync.sync(horizon=14);ids=set(self.client.events)
        versions=self.store.db.execute("SELECT count(*) FROM plan_versions").fetchone()[0]
        first=update_own_color(self.plan,42,"legacy:7",0)
        self.assertEqual(first["parent"],"p1")
        self.assertEqual(first["revision"],1)
        second=update_own_color(self.plan,99,"legacy:3",0)
        self.assertEqual(second["parent"],"p2")
        self.assertEqual(second["parent_colors"],{"p1":"#46d6db","p2":"#dbadff"})
        self.assertEqual(update_own_color(self.plan,42,"legacy:7",0)["revision"],1)
        with self.assertRaises(SpecialEventConflict):update_own_color(self.plan,42,"legacy:1",0)
        with self.assertRaises(PlanError):update_own_color(self.plan,42,"legacy:3",1)
        with self.assertRaises(PlanError):update_own_color(self.plan,42,"label:unknown",1)
        with self.assertRaises(PlanError):update_own_color(self.plan,55,"legacy:1",0)
        with self.assertRaises(PlanError):update_own_color(self.plan,42,"legacy:1",True)
        self.sync.sync(horizon=14)
        self.assertEqual(set(self.client.events),ids)
        self.assertEqual({e["colorId"] for e in self.client.events.values()},{"7","3"})
        self.assertEqual(self.sync.sync(horizon=14),0)
        self.assertEqual(self.plan.schedule(self.start,date(2026,12,6)),before)
        self.assertEqual(self.store.db.execute("SELECT status FROM requests WHERE id=?",(pending["id"],)).fetchone()[0],"draft")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM plan_versions").fetchone()[0],versions)
        self.store.db.execute("UPDATE family_members SET revoked=? WHERE telegram_id=99",(self.now.isoformat(),))
        with self.assertRaises(PlanError):update_own_color(self.plan,99,"legacy:2",1)

    def test_cached_google_labels_are_exact_and_bound_to_connected_calendar(self):
        label="33333333-3333-4333-8333-333333333333"
        option={"key":"label:"+label,"label_id":label,"color":"#b39ddb","name":"Lavender"}
        self.store.set("google_parent_palette",json.dumps({"calendar_id":self.store.get("google_calendar_id"),"colors":[option]}))
        value=update_own_color(self.plan,42,option["key"],0)
        self.assertEqual(value["options"],[option])
        self.assertEqual(plan_parent_palette(self.plan)["p1"],{"label_id":label,"color":"#b39ddb"})
        self.sync.sync(horizon=2)
        event=next(iter(self.client.events.values()))
        self.assertEqual(event["eventLabelId"],label)
        self.assertTrue(any(path.endswith("?eventLabelVersion=1") for method,path,body in self.client.calls if method=="POST"))
        self.store.set("google_calendar_id","other")
        with self.assertRaises(PlanError):own_color_data(self.plan,42)

    def test_color_change_during_upload_keeps_dirty_until_all_periods_match(self):
        original=self.client.request;changed=False
        def request(method,path,body=None):
            nonlocal changed
            result=original(method,path,body)
            if method=="POST" and not changed:
                changed=True;update_own_color(self.plan,42,"legacy:7",0)
            return result
        self.client.request=request;self.sync.sync(horizon=14)
        self.assertEqual(self.store.get("calendar_dirty"),"1")
        self.client.request=original;self.sync.sync(horizon=14)
        self.assertEqual(self.store.get("calendar_dirty"),"0")
        self.assertEqual({e["colorId"] for e in self.client.events.values()},{"7","6"})

    def test_owner_identity_setting_preserves_fingerprint_and_invites_other_parent(self):
        cfg=replace(self.config,operator_parent_id=None)
        plan=Plan(cfg,self.store,lambda:self.now)
        self.store.db.execute("DELETE FROM family_members")
        fingerprint=cfg.fingerprint()
        self.store.set("operator_parent_id","p1")
        self.assertEqual(parent_for_actor(plan,42),"p1")
        self.assertEqual(create_invite(plan,42)["parent_id"],"p2")
        with self.assertRaises(PlanError):create_invite(plan,42,"p1")
        self.assertEqual(cfg.fingerprint(),fingerprint)

    def test_api_authorization_personal_scope_and_restricted_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"fixture.sqlite3";store=Store(path);plan=Plan(self.config,store,lambda:self.now)
            store.set("google_calendar_id","fixture")
            store.db.execute("INSERT INTO family_members VALUES(?,?,?,NULL)",(99,"p2",self.now.isoformat()));store.set("calendar_dirty","0");store.close()
            server=ThreadingHTTPServer(("127.0.0.1",0),make_handler(self.config,path,TOKEN))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base=f"http://127.0.0.1:{server.server_port}"
            def call(route,body,actor=42,raw=None):
                req=Request(base+route,data=json.dumps(body).encode(),headers={"Content-Type":"application/json","Authorization":"tma "+(signed(actor) if raw is None else raw)})
                try:
                    with urlopen(req) as response:return response.status,json.load(response)
                except HTTPError as exc:return exc.code,json.load(exc)
            try:
                self.assertEqual(call("/api/colors",{})[1]["parent"],"p1")
                self.assertEqual(call("/api/colors",{},99)[1]["parent"],"p2")
                self.assertEqual(call("/api/colors",{},55)[0],401)
                self.assertEqual(call("/api/colors/update",{"choice":"legacy:7","revision":0},raw="invalid")[0],401)
                self.assertEqual(call("/api/colors/update",{"choice":"legacy:7","revision":0,"parent":"p2"})[0],400)
                self.assertEqual(call("/api/colors/update",{"choice":"legacy:7","revision":0})[0],200)
                self.assertEqual(call("/api/colors/update",{"choice":"legacy:1","revision":0})[0],409)
                self.assertEqual(call("/api/colors/update",{"choice":"legacy:3","revision":0},99)[0],200)
            finally:server.shutdown();server.server_close();thread.join()
            store=Store(path,initialize=False);store.db.set_authorizer(color_write_authorizer)
            try:
                self.assertEqual(store.get("calendar_dirty"),"1")
                for sql in ("UPDATE settings SET value='bad'", "DELETE FROM parent_color_preferences", "UPDATE parent_color_preferences SET parent='p2'", "UPDATE overrides SET parent='p2'", "DELETE FROM cycles", "UPDATE requests SET status='confirmed'", "UPDATE special_events SET title='bad'", "DELETE FROM audit", "INSERT INTO outbox VALUES(1,'{}')"):
                    with self.subTest(sql=sql),self.assertRaises(sqlite3.DatabaseError):store.db.execute(sql)
            finally:store.close()
