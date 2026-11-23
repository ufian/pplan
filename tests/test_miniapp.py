from datetime import date
import hashlib
import hmac
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from unittest.mock import patch

from pplan.core import Plan, PlanError, Store
from pplan.miniapp import authorize_init_data, calendar_data, make_handler
from tests.test_plan import Fixture

TOKEN = "12345:fixture-token"


def signed(actor=42, at=None, extra=None):
    fields = {"auth_date": str(int(time.time() if at is None else at)), "user": json.dumps({"id": actor})}
    fields.update(extra or {})
    secret = hmac.digest(b"WebAppData", TOKEN.encode(), "sha256")
    check = "\n".join(f"{k}={v}" for k,v in sorted(fields.items()))
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class MiniAppTests(Fixture):
    def test_valid_signature_including_telegram_signature_field(self):
        self.assertEqual(authorize_init_data(signed(at=1000, extra={"signature": "fixture"}), TOKEN, 42, now=1000), 42)

    def test_invalid_expired_future_and_other_users_are_rejected(self):
        valid = signed(at=1000)
        for raw in ("", valid.replace("1000", "1001"), valid+"&auth_date=1000", signed(99,1000),
                    signed(at=-3000), signed(at=1031), signed(at=1000, extra={"user":"[]"}), "user=%ZZ"):
            with self.subTest(raw=raw), self.assertRaises(PlanError):
                authorize_init_data(raw, TOKEN, 42, now=1000)

    def test_calendar_matches_confirmed_plan_and_preserves_pending_changes(self):
        pending = self.draft()
        data = calendar_data(self.plan, "2026-11")
        target = next(d for d in data["days"] if d["date"] == "2026-11-23")
        self.assertEqual(target["before"], {"c1": None,"c2":None})
        self.assertEqual(target["after"], {"c1":"p1","c2":"p1"})
        self.plan.confirm(42, pending["id"])
        self.plan.add_special(42, date(2026,11,23), date(2026,11,24), "Fixture event")
        data = calendar_data(self.plan, "2026-11")
        target = next(d for d in data["days"] if d["date"] == "2026-11-23")
        self.assertEqual(target["after"], {"c1":"p2","c2":"p1"})
        self.assertEqual(target["events"][0]["title"], "Fixture event")
        self.assertEqual(sum(data["totals"]["c1"].values()), 8)
        self.assertEqual(data["days"][0]["date"], "2026-10-26")
        self.assertEqual(data["days"][-1]["date"], "2026-12-06")

    def test_invalid_months_and_leap_year(self):
        for month in (None, "2026-1", "2026-13", "0001-01", "2100-01", "../../config.json"):
            with self.subTest(month=month), self.assertRaises(PlanError):
                calendar_data(self.plan, month)
        self.assertTrue(any(d["date"]=="2028-02-29" for d in calendar_data(self.plan,"2028-02")["days"]))

    def test_read_only_store_cannot_change_data_or_pending_confirmations(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"plan.sqlite3"
            writer = Store(path)
            try:
                plan = Plan(self.config, writer, lambda:self.now)
                pending = plan.draft(42,self.start,self.start,"p2",["c1"])
                reader = Store(path,read_only=True)
                try:
                    view = Plan(self.config,reader,lambda:self.now)
                    calendar_data(view,"2026-11")
                    with self.assertRaises(sqlite3.OperationalError):
                        reader.set("fixture","forbidden")
                finally:
                    reader.close()
                self.assertEqual(writer.db.execute("SELECT status FROM requests WHERE id=?",(pending["id"],)).fetchone()[0],"draft")
                plan.confirm(42,pending["id"])
            finally:
                writer.close()

    def test_http_requires_auth_and_never_exposes_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"plan.sqlite3"
            writer=Store(path);Plan(self.config,writer,lambda:self.now)
            server=ThreadingHTTPServer(("127.0.0.1",0),make_handler(self.config,path,TOKEN))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            url=f"http://127.0.0.1:{server.server_port}"
            try:
                for target in ("/","/?tgWebAppStartParam=calendar","/app.js","/style.css","/health"):
                    with urlopen(url+target) as response:
                        self.assertEqual(response.status,200)
                        self.assertEqual(response.headers["Cache-Control"],"no-store")
                for target in ("/config.json","/../core.py","/api/calendar"):
                    with self.assertRaises(HTTPError) as exc:
                        urlopen(url+target)
                    self.assertEqual(exc.exception.code,404)
                for auth,expected in (("",401),("tma "+signed(99),401),("tma "+signed(),200)):
                    request=Request(url+"/api/calendar",data=b'{"month":"2026-11"}',headers={"Content-Type":"application/json","Authorization":auth})
                    if expected==200:
                        with urlopen(request) as response:
                            self.assertEqual(json.load(response)["month"],"2026-11")
                    else:
                        with self.assertRaises(HTTPError) as exc:
                            urlopen(request)
                        self.assertEqual(exc.exception.code,expected)
                with self.assertRaises(HTTPError) as exc:
                    urlopen(Request(url+"/api/calendar",data=b'[]',headers={"Content-Type":"application/json","Authorization":"tma "+signed()}))
                self.assertEqual(exc.exception.code,400)
            finally:
                server.shutdown();server.server_close();thread.join();writer.close()

    def test_bot_calendar_button(self):
        from pplan.telegram import Bot
        bot=Bot(self.plan,None)
        with patch.dict(os.environ,{"PPLAN_MINIAPP_URL":"https://calendar.example:8443"}):
            bot.command(42,"/calendar")
        job=json.loads(self.store.db.execute("SELECT payload FROM outbox").fetchone()[0])
        self.assertEqual(job["payload"]["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"],"https://calendar.example:8443")
