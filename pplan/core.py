"""Transport-independent scheduling, persistence, and confirmed changes."""

from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4
from zoneinfo import ZoneInfo


class PlanError(ValueError):
    """An actionable error that is safe to display to the operator."""


@dataclass(frozen=True)
class Config:
    timezone: str
    operator_telegram_id: int
    parents: dict[str, str]
    children: dict[str, str]
    anchor_date: date
    cycle: tuple[str, ...]
    operator_parent_id: str | None = None
    school_handover_times: dict[str, str] = field(default_factory=lambda: {
        "0": "15:00", "1": "15:00", "2": "15:00", "3": "15:00", "4": "15:00"})
    non_school_handover_time: str = "10:00"
    non_school_dates: tuple[str, ...] = ()
    handover_confirmed: bool = True

    @classmethod
    def load(cls, path: str | Path):
        raw = json.loads(Path(path).read_text())
        try:
            config = cls(
                raw["timezone"], raw["operator_telegram_id"], raw["parents"], raw["children"],
                date.fromisoformat(raw["schedule"]["anchor_date"]),
                tuple(raw["schedule"]["cycle"]),
                raw.get("operator_parent_id"),
                raw["handover"]["school_day_times"], raw["handover"]["non_school_time"],
                tuple(raw["handover"].get("non_school_dates", [])),
                raw["handover"].get("confirmed", False),
            )
            config.validate()
            return config
        except (KeyError, TypeError, ValueError) as exc:
            raise PlanError("Invalid configuration. See config.example.json.") from exc

    def validate(self):
        ZoneInfo(self.timezone)
        if type(self.operator_telegram_id) is not int or self.operator_telegram_id <= 0:
            raise PlanError("operator_telegram_id must be a positive Telegram user ID.")
        if not isinstance(self.parents, dict) or len(self.parents) != 2:
            raise PlanError("Configure exactly two parents.")
        for key, name in self.parents.items():
            if not isinstance(key, str) or not key.isascii() or not key.isidentifier():
                raise PlanError("Parent IDs must be ASCII identifiers, for example alex.")
            if not isinstance(name, str) or not name.strip() or len(name) > 60 or any(ord(c) < 32 for c in name):
                raise PlanError("Parent names must be 1–60 characters without control characters.")
        # A parent's own ID may equal their display name; parents must remain distinguishable.
        a, b = list(self.parents.items())
        if {s.casefold() for s in a} & {s.casefold() for s in b}:
            raise PlanError("Parents must have distinct names and IDs.")
        if not self.cycle or len(self.cycle) > 366 or any(p not in self.parents for p in self.cycle):
            raise PlanError("The schedule cycle must contain 1–366 configured parent IDs.")
        if self.operator_parent_id is not None and self.operator_parent_id not in self.parents:
            raise PlanError("operator_parent_id must be a configured parent ID.")
        if not isinstance(self.children, dict) or len(self.children) != 2:
            raise PlanError("Configure exactly two children.")
        seen = set()
        for key, name in self.children.items():
            if not isinstance(key, str) or not key.isascii() or not key.isidentifier():
                raise PlanError("Child IDs must be ASCII identifiers.")
            if not isinstance(name, str) or not name.strip() or len(name) > 60 or any(ord(c) < 32 for c in name):
                raise PlanError("Child names must be 1–60 characters without control characters.")
            aliases = {key.casefold(), name.casefold()}
            if seen & aliases:
                raise PlanError("Children must have distinct names and IDs.")
            seen |= aliases
        if not isinstance(self.school_handover_times, dict) or any(k not in {str(i) for i in range(7)} for k in self.school_handover_times):
            raise PlanError("School handover keys must be weekday numbers 0 (Monday) through 6 (Sunday).")
        for value in [*self.school_handover_times.values(), self.non_school_handover_time]:
            if not isinstance(value, str) or len(value) != 5 or time.fromisoformat(value).isoformat(timespec="minutes") != value:
                raise PlanError("Handover times must use HH:MM.")
        for value in self.non_school_dates:
            if date.fromisoformat(value).isoformat() != value:
                raise PlanError("Non-school dates must use YYYY-MM-DD.")

    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True, default=str).encode()).hexdigest()

    def boundary(self, day: date):
        value = self.non_school_handover_time
        if day.isoformat() not in self.non_school_dates:
            value = self.school_handover_times.get(str(day.weekday()), value)
        boundary = datetime.combine(day, time.fromisoformat(value), ZoneInfo(self.timezone))
        if boundary.replace(fold=1).utcoffset() != boundary.utcoffset():
            raise PlanError("A handover falls in a daylight-saving transition. Choose an unambiguous time.")
        return boundary


