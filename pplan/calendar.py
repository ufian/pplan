"""One-way Google Calendar projection with continuous parent periods and shared events for matching child schedules."""

from datetime import date, datetime, timedelta
import hashlib
import json
import os
import re
import random
import time
from pathlib import Path
import tempfile
from urllib.parse import quote, urlencode, urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from pplan.core import PlanError, days
from pplan.colors import google_color, plan_parent_palette, parent_palette_version

SCOPES = ["https://www.googleapis.com/auth/calendar.app.created"]
EVENT_SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
BASE = "https://www.googleapis.com/calendar/v3"


def request_with_backoff(session, method, path, body=None):
    """Retry transient Google throttling/backend errors; never log provider payloads."""
    for attempt in range(5):
        response = session.request(method, BASE + path, json=body, timeout=20)
        retry = response.status_code in {429,500,502,503,504}
        if response.status_code == 403:
            try:
                reasons = {error.get("reason") for error in response.json().get("error", {}).get("errors", [])}
                retry = bool(reasons & {"rateLimitExceeded", "userRateLimitExceeded", "backendError"})
            except (ValueError, TypeError, AttributeError):
                retry = False
        if not retry or attempt == 4:
            return response
        time.sleep(2 ** attempt + random.random())


def save_token(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".oauth-")
    try:
        with os.fdopen(fd, "w") as file:
            file.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def login(credentials_path, token_path):
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise PlanError("Install the Google extra: pip install '.[google]'.") from exc
    flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    save_token(token_path, credentials.to_json())


class GoogleClient:
    def __init__(self, token_path):
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import AuthorizedSession
        except ImportError as exc:
            raise PlanError("Install the Google extra: pip install '.[google]'.") from exc
        self.token_path = token_path
        self.credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        self.session = AuthorizedSession(self.credentials)
        self.saved_token = self.credentials.to_json()

    def request(self, method, path, body=None):
        response = request_with_backoff(self.session, method, path, body)
        serialized = self.credentials.to_json()
        if serialized != self.saved_token:
            save_token(self.token_path, serialized)
            self.saved_token = serialized
        if response.status_code >= 400:
            return response.status_code, None
        return response.status_code, None if response.status_code == 204 else response.json()


def validate_calendar_id(value):
    if not isinstance(value, str) or not value or value == "primary" or len(value) > 1024 or any(c.isspace() or ord(c) < 32 for c in value):
        raise PlanError("Set GOOGLE_CALENDAR_ID to the explicit ID of the calendar shared with the service account.")
    return value


class GoogleServiceAccountClient:
    """Use only one explicitly configured existing calendar; never impersonate a user."""
    def __init__(self, key_path, calendar_id):
        self.expected_calendar_id = validate_calendar_id(calendar_id)
        self.event_path = "/calendars/" + quote(self.expected_calendar_id, safe="") + "/events"
        try:
            from google.oauth2 import service_account
            from google.auth.transport.requests import AuthorizedSession
        except ImportError as exc:
            raise PlanError("Install the Google extra: pip install '.[google]'.") from exc
        self.credentials = service_account.Credentials.from_service_account_file(str(key_path), scopes=EVENT_SCOPES)
        self.session = AuthorizedSession(self.credentials)

    def request(self, method, path, body=None):
        parsed = urlsplit(path) if isinstance(path, str) else None
        suffix = parsed.path[len(self.event_path):] if parsed and parsed.path.startswith(self.event_path) else None
        event_route = suffix is not None and re.fullmatch(r"/[0-9a-f]{32}", suffix)
        label_query = parsed is not None and parsed.query == "eventLabelVersion=1"
        allowed = (parsed is not None and not parsed.scheme and not parsed.netloc and not parsed.fragment
                   and suffix is not None and ((method == "GET" and (suffix == "" or event_route))
                   or (method == "POST" and suffix == "" and (not parsed.query or label_query))
                   or (method in {"PUT", "DELETE"} and event_route and (not parsed.query or method == "PUT" and label_query))))
        if not allowed:
            raise PlanError("Service-account requests must target events in the configured calendar.")
        response = request_with_backoff(self.session, method, path, body)
        if response.status_code >= 400:
            return response.status_code, None
        return response.status_code, None if response.status_code == 204 else response.json()


