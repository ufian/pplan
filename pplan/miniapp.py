"""Read-only Telegram Mini App; family data and bot credentials stay on the NAS."""

import argparse
import calendar
from datetime import date, timedelta
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import time
from urllib.parse import parse_qsl, urlsplit

from pplan.core import Config, Plan, PlanError, Store

ASSETS = Path(__file__).with_name("web")


def authorize_init_data(raw, token, operator, now=None):
    """Verify Telegram's HMAC, freshness and the configured operator allowlist."""
    try:
        if not isinstance(raw, str) or not 0 < len(raw) <= 12000:
            raise ValueError()
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=32)
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError()
        supplied = fields.pop("hash")
        check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        secret = hmac.digest(b"WebAppData", token.encode(), "sha256")
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied):
            raise ValueError()
        age = (time.time() if now is None else now) - int(fields["auth_date"])
        user = json.loads(fields["user"])
        if not -30 <= age <= 3600 or type(user.get("id")) is not int or user["id"] != operator:
            raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError):
        raise PlanError("Open the calendar from your private bot chat. If it expired, close and reopen it.") from None
    return operator


def calendar_data(plan, month):
    if not isinstance(month, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
        raise PlanError("Choose a month in YYYY-MM format.")
    try:
        first = date.fromisoformat(month + "-01")
        if not 1901 <= first.year <= 2099:
            raise ValueError()
        weeks = calendar.Calendar(0).monthdatescalendar(first.year, first.month)
    except ValueError:
        raise PlanError("Choose a month between 1901 and 2099.") from None
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    events = plan.special_events(weeks[0][0], weeks[-1][-1])
    result = []
    for week in weeks:
        for day in week:
            before_day = day - timedelta(days=1)
            before = {c: plan.assignment(c, before_day)["parent"] if before_day >= plan.config.anchor_date else None for c in plan.config.children}
            after = {c: plan.assignment(c, day)["parent"] if day >= plan.config.anchor_date else None for c in plan.config.children}
            result.append({"date": day.isoformat(), "before": before, "after": after,
                           "handover": plan.config.boundary(day).isoformat(),
                           "until": plan.config.boundary(day + timedelta(days=1)).isoformat(),
                           "events": [{"id": e["id"], "title": e["title"] or "Special event"} for e in events if e["start"] <= day.isoformat() <= e["end"]]})
    return {"month": month, "today": plan.today().isoformat(), "timezone": plan.config.timezone,
            "parents": plan.config.parents, "children": plan.config.children,
            "handover_confirmed": plan.config.handover_confirmed,
            "days": result, "totals": plan.overnight_totals(first, last),
            "year_totals": plan.overnight_totals(date(first.year, 1, 1), last)}


def make_handler(config, database, token):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def log_message(self, *args):
            pass  # Never log initData, headers, family data or credentials.

        def reply(self, status, body, content_type="application/json"):
            if not isinstance(body, bytes):
                body = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'self' https://web.telegram.org https://*.telegram.org")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/health":
                try:
                    store = Store(database, read_only=True)
                    try:
                        Plan(config, store)
                    finally:
                        store.close()
                    self.reply(200, {"status": "ok"})
                except Exception:
                    self.reply(503, {"error": "Calendar unavailable."})
                return
            assets = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if path not in assets:
                self.reply(404, {"error": "Not found."})
                return
            name, kind = assets[path]
            self.reply(200, (ASSETS / name).read_bytes(), kind)

        def do_POST(self):
            if self.path != "/api/calendar":
                self.reply(404, {"error": "Not found."})
                return
            try:
                authorization = self.headers.get("Authorization", "")
                authorize_init_data(authorization.removeprefix("tma ") if authorization.startswith("tma ") else "", token, config.operator_telegram_id)
            except PlanError as exc:
                self.reply(401, {"error": str(exc)})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 256 or self.headers.get_content_type() != "application/json":
                    raise ValueError()
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError()
                store = Store(database, read_only=True)
                try:
                    store.db.execute("BEGIN")
                    plan = Plan(config, store)
                    data = calendar_data(plan, body.get("month", plan.today().strftime("%Y-%m")))
                finally:
                    store.close()
                self.reply(200, data)
            except PlanError as exc:
                self.reply(400, {"error": str(exc)})
            except (ValueError, TypeError):
                self.reply(400, {"error": "Invalid calendar request."})
            except Exception:
                logging.warning("Calendar read failed; check NAS storage and configuration.")
                self.reply(503, {"error": "Calendar unavailable. Please try again."})
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/config/config.json")
    parser.add_argument("--database", default="/data/plan.sqlite3")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    token = Path(os.environ["TELEGRAM_BOT_TOKEN_FILE"]).read_text().strip()
    config = Config.load(args.config)
    ThreadingHTTPServer((args.host, args.port), make_handler(config, args.database, token)).serve_forever()


if __name__ == "__main__":
    main()