def days(start: date, end: date):
    for offset in range((end - start).days + 1):
        yield start + timedelta(days=offset)


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(str(path), isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS overrides (
                child TEXT NOT NULL, day TEXT NOT NULL, parent TEXT NOT NULL,
                revision TEXT NOT NULL, PRIMARY KEY(child, day)
            );
            CREATE TABLE IF NOT EXISTS requests (
                id TEXT PRIMARY KEY, actor INTEGER NOT NULL, start TEXT NOT NULL,
                end TEXT NOT NULL, parent TEXT NOT NULL, children TEXT NOT NULL, snapshot TEXT NOT NULL,
                status TEXT NOT NULL, created TEXT NOT NULL, expires TEXT NOT NULL, actions TEXT
            );
            CREATE TABLE IF NOT EXISTS cycles (
                child TEXT NOT NULL, start TEXT NOT NULL, cycle TEXT NOT NULL,
                revision TEXT NOT NULL, PRIMARY KEY(child, start)
            );
            CREATE TABLE IF NOT EXISTS special_events (
                id INTEGER PRIMARY KEY, actor INTEGER NOT NULL, start TEXT NOT NULL,
                end TEXT NOT NULL, title TEXT NOT NULL, created TEXT NOT NULL, removed TEXT
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY, request_id TEXT NOT NULL, actor INTEGER NOT NULL,
                action TEXT NOT NULL, at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS improvements (
                id INTEGER PRIMARY KEY, actor INTEGER NOT NULL, message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued', created TEXT NOT NULL,
                updated TEXT NOT NULL, result TEXT
            );
            CREATE TABLE IF NOT EXISTS calendar_cache (
                key TEXT PRIMARY KEY, signature TEXT NOT NULL
            );
        """)
        with self.transaction():
            if "actions" not in {row["name"] for row in self.db.execute("PRAGMA table_info(requests)")}:
                self.db.execute("ALTER TABLE requests ADD COLUMN actions TEXT")
        if str(path) != ":memory:":
            Path(path).chmod(0o600)

    @contextmanager
    def transaction(self):
        nested = self.db.in_transaction
        name = "unit_" + uuid4().hex
        self.db.execute(f"SAVEPOINT {name}" if nested else "BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if nested:
                self.db.execute(f"ROLLBACK TO {name}")
                self.db.execute(f"RELEASE {name}")
            else:
                self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute(f"RELEASE {name}" if nested else "COMMIT")

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, str(value)))

    def enqueue(self, payload):
        self.db.execute("INSERT INTO outbox(payload) VALUES (?)", (json.dumps(payload),))

    def close(self):
        self.db.close()


class Plan:
    def __init__(self, config: Config, store: Store, clock=None):
        config.validate()
        self.config, self.store = config, store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with store.transaction():
            fingerprint = store.get("config_fingerprint")
            has_requests = store.db.execute("SELECT 1 FROM requests LIMIT 1").fetchone()
            if fingerprint and fingerprint != config.fingerprint() and has_requests:
                raise PlanError("Configuration differs from this database. Use a separate database for a new plan.")
            store.set("config_fingerprint", config.fingerprint())
            if not store.get("plan_id"):
                store.set("plan_id", uuid4().hex)

    def today(self):
        return self.clock().astimezone(ZoneInfo(self.config.timezone)).date()

    def authorize(self, actor):
        if actor != self.config.operator_telegram_id:
            raise PlanError("You are not authorized to manage this plan.")

    def assignment(self, child: str, day: date):
        if child not in self.config.children:
            raise PlanError("Unknown child.")
        if day < self.config.anchor_date:
            raise PlanError(f"This plan starts on {self.config.anchor_date}.")
        row = self.store.db.execute("SELECT parent, revision FROM overrides WHERE child=? AND day=?", (child, day.isoformat())).fetchone()
        if row:
            return {"parent": row["parent"], "revision": row["revision"]}
        phase = self.store.db.execute(
            "SELECT * FROM cycles WHERE child=? AND start<=? ORDER BY start DESC LIMIT 1", (child, day.isoformat())
        ).fetchone()
        if phase:
            cycle = json.loads(phase["cycle"])
            index = (day - date.fromisoformat(phase["start"])).days % len(cycle)
            return {"parent": cycle[index], "revision": "cycle:" + phase["revision"]}
        index = (day - self.config.anchor_date).days % len(self.config.cycle)
        return {"parent": self.config.cycle[index], "revision": "base"}

    def schedule(self, start: date, end: date):
        if end < start or (end - start).days > 365:
            raise PlanError("Choose an ordered date range of at most 366 days.")
        return [(day, child, self.assignment(child, day)["parent"])
                for day in days(start, end) for child in self.config.children]

    def repeating_pattern(self, child, day=None):
        if child not in self.config.children:
            raise PlanError("Unknown child.")
        day = day or self.today()
        phase = self.store.db.execute(
            "SELECT cycle FROM cycles WHERE child=? AND start<=? ORDER BY start DESC LIMIT 1", (child, day.isoformat())
        ).fetchone()
        return json.loads(phase["cycle"]) if phase else list(self.config.cycle)

    def special_events(self, start=None, end=None):
        query = "SELECT * FROM special_events WHERE actor=? AND removed IS NULL"
        args = [self.config.operator_telegram_id]
        if start is not None and end is not None:
            query += " AND start<=? AND end>=?"
            args += [end.isoformat(), start.isoformat()]
        return [dict(row) for row in self.store.db.execute(query + " ORDER BY start, end, id", args)]

    def add_special(self, actor, start, end, title=""):
        self.authorize(actor)
        if end < start or (end - start).days > 365:
            raise PlanError("Choose an ordered special-event range of at most 366 days.")
        if not isinstance(title, str) or len(title) > 120 or any(ord(c) < 32 for c in title):
            raise PlanError("Special-event titles must be at most 120 characters without control characters.")
        title = title.strip()
        with self.store.transaction():
            row = self.store.db.execute(
                "SELECT * FROM special_events WHERE actor=? AND start=? AND end=? AND title=? AND removed IS NULL",
                (actor, start.isoformat(), end.isoformat(), title),
            ).fetchone()
            if row:
                return dict(row)
            cursor = self.store.db.execute(
                "INSERT INTO special_events(actor, start, end, title, created) VALUES (?, ?, ?, ?, ?)",
                (actor, start.isoformat(), end.isoformat(), title, self.clock().isoformat()),
            )
            return dict(self.store.db.execute("SELECT * FROM special_events WHERE id=?", (cursor.lastrowid,)).fetchone())

    def remove_special(self, actor, event_id):
        self.authorize(actor)
        with self.store.transaction():
            row = self.store.db.execute("SELECT * FROM special_events WHERE id=? AND actor=? AND removed IS NULL", (event_id, actor)).fetchone()
            if not row:
                raise PlanError("Special event not found.")
            self.store.db.execute("UPDATE special_events SET removed=? WHERE id=?", (self.clock().isoformat(), event_id))
            return dict(row)

    def _validate_dates(self, start, end):
        if end < start or (end - start).days > 30:
            raise PlanError("Choose an ordered date range of at most 31 days.")
        if start < self.config.anchor_date:
            raise PlanError(f"This plan starts on {self.config.anchor_date}.")
        if self.config.boundary(start) <= self.clock():
            raise PlanError("The first handover is already in the past. Choose a future handover date.")
        if end > self.today() + timedelta(days=365):
            raise PlanError("Changes must be within the next 365 days.")

    def draft(self, actor: int, start: date, end: date, parent: str, children):
        return self.draft_actions(actor, [{"type": "assign", "start": start.isoformat(), "end": end.isoformat(),
                                          "parent": parent, "children": list(children), "cycle": []}])

    def _validated_actions(self, actions):
        if not isinstance(actions, list) or not 1 <= len(actions) <= 12:
            raise PlanError("A change request must contain 1–12 supported actions.")
        normalized, assigned, cycle_starts = [], set(), set()
        keys = {"type", "start", "end", "parent", "children", "cycle"}
        for action in actions:
            if not isinstance(action, dict) or set(action) != keys:
                raise PlanError("Invalid action fields.")
            children = action["children"]
            if (not isinstance(children, list) or not children
                    or any(not isinstance(c, str) or c not in self.config.children for c in children)
                    or len(set(children)) != len(children)):
                raise PlanError("Select one or both configured children for each action.")
            try:
                start = date.fromisoformat(action["start"])
                if start.isoformat() != action["start"]:
                    raise ValueError()
                end = date.fromisoformat(action["end"]) if action["end"] is not None else start
                if action["end"] is not None and end.isoformat() != action["end"]:
                    raise ValueError()
            except (TypeError, ValueError) as exc:
                raise PlanError("Actions must use exact YYYY-MM-DD dates.") from exc
            self._validate_dates(start, end)
            if action["type"] in ("assign", "overwrite"):
                if action["end"] is None or not isinstance(action["parent"], str) or action["parent"] not in self.config.parents or action["cycle"] != []:
                    raise PlanError("An assignment requires a parent, end date, and empty cycle.")
                targets = {(c, d) for c in children for d in days(start, end)}
                if assigned & targets:
                    raise PlanError("Assignment actions overlap for the same child. Clarify the handover dates.")
                assigned |= targets
            elif action["type"] == "set_cycle":
                cycle = action["cycle"]
                if (action["end"] is not None or action["parent"] is not None
                        or not isinstance(cycle, list) or not 1 <= len(cycle) <= 366
                        or any(not isinstance(p, str) or p not in self.config.parents for p in cycle)):
                    raise PlanError("A set_cycle action requires a repeating list of parent IDs and no end date or parent field.")
                targets = {(c, start) for c in children}
                if cycle_starts & targets:
                    raise PlanError("Two repeating rules cannot start on the same date for the same child.")
                cycle_starts |= targets
            else:
                raise PlanError("Supported change actions are assign, overwrite, and set_cycle.")
            normalized.append(dict(action, children=list(children), cycle=list(action["cycle"])))
        return sorted(normalized, key=lambda a: (a["start"], a["type"]))

    def _cycle_snapshot(self, children):
        return {c: [dict(row) for row in self.store.db.execute(
            "SELECT start, cycle, revision FROM cycles WHERE child=? ORDER BY start", (c,)
        )] for c in sorted(children)}

    def draft_actions(self, actor, actions):
        self.authorize(actor)
        if not self.config.handover_confirmed:
            raise PlanError("Handover times have not been configured yet. Finish setup before making changes.")
        actions = self._validated_actions(actions)
        with self.store.transaction():
            snapshot, cycle_children, has_change = {}, set(), False
            for action in actions:
                start = date.fromisoformat(action["start"])
                end = date.fromisoformat(action["end"]) if action["end"] else self.today() + timedelta(days=365)
                for child in action["children"]:
                    for day in days(start, end):
                        expected = self.assignment(child, day)
                        snapshot[f"{child}:{day.isoformat()}"] = expected
                        has_change |= action["type"] in ("set_cycle", "overwrite") or expected["parent"] != action["parent"]
                if action["type"] == "set_cycle":
                    cycle_children.update(action["children"])
            if not has_change:
                raise PlanError("Those days are already assigned to that parent.")
            if cycle_children:
                snapshot["__cycles__"] = self._cycle_snapshot(cycle_children)
            request_id = uuid4().hex
            now = self.clock()
            start = min(a["start"] for a in actions)
            end = max(a["end"] or a["start"] for a in actions)
            children = sorted({c for a in actions for c in a["children"]})
            parent = actions[0]["parent"] or actions[0]["cycle"][0]
            self.store.db.execute(
                "INSERT INTO requests(id, actor, start, end, parent, children, snapshot, status, created, expires, actions) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?)",
                (request_id, actor, start, end, parent, json.dumps(children), json.dumps(snapshot),
                 now.isoformat(), (now + timedelta(hours=24)).isoformat(), json.dumps(actions)),
            )
            self._audit(request_id, actor, "drafted")
            return self.request(request_id)

    def request(self, request_id):
        row = self.store.db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
        if not row:
            raise PlanError("Change request not found.")
        return dict(row)

    def confirm(self, actor: int, request_id: str):
        self.authorize(actor)
        with self.store.transaction():
            request = self.request(request_id)
            if request["actor"] != actor:
                raise PlanError("Only the request's author can confirm it.")
            if request["status"] != "draft":
                raise PlanError(f"This request is already {request['status']}.")
            if self.clock() >= datetime.fromisoformat(request["expires"]):
                raise PlanError("This preview has expired. Create a new request.")
            start, end = date.fromisoformat(request["start"]), date.fromisoformat(request["end"])
            actions = json.loads(request["actions"]) if request["actions"] else [{
                "type": "assign", "start": request["start"], "end": request["end"], "parent": request["parent"],
                "children": json.loads(request["children"]), "cycle": [],
            }]
            actions = self._validated_actions(actions)
            snapshot = json.loads(request["snapshot"])
            for key, expected in snapshot.items():
                if key == "__cycles__":
                    if self._cycle_snapshot(expected) != expected:
                        raise PlanError("The repeating schedule changed since this preview. Create a new request.")
                    continue
                child, day = key.split(":")
                if self.assignment(child, date.fromisoformat(day)) != expected:
                    raise PlanError("The schedule changed since this preview. Create a new request.")
            for action in actions:
                if action["type"] in ("assign", "overwrite"):
                    for day in days(date.fromisoformat(action["start"]), date.fromisoformat(action["end"])):
                        for child in action["children"]:
                            self.store.db.execute("INSERT OR REPLACE INTO overrides VALUES (?, ?, ?, ?)",
                                                  (child, day.isoformat(), action["parent"], request_id))
                else:
                    for child in action["children"]:
                        self.store.db.execute("DELETE FROM cycles WHERE child=? AND start>=?", (child, action["start"]))
                        self.store.db.execute("INSERT INTO cycles VALUES (?, ?, ?, ?)",
                                              (child, action["start"], json.dumps(action["cycle"]), request_id))
            self.store.db.execute("UPDATE requests SET status='confirmed' WHERE id=?", (request_id,))
            self._audit(request_id, actor, "confirmed")
            self.store.set("calendar_dirty", "1")
            return self.request(request_id)

    def cancel(self, actor, request_id):
        self.authorize(actor)
        with self.store.transaction():
            request = self.request(request_id)
            if request["actor"] != actor or request["status"] != "draft":
                raise PlanError("Only your draft requests can be cancelled.")
            self.store.db.execute("UPDATE requests SET status='cancelled' WHERE id=?", (request_id,))
            self._audit(request_id, actor, "cancelled")

    def _audit(self, request_id, actor, action):
        self.store.db.execute("INSERT INTO audit(request_id, actor, action, at) VALUES (?, ?, ?, ?)",
                              (request_id, actor, action, self.clock().isoformat()))