def configured_google_client():
    key_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE")
    if key_path:
        return GoogleServiceAccountClient(key_path, os.environ.get("GOOGLE_CALENDAR_ID", ""))
    return GoogleClient(os.environ.get("GOOGLE_TOKEN_PATH", "data/google-token.json"))


def google_credentials_available():
    path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE") or os.environ.get("GOOGLE_TOKEN_PATH", "data/google-token.json")
    return Path(path).is_file()


class CalendarSync:
    def __init__(self, plan, client):
        self.plan, self.store, self.client = plan, plan.store, client
        self.expected_calendar_id = getattr(client, "expected_calendar_id", None)
        self._check_target()

    def _check_target(self):
        connected = self.store.get("google_calendar_id")
        if self.expected_calendar_id and connected and connected != self.expected_calendar_id:
            raise PlanError("The configured calendar differs from the connected calendar. Refusing to switch calendars silently.")

    def connect(self):
        self._check_target()
        existing = self.store.get("google_calendar_id")
        if self.expected_calendar_id:
            path = "/calendars/" + quote(self.expected_calendar_id, safe="") + "/events?maxResults=1&fields=accessRole"
            status, body = self.client.request("GET", path)
            if status != 200 or not isinstance(body, dict) or body.get("accessRole") not in {"writer", "owner"}:
                raise PlanError("The service account needs 'Make changes to events' access to the configured calendar. Check its Calendar ID and sharing settings.")
            self.store.set("google_calendar_id", self.expected_calendar_id)
            self.store.set("calendar_dirty", "1")
            return self.expected_calendar_id
        if existing:
            return existing
        status, body = self.client.request("POST", "/calendars", {
            "summary": "Parenting Plan", "timeZone": self.plan.config.timezone,
            "description": "Managed by Parenting Plan. Make changes through the bot.",
        })
        if status != 200 or not body or not body.get("id"):
            raise PlanError("Could not create a Google Calendar. Check OAuth configuration.")
        self.store.set("google_calendar_id", body["id"])
        return body["id"]

    def _children(self, event):
        private = event.get("extendedProperties", {}).get("private", {})
        if private.get("pplan") != self.store.get("plan_id") or not re.fullmatch(r"[0-9a-f]{32}", event.get("id", "")):
            return []
        if private.get("format") == "periods-v1":
            children = private.get("children", "").split(",")
        elif not private.get("format"):
            children = [private.get("child")]
        else:
            return []
        return children if children and len(set(children)) == len(children) and all(c in self.plan.config.children for c in children) else []

    def _special_id(self, event):
        private = event.get("extendedProperties", {}).get("private", {})
        value = private.get("special_id", "")
        if (private.get("pplan") == self.store.get("plan_id") and private.get("format") == "specials-v1"
                and re.fullmatch(r"[0-9a-f]{32}", event.get("id", "")) and re.fullmatch(r"[1-9][0-9]*", value)):
            return int(value)
        return None

    def owned_events(self, path, specials=False):
        """List only the requested recognized format belonging to this plan."""
        result, page, seen = {}, None, set()
        while True:
            query = {"privateExtendedProperty": "pplan=" + self.store.get("plan_id"), "maxResults": 2500,
                     "showDeleted": "false", "fields": "items(id,start,end,extendedProperties),nextPageToken"}
            if page:
                query["pageToken"] = page
            status, body = self.client.request("GET", path + "?" + urlencode(query))
            if status != 200 or not isinstance(body, dict):
                raise PlanError(f"Could not inspect managed Google events (status {status}).")
            for event in body.get("items", []):
                owned = self._special_id(event) is not None if specials else bool(self._children(event))
                if owned:
                    result[event["id"]] = event
            page = body.get("nextPageToken")
            if not page:
                return result
            if page in seen:
                raise PlanError("Google returned a repeated event page; retry later.")
            seen.add(page)

    def periods(self, horizon=60, remote=None):
        """Create maximal runs per child; combine runs with identical parent and boundaries."""
        config = self.plan.config
        first = max(self.plan.today() - timedelta(days=1), config.anchor_date)
        last = first + timedelta(days=horizon - 1)
        for row in self.store.db.execute("SELECT day FROM overrides WHERE day>=?", (first.isoformat(),)):
            last = max(last, date.fromisoformat(row["day"]))
        for row in self.store.db.execute("SELECT start FROM cycles WHERE start>=?", (first.isoformat(),)):
            last = max(last, date.fromisoformat(row["start"]) + timedelta(days=horizon - 1))
        bounds = {child: [first, last] for child in config.children}

        def cover(children, start, end):
            if end <= start or (end - start).days > 3660:
                return
            for child in children:
                if child in bounds and end > config.anchor_date:
                    bounds[child][0] = min(bounds[child][0], max(start, config.anchor_date))
                    bounds[child][1] = max(bounds[child][1], end - timedelta(days=1))

        for row in self.store.db.execute("SELECT key FROM calendar_cache"):
            child, value = row["key"].split(":", 1)
            day = date.fromisoformat(value)
            cover([child], day, day + timedelta(days=1))
        for row in self.store.db.execute("SELECT start,end,children FROM calendar_period_cache"):
            cover(json.loads(row["children"]), date.fromisoformat(row["start"]), date.fromisoformat(row["end"]))
        for event in (remote or {}).values():
            try:
                def local_day(boundary):
                    if "dateTime" in boundary:
                        return datetime.fromisoformat(boundary["dateTime"]).astimezone(ZoneInfo(config.timezone)).date()
                    return date.fromisoformat(boundary["date"])
                cover(self._children(event), local_day(event["start"]), local_day(event["end"]))
            except (KeyError, TypeError, ValueError):
                continue
        groups = {}
        for child, (start, end) in bounds.items():
            parent = self.plan.assignment(child, start)["parent"]
            while start > config.anchor_date and self.plan.assignment(child, start - timedelta(days=1))["parent"] == parent:
                start -= timedelta(days=1)
            cursor = start
            while cursor <= end:
                parent = self.plan.assignment(child, cursor)["parent"]
                until = cursor + timedelta(days=1)
                while until <= end and self.plan.assignment(child, until)["parent"] == parent:
                    until += timedelta(days=1)
                if until > end:
                    probe, limit = until, end + timedelta(days=366)
                    while probe <= limit and self.plan.assignment(child, probe)["parent"] == parent:
                        probe += timedelta(days=1)
                    until = probe if probe <= limit else end + timedelta(days=1)
                groups.setdefault((cursor, until, parent), []).append(child)
                cursor = until
        result = []
        for (start, end, parent), children in sorted(groups.items()):
            identity = json.dumps([self.store.get("plan_id"), "periods-v1", children, start.isoformat(), parent])
            result.append({"key": hashlib.sha256(identity.encode()).hexdigest()[:32], "children": children,
                           "start": start, "end": end, "parent": parent})
        return result

    def event(self, period, event_id):
        config = self.plan.config
        return {"id": event_id,
                "summary": " and ".join(config.children[c] for c in period["children"]) + " with " + config.parents[period["parent"]],
                **self.parent_color_fields(period["parent"]),
                "description": "Confirmed continuous parenting period. Edit through the bot; calendar edits are not imported.",
                "start": {"dateTime": config.boundary(period["start"]).isoformat(), "timeZone": config.timezone},
                "end": {"dateTime": config.boundary(period["end"]).isoformat(), "timeZone": config.timezone},
                "extendedProperties": {"private": {"pplan": self.store.get("plan_id"), "format": "periods-v1",
                                                   "children": ",".join(period["children"]), "period_key": period["key"]}}}

    def parent_color_fields(self, parent):
        color = plan_parent_palette(self.plan)[parent]
        return {"eventLabelId": color["label_id"]} if "label_id" in color else {"colorId": color["id"]}

    def _write_period(self, method, path, event):
        if "eventLabelId" in event:
            path += "?eventLabelVersion=1"
        return self.client.request(method, path, event)

    def _upsert_period(self, path, period, remote, force):
        # Reserve IDs before HTTP so a retry after a lost response cannot create duplicates.
        # New IDs after a retired run also avoid reusing Google's deleted-event tombstones.
        self.store.db.execute("INSERT INTO calendar_period_cache VALUES(?,?,?, ?,?,?) ON CONFLICT(period_key) "
                              "DO UPDATE SET start=excluded.start,end=excluded.end,children=excluded.children",
                              (period["key"], uuid4().hex, "", period["start"].isoformat(), period["end"].isoformat(), json.dumps(period["children"])))
        for _ in range(3):
            cached = self.store.db.execute("SELECT * FROM calendar_period_cache WHERE period_key=?", (period["key"],)).fetchone()
            event = self.event(period, cached["event_id"])
            signature = hashlib.sha256(json.dumps([self.store.get("google_calendar_id"), event], sort_keys=True).encode()).hexdigest()
            if not force and cached["signature"] == signature and event["id"] in remote:
                return event["id"], 0
            event_path = path + "/" + event["id"]
            status, _ = self._write_period("PUT" if event["id"] in remote else "POST", event_path if event["id"] in remote else path, event)
            if status == 409:
                found, existing = self.client.request("GET", event_path + "?fields=id,extendedProperties")
                if found == 200 and isinstance(existing, dict) and self._children(existing):
                    status, _ = self._write_period("PUT", event_path, event)
                elif found in (404,410) or found == 200:
                    status = 410
                else:
                    raise PlanError(f"Could not verify projected event identity (status {found}).")
            if status in (404,410):
                self.store.db.execute("UPDATE calendar_period_cache SET event_id=?,signature='' WHERE period_key=?", (uuid4().hex, period["key"]))
                continue
            if status not in (200,201):
                raise PlanError(f"Google Calendar sync failed (status {status}); confirmed changes remain saved.")
            self.store.db.execute("UPDATE calendar_period_cache SET signature=? WHERE period_key=?", (signature, period["key"]))
            return event["id"], 1
        raise PlanError("Could not repair a deleted Google event. Retry synchronization.")

    def special_event(self, special, event_id):
        return {"id": event_id, "summary": special["title"] or "Special event",
                "description": special["description"], "colorId": google_color(special["color"])["id"],
                "start": {"date": special["start"]},
                "end": {"date": (date.fromisoformat(special["end"])+timedelta(days=1)).isoformat()},
                "transparency": "transparent",
                "extendedProperties": {"private": {"pplan": self.store.get("plan_id"), "format": "specials-v1",
                                                   "special_id": str(special["id"])}}}

    def _upsert_special(self, path, special, remote, force):
        self.store.db.execute("INSERT OR IGNORE INTO calendar_special_cache VALUES(?,?,?)", (special["id"], uuid4().hex, ""))
        for _ in range(3):
            cached = self.store.db.execute("SELECT * FROM calendar_special_cache WHERE special_id=?", (special["id"],)).fetchone()
            event = self.special_event(special, cached["event_id"])
            signature = hashlib.sha256(json.dumps([self.store.get("google_calendar_id"), event], sort_keys=True).encode()).hexdigest()
            if not force and cached["signature"] == signature and event["id"] in remote:
                return event["id"], 0
            event_path = path + "/" + event["id"]
            status, _ = self.client.request("PUT" if event["id"] in remote else "POST", event_path if event["id"] in remote else path, event)
            if status == 409:
                found, existing = self.client.request("GET", event_path + "?fields=id,extendedProperties")
                if found == 200 and isinstance(existing, dict) and self._special_id(existing) == special["id"]:
                    status, _ = self.client.request("PUT", event_path, event)
                elif found in (404,410) or found == 200:
                    status = 410
                else:
                    raise PlanError(f"Could not verify projected special-event identity (status {found}).")
            if status in (404,410):
                self.store.db.execute("UPDATE calendar_special_cache SET event_id=?,signature='' WHERE special_id=?", (uuid4().hex, special["id"]))
                continue
            if status not in (200,201):
                raise PlanError(f"Google special-event sync failed (status {status}); changes remain saved.")
            self.store.db.execute("UPDATE calendar_special_cache SET signature=? WHERE special_id=?", (signature, special["id"]))
            return event["id"], 1
        raise PlanError("Could not repair a deleted Google special event. Retry synchronization.")

    def sync(self, horizon=60, force=False):
        self._check_target()
        if not self.plan.config.handover_confirmed:
            raise PlanError("Confirm school-day and non-school handover times in configuration before calendar sync.")
        if not 1 <= horizon <= 366:
            raise PlanError("Calendar horizon must be 1–366 days.")
        calendar_id = self.store.get("google_calendar_id")
        if not calendar_id:
            raise PlanError("Google Calendar is not connected. Run calendar-connect first.")
        path = "/calendars/" + quote(calendar_id, safe="") + "/events"
        remote = self.owned_events(path)
        remote_specials = self.owned_events(path, specials=True)
        with self.store.transaction():
            version = self.store.db.execute("SELECT max(id) FROM plan_versions").fetchone()[0]
            palette_version = parent_palette_version(self.plan)
            desired = self.periods(horizon, remote)
            specials = self.plan.special_events()
        active, count = set(), 0
        # Publish replacements first; a failed upload must leave older events available.
        for period in desired:
            event_id, changed = self._upsert_period(path, period, remote, force)
            active.add(event_id)
            count += changed
        for event_id in sorted(set(remote) - active):
            status, _ = self.client.request("DELETE", path + "/" + event_id)
            if status not in (200,204,404,410):
                raise PlanError(f"Could not remove an obsolete managed Google event (status {status}); retry scheduled.")
            count += status in (200,204)
        for row in self.store.db.execute("SELECT event_id FROM calendar_period_cache").fetchall():
            if row["event_id"] not in active:
                self.store.db.execute("DELETE FROM calendar_period_cache WHERE event_id=?", (row["event_id"],))
        special_active = set()
        for special in specials:
            event_id, changed = self._upsert_special(path, special, remote_specials, force)
            special_active.add(event_id)
            count += changed
        for event_id in sorted(set(remote_specials) - special_active):
            status, _ = self.client.request("DELETE", path + "/" + event_id)
            if status not in (200,204,404,410):
                raise PlanError(f"Could not remove an obsolete managed Google special event (status {status}).")
            count += status in (200,204)
        for row in self.store.db.execute("SELECT event_id FROM calendar_special_cache").fetchall():
            if row["event_id"] not in special_active:
                self.store.db.execute("DELETE FROM calendar_special_cache WHERE event_id=?", (row["event_id"],))
        self.store.db.execute("DELETE FROM calendar_cache")
        with self.store.transaction():
            newer = self.store.db.execute("SELECT max(id) FROM plan_versions").fetchone()[0]
            changed = newer != version or parent_palette_version(self.plan) != palette_version
            self.store.set("calendar_dirty", "1" if changed else "0")
            self.store.set("calendar_error", "none")
            self.store.set("calendar_last_success", self.plan.clock().isoformat())
        return count
