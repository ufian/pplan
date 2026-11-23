"""Owner-issued, one-time invitations to the second parent of this single-family plan."""

from datetime import datetime, timedelta
import hashlib
import re
import secrets
from uuid import uuid4

from pplan.core import PlanError

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
INVALID_INVITE = "This invitation code is invalid, expired, or already used. Ask the owner for a new code."


def authorize_owner(plan, actor):
    if type(actor) is not int or actor != plan.config.operator_telegram_id:
        raise PlanError("Only the bot owner can manage invitations, access, and app improvements.")


def member(plan, actor):
    if type(actor) is not int or actor <= 0:
        return None
    row = plan.store.db.execute("SELECT * FROM family_members WHERE telegram_id=? AND revoked IS NULL", (actor,)).fetchone()
    return dict(row) if row else None


def has_access(plan, actor):
    return type(actor) is int and (actor == plan.config.operator_telegram_id or member(plan, actor) is not None)


def authorize(plan, actor):
    if not has_access(plan, actor):
        raise PlanError("You are not authorized to manage this plan.")


def parent_for_actor(plan, actor):
    authorize(plan, actor)
    if actor == plan.config.operator_telegram_id:
        parent = plan.config.operator_parent_id or plan.store.get("operator_parent_id")
        return parent if parent in plan.config.parents else None
    return member(plan, actor)["parent_id"]


def create_invite(plan, actor, parent_id=None):
    authorize_owner(plan, actor)
    owner_parent = parent_for_actor(plan, actor)
    if parent_id is None and owner_parent:
        parent_id = next(p for p in plan.config.parents if p != owner_parent)
    if parent_id not in plan.config.parents or parent_id == owner_parent:
        raise PlanError("Choose the second parent's configured name or ID with /invite <parent>.")
    raw = "".join(secrets.choice(ALPHABET) for _ in range(16))
    now = plan.clock()
    expires = now + timedelta(hours=24)
    with plan.store.transaction():
        authorize_owner(plan, actor)
        if plan.store.db.execute("SELECT 1 FROM family_members WHERE revoked IS NULL").fetchone():
            raise PlanError("A second parent is already connected. Use /revoke before inviting a replacement.")
        plan.store.db.execute("UPDATE parent_invites SET revoked=? WHERE used IS NULL AND revoked IS NULL", (now.isoformat(),))
        invite_id = uuid4().hex
        plan.store.db.execute("INSERT INTO parent_invites(id,code_hash,parent_id,created,expires) VALUES(?,?,?,?,?)",
                              (invite_id, hashlib.sha256(raw.encode()).hexdigest(), parent_id, now.isoformat(), expires.isoformat()))
        plan._audit("invite:" + invite_id, actor, "invite_created")
    return {"code": "-".join(raw[i:i+4] for i in range(0,16,4)), "parent_id": parent_id, "expires": expires.isoformat()}


def redeem_invite(plan, actor, code):
    if type(actor) is not int or actor <= 0:
        raise PlanError(INVALID_INVITE)
    if actor == plan.config.operator_telegram_id:
        raise PlanError("You already own this plan. Share the code with the second parent.")
    normalized = re.sub(r"[-\s]", "", code.upper()) if isinstance(code, str) and len(code) <= 128 else ""
    code_hash = hashlib.sha256(normalized.encode()).hexdigest()
    now, error, result = plan.clock(), None, None
    with plan.store.transaction():
        attempts = plan.store.db.execute("SELECT * FROM invite_attempts WHERE actor=?", (actor,)).fetchone()
        window = datetime.fromisoformat(attempts["window_start"]) if attempts else now
        failures = attempts["failures"] if attempts and now - window < timedelta(minutes=15) else 0
        if failures >= 5:
            error = "Too many invitation attempts. Try again in 15 minutes."
        else:
            invite = (plan.store.db.execute("SELECT * FROM parent_invites WHERE code_hash=?", (code_hash,)).fetchone()
                      if re.fullmatch(r"[A-HJ-NP-Z2-9]{16}", normalized) else None)
            existing = member(plan, actor)
            if invite and invite["used_by"] == actor and not invite["revoked"] and existing and existing["parent_id"] == invite["parent_id"]:
                result = {**existing, "new": False}
            elif (invite and not invite["used"] and not invite["revoked"] and now < datetime.fromisoformat(invite["expires"])
                  and not plan.store.db.execute("SELECT 1 FROM family_members WHERE revoked IS NULL").fetchone()
                  and invite["parent_id"] in plan.config.parents and invite["parent_id"] != parent_for_actor(plan, plan.config.operator_telegram_id)):
                plan.store.db.execute("INSERT INTO family_members(telegram_id,parent_id,joined,revoked) VALUES(?,?,?,NULL) "
                                      "ON CONFLICT(telegram_id) DO UPDATE SET parent_id=excluded.parent_id,joined=excluded.joined,revoked=NULL",
                                      (actor, invite["parent_id"], now.isoformat()))
                plan.store.db.execute("UPDATE parent_invites SET used_by=?,used=? WHERE id=?", (actor, now.isoformat(), invite["id"]))
                plan._audit("invite:" + invite["id"], actor, "invite_redeemed")
                result = {**member(plan, actor), "new": True}
            else:
                if not attempts or now - window >= timedelta(minutes=15):
                    window = now
                plan.store.db.execute("INSERT INTO invite_attempts VALUES(?,?,?) ON CONFLICT(actor) "
                                      "DO UPDATE SET window_start=excluded.window_start,failures=excluded.failures",
                                      (actor, window.isoformat(), failures + 1))
                error = INVALID_INVITE
        if result:
            plan.store.db.execute("DELETE FROM invite_attempts WHERE actor=?", (actor,))
    # Raise after committing the attempt counter; failed standalone calls must not reset throttling.
    if error:
        raise PlanError(error)
    return result


def revoke_access(plan, actor):
    authorize_owner(plan, actor)
    now = plan.clock().isoformat()
    with plan.store.transaction():
        authorize_owner(plan, actor)
        plan.store.db.execute("UPDATE family_members SET revoked=? WHERE revoked IS NULL", (now,))
        plan.store.db.execute("UPDATE parent_invites SET revoked=? WHERE revoked IS NULL", (now,))
        plan._audit("access:" + uuid4().hex, actor, "second_parent_revoked")
