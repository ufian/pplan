"""Immutable schedule/event versions and owner-confirmed restoration; membership is never restored."""

from datetime import datetime, timedelta
import hashlib
import json
from uuid import uuid4

from pplan.core import PlanError


def capture_state(plan):
    state = {"schema": 1}
    for table, order in (("overrides", "child,day"), ("cycles", "child,start"), ("special_events", "id")):
        state[table] = [dict(row) for row in plan.store.db.execute(f"SELECT * FROM {table} ORDER BY {order}")]
    return state


def encode_state(state):
    return json.dumps(state, sort_keys=True, separators=(",", ":"))


def state_hash(state):
    return hashlib.sha256(encode_state(state).encode()).hexdigest()


def record_change(plan, actor, action, reference=None):
    if not plan.store.db.in_transaction:
        raise RuntimeError("History must commit atomically with its change.")
    state = encode_state(capture_state(plan))
    cursor = plan.store.db.execute(
        "INSERT INTO plan_versions(actor,action,reference,created,config_fingerprint,state,state_hash) VALUES(?,?,?,?,?,?,?)",
        (actor, action, reference, plan.clock().isoformat(), plan.config.fingerprint(), state, hashlib.sha256(state.encode()).hexdigest()),
    )
    return cursor.lastrowid


def ensure_baseline(plan):
    if not plan.store.db.execute("SELECT 1 FROM plan_versions LIMIT 1").fetchone():
        record_change(plan, plan.config.operator_telegram_id, "baseline")


def list_versions(plan, actor, limit=10):
    plan.authorize_owner(actor)
    return [dict(row) for row in plan.store.db.execute(
        "SELECT id,actor,action,reference,created FROM plan_versions ORDER BY id DESC LIMIT ?", (max(1,min(int(limit),100)),))]


def load_version(plan, version_id):
    if type(version_id) is not int or version_id <= 0:
        raise PlanError("Use a positive version number from /history.")
    row = plan.store.db.execute("SELECT * FROM plan_versions WHERE id=?", (version_id,)).fetchone()
    if not row:
        raise PlanError("History version not found. Use /history.")
    try:
        state = json.loads(row["state"])
        if (row["config_fingerprint"] != plan.config.fingerprint() or state.get("schema") != 1
                or state_hash(state) != row["state_hash"]):
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise PlanError("This version is incompatible or failed its integrity check. It cannot be restored.") from None
    return dict(row), state


def describe_diff(plan, before, after):
    lines = []
    for table, keys, label in (("overrides", ("child","day"), "Date-specific assignments"),
                               ("cycles", ("child","start"), "Repeating rules"),
                               ("special_events", ("id",), "Special events")):
        old = {tuple(row[key] for key in keys):row for row in before[table]}
        new = {tuple(row[key] for key in keys):row for row in after[table]}
        changed = [key for key in sorted(set(old)|set(new)) if old.get(key) != new.get(key)]
        lines.append(f"{label}: {len(changed)} changed record(s).")
        for key in changed[:8]:
            a,b = old.get(key),new.get(key)
            if table == "overrides":
                left=plan.config.parents[a["parent"]] if a else "repeating schedule"
                right=plan.config.parents[b["parent"]] if b else "repeating schedule"
                lines.append(f"  {plan.config.children[key[0]]}, {key[1]}: {left} to {right}")
            elif table == "cycles":
                details = "remove rule" if b is None else ", ".join(plan.config.parents[parent] for parent in json.loads(b["cycle"])[:14])
                lines.append(f"  {plan.config.children[key[0]]}, from {key[1]}: {details[:180]}")
            else:
                event=b or a
                state="remove" if b is None or b["removed"] else "restore"
                lines.append(f"  #{key[0]} {event['title'] or 'Special event'}: {state}, {event['start']} through {event['end']}")
        if len(changed)>8:lines.append(f"  … and {len(changed)-8} more.")
    return lines


