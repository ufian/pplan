"""One-way Google Calendar projection with stable per-child event IDs."""

from datetime import date, timedelta
import hashlib
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import quote

from pplan.core import PlanError, days

SCOPES = ["https://www.googleapis.com/auth/calendar.app.created"]
BASE = "https://www.googleapis.com/calendar/v3"


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
        response = self.session.request(method, BASE + path, json=body, timeout=20)
        serialized = self.credentials.to_json()
        if serialized != self.saved_token:
            save_token(self.token_path, serialized)
            self.saved_token = serialized
        if response.status_code >= 400:
            return response.status_code, None
        return response.status_code, response.json()


class CalendarSync:
    def __init__(self, plan, client):
        self.plan, self.store, self.client = plan, plan.store, client

    def connect(self):
        existing = self.store.get("google_calendar_id")
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

    def event(self, child, day):
        config = self.plan.config
        parent = self.plan.assignment(child, day)["parent"]
        event_id = hashlib.sha256(f"{self.store.get('plan_id')}:{child}:{day}".encode()).hexdigest()[:32]
        return {
            "id": event_id,
            "summary": f"{config.children[child]} with {config.parents[parent]}",
            "description": "Confirmed parenting schedule. Edit through the bot; calendar edits are not imported.",
            "start": {"dateTime": config.boundary(day).isoformat(), "timeZone": config.timezone},
            "end": {"dateTime": config.boundary(day + timedelta(days=1)).isoformat(), "timeZone": config.timezone},
            "extendedProperties": {"private": {"pplan": self.store.get("plan_id"), "child": child}},
        }

    def sync(self, horizon=60, force=False):
        if not self.plan.config.handover_confirmed:
            raise PlanError("Confirm school-day and non-school handover times in configuration before calendar sync.")
        if not 1 <= horizon <= 366:
            raise PlanError("Calendar horizon must be 1–366 days.")
        calendar_id = self.store.get("google_calendar_id")
        if not calendar_id:
            raise PlanError("Google Calendar is not connected. Run calendar-connect first.")
        start = max(self.plan.today() - timedelta(days=1), self.plan.config.anchor_date)
        targets = {(child, day) for day in days(start, start + timedelta(days=horizon - 1)) for child in self.plan.config.children}
        # Include approved changes beyond the rolling window immediately.
        for row in self.store.db.execute("SELECT child, day FROM overrides WHERE day>=?", (start.isoformat(),)):
            targets.add((row["child"], date.fromisoformat(row["day"])))
        for row in self.store.db.execute("SELECT child, start FROM cycles WHERE start>=?", (start.isoformat(),)):
            phase_start = date.fromisoformat(row["start"])
            targets.update((row["child"], day) for day in days(phase_start, phase_start + timedelta(days=horizon - 1)))
        # A new cycle can change previously projected future dates outside today's rolling window.
        for row in self.store.db.execute("SELECT key FROM calendar_cache"):
            child, value = row["key"].split(":")
            day = date.fromisoformat(value)
            if day >= start:
                targets.add((child, day))
        path = "/calendars/" + quote(calendar_id, safe="") + "/events"
        count = 0
        for child, day in sorted(targets):
            event = self.event(child, day)
            signature = hashlib.sha256(json.dumps([calendar_id, event], sort_keys=True).encode()).hexdigest()
            key = f"{child}:{day}"
            cached = self.store.db.execute("SELECT signature FROM calendar_cache WHERE key=?", (key,)).fetchone()
            if cached and cached[0] == signature and not force:
                continue
            event_path = path + "/" + event["id"]
            status, _ = self.client.request("PUT", event_path, event)
            if status == 404:
                status, _ = self.client.request("POST", path, event)
                if status == 409:
                    status, _ = self.client.request("PUT", event_path, event)
            if status not in (200, 201):
                raise PlanError(f"Google Calendar sync failed (status {status}); confirmed changes remain saved.")
            self.store.db.execute("INSERT OR REPLACE INTO calendar_cache VALUES (?, ?)", (key, signature))
            count += 1
        self.store.set("calendar_dirty", "0")
        self.store.set("calendar_error", "none")
        self.store.set("calendar_last_success", self.plan.clock().isoformat())
        return count