def prepare_restore(plan, actor, version_id):
    plan.authorize_owner(actor)
    with plan.store.transaction():
        plan.authorize_owner(actor)
        version,target=load_version(plan,version_id)
        current=capture_state(plan)
        if state_hash(current)==version["state_hash"]:
            raise PlanError("The plan already matches this version.")
        now=plan.clock();preview_id=uuid4().hex
        plan.store.db.execute("INSERT INTO restore_previews(id,actor,version_id,base_hash,created,expires) VALUES(?,?,?,?,?,?)",
                              (preview_id,actor,version_id,state_hash(current),now.isoformat(),(now+timedelta(minutes=15)).isoformat()))
        return {"id":preview_id,"version_id":version_id,"created":version["created"],
                "changes":describe_diff(plan,current,target),
                "pending":plan.store.db.execute("SELECT count(*) FROM requests WHERE status='draft'").fetchone()[0]}


def confirm_restore(plan, actor, preview_id):
    plan.authorize_owner(actor)
    with plan.store.transaction():
        plan.authorize_owner(actor)
        preview=plan.store.db.execute("SELECT * FROM restore_previews WHERE id=? AND actor=?",(preview_id,actor)).fetchone()
        if not preview or preview["status"]!='draft':
            raise PlanError("This restore preview is no longer available. Use /restore VERSION again.")
        if plan.clock()>=datetime.fromisoformat(preview["expires"]):
            raise PlanError("This restore preview expired. Use /restore VERSION again.")
        if state_hash(capture_state(plan))!=preview["base_hash"]:
            raise PlanError("The plan changed since this restore preview. Review a new /restore VERSION preview.")
        _,state=load_version(plan,preview["version_id"])
        revision="restore:"+uuid4().hex
        plan.store.db.execute("DELETE FROM overrides")
        for row in state["overrides"]:
            plan.store.db.execute("INSERT INTO overrides VALUES(?,?,?,?)",(row["child"],row["day"],row["parent"],revision))
        plan.store.db.execute("DELETE FROM cycles")
        for row in state["cycles"]:
            plan.store.db.execute("INSERT INTO cycles VALUES(?,?,?,?)",(row["child"],row["start"],row["cycle"],revision))
        now=plan.clock().isoformat()
        target_ids={row["id"] for row in state["special_events"]}
        current=[dict(row) for row in plan.store.db.execute("SELECT * FROM special_events")]
        for row in current:
            if row["id"] not in target_ids:
                plan.store.db.execute("UPDATE special_events SET removed=?,revision=revision+1 WHERE id=?",(now,row["id"]))
        for row in state["special_events"]:
            current=plan.store.db.execute("SELECT revision FROM special_events WHERE id=?",(row["id"],)).fetchone()
            next_revision=max(row["revision"],current["revision"] if current else 0)+1
            columns=("id","actor","start","end","title","created","removed","description","color")
            from pplan.colors import google_color
            values=tuple(google_color(row[key])["color"] if key=="color" else row[key] for key in columns)+(next_revision,)
            plan.store.db.execute("INSERT INTO special_events(id,actor,start,end,title,created,removed,description,color,revision) VALUES(?,?,?,?,?,?,?,?,?,?) "
                                  "ON CONFLICT(id) DO UPDATE SET actor=excluded.actor,start=excluded.start,end=excluded.end,title=excluded.title,"
                                  "created=excluded.created,removed=excluded.removed,description=excluded.description,color=excluded.color,revision=excluded.revision",values)
        for row in plan.store.db.execute("SELECT id FROM requests WHERE status='draft'").fetchall():
            plan._audit(row["id"],actor,"cancelled_by_restore")
        plan.store.db.execute("UPDATE requests SET status='cancelled' WHERE status='draft'")
        plan.store.db.execute("UPDATE restore_previews SET status='cancelled' WHERE status='draft'")
        plan.store.db.execute("UPDATE restore_previews SET status='confirmed' WHERE id=?",(preview_id,))
        plan.store.set("calendar_dirty","1")
        plan._audit("restore:"+preview_id,actor,"restored_version:"+str(preview["version_id"]))
        return record_change(plan,actor,"restored",str(preview["version_id"]))


def cancel_restore(plan, actor, preview_id):
    plan.authorize_owner(actor)
    with plan.store.transaction():
        row=plan.store.db.execute("SELECT status FROM restore_previews WHERE id=? AND actor=?",(preview_id,actor)).fetchone()
        if not row or row["status"]!='draft':
            raise PlanError("This restore preview is no longer available.")
        plan.store.db.execute("UPDATE restore_previews SET status='cancelled' WHERE id=?",(preview_id,))
