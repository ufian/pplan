"""Shared Google Calendar event palette, independent of provider credentials.

Standard color IDs use the Calendar v3 event palette. Google clients can theme
these swatches differently; colorId is the portable identity, not a custom label.
"""
from colorsys import rgb_to_hsv
import json
import re
from uuid import UUID

PALETTE = tuple({"id": str(i), "color": color, "name": name} for i, color, name in (
    (1, "#a4bdfc", "Lavender"), (2, "#7ae7bf", "Sage"),
    (3, "#dbadff", "Grape"), (4, "#ff887c", "Flamingo"),
    (5, "#fbd75b", "Banana"), (6, "#ffb878", "Tangerine"),
    (7, "#46d6db", "Peacock"), (8, "#e1e1e1", "Graphite"),
    (9, "#5484ed", "Blueberry"), (10, "#51b749", "Basil"),
    (11, "#dc2127", "Tomato")))
BY_ID = {item["id"]: item for item in PALETTE}
BY_COLOR = {item["color"]: item for item in PALETTE}
DEFAULT_SPECIAL = BY_ID["3"]["color"]
PARENT_IDS = ("10", "6")
LEGACY = {"#7845a6": "3", "#2563eb": "9", "#17856b": "10",
          "#ca5135": "6", "#cf3b70": "4", "#a66a06": "5"}


def _hsv(color):
    return rgb_to_hsv(*(int(color[i:i+2], 16)/255 for i in (1, 3, 5)))


def google_color(color):
    """Keep exact palette colors; map legacy/custom shades preserving hue."""
    color = color.lower()
    if color in BY_COLOR:
        return BY_COLOR[color]
    if color in LEGACY:
        return BY_ID[LEGACY[color]]
    h, s, v = _hsv(color)
    def distance(item):
        ih, ins, iv = _hsv(item["color"])
        hue = min(abs(h-ih), 1-abs(h-ih))
        return 8 * hue ** 2 * min(s, ins) + (s-ins) ** 2 + .25 * (v-iv) ** 2
    return min(PALETTE, key=distance)


def parent_palette(parents):
    return {parent: BY_ID[PARENT_IDS[i]] for i, parent in enumerate(parents)}


def align_special_colors(plan, actor):
    """Migrate annotation colors with one history entry; never edit assignments."""
    plan.authorize_owner(actor)
    with plan.store.transaction():
        changed = []
        for event in plan.special_events():
            color = google_color(event["color"])["color"]
            if color != event["color"]:
                plan.store.db.execute("UPDATE special_events SET color=?,revision=revision+1 WHERE id=?", (color, event["id"]))
                changed.append(event["id"])
        if changed:
            plan._record_version(actor, "palette_aligned", str(len(changed)))
        return len(changed)


def _validate_parent_selection(parents, selection):
    if not isinstance(selection, dict) or set(selection) != set(parents):
        raise ValueError("Select exactly one color for each configured parent.")
    result = {}
    for parent, item in selection.items():
        if not isinstance(item, dict) or set(item) != {"color", "label_id"}:
            raise ValueError("A parent color requires a Google label and its HEX color.")
        color, label = item["color"], item["label_id"]
        if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise ValueError("Invalid parent HEX color.")
        if not isinstance(label, str) or str(UUID(label)) != label:
            raise ValueError("Invalid Google color label ID.")
        result[parent] = {"color": color.lower(), "label_id": label}
    if len({item["color"] for item in result.values()}) != len(parents):
        raise ValueError("Choose distinct parent colors.")
    return result


def _base_parent_palette(plan):
    """Read per-family selected Google colors; defaults apply until explicitly set."""
    raw = plan.store.get("parent_palette")
    if raw is None:
        return parent_palette(plan.config.parents)
    from pplan.core import PlanError
    try:
        selected = json.loads(raw)
        if selected["calendar_id"] != plan.store.get("google_calendar_id"):
            raise ValueError("Parent colors belong to a different calendar.")
        return _validate_parent_selection(plan.config.parents, selected["parents"])
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise PlanError("Invalid stored parent colors. Reapply colors from the connected Google calendar.") from exc


def set_parent_palette(plan, actor, selection):
    """Explicit owner-authorized preference update; schedule and previews stay intact."""
    from pplan.core import PlanError
    plan.authorize_owner(actor)
    try:
        colors = _validate_parent_selection(plan.config.parents, selection)
    except (ValueError, TypeError, AttributeError) as exc:
        raise PlanError(str(exc)) from None
    with plan.store.transaction():
        calendar_id = plan.store.get("google_calendar_id")
        if not calendar_id:
            raise PlanError("Connect Google Calendar before selecting its color labels.")
        value = json.dumps({"calendar_id": calendar_id, "parents": colors}, sort_keys=True)
        if plan.store.get("parent_palette") == value:
            return False
        plan.store.set("parent_palette", value)
        plan.store.set("calendar_dirty", "1")
        plan._audit("parent-colors", actor, "parent_colors_updated")
        return True


def plan_parent_palette(plan):
    colors = _base_parent_palette(plan)
    from pplan.core import PlanError
    for row in plan.store.db.execute("SELECT * FROM parent_color_preferences"):
        if row["parent"] not in colors or row["calendar_id"] != (plan.store.get("google_calendar_id") or ""):
            raise PlanError("Stored parent colors do not match this calendar.")
        if row["label_id"]:
            item = _validate_parent_selection({row["parent"]: ""}, {row["parent"]: {"color": row["color"], "label_id": row["label_id"]}})[row["parent"]]
        elif row["color_id"] in BY_ID and BY_ID[row["color_id"]]["color"] == row["color"]:
            item = BY_ID[row["color_id"]]
        else:
            raise PlanError("Invalid stored parent color.")
        colors[row["parent"]] = item
    return colors


def parent_palette_version(plan):
    return (plan.store.get("parent_palette"), tuple(tuple(r) for r in plan.store.db.execute("SELECT parent,revision FROM parent_color_preferences ORDER BY parent")))


def parent_color_options(plan):
    """Use cached labels from the connected calendar; no Google key in the Mini App."""
    raw = plan.store.get("google_parent_palette")
    if raw:
        cached = json.loads(raw)
        if cached.get("calendar_id") == plan.store.get("google_calendar_id"):
            return cached["colors"]
    return [{**item, "key": "legacy:"+item["id"]} for item in PALETTE]


def own_color_data(plan, actor):
    from pplan.access import parent_for_actor
    from pplan.core import PlanError
    parent = parent_for_actor(plan, actor)
    if parent is None:
        raise PlanError("The owner must configure their parent identity before selecting a personal color.")
    colors = plan_parent_palette(plan)
    current = colors[parent]
    row = plan.store.db.execute("SELECT revision FROM parent_color_preferences WHERE parent=?", (parent,)).fetchone()
    return {"parent": parent, "name": plan.config.parents[parent],
            "choice": "label:"+current["label_id"] if "label_id" in current else "legacy:"+current["id"],
            "revision": row["revision"] if row else 0,
            "options": parent_color_options(plan), "parent_colors": {p:c["color"] for p,c in colors.items()},
            "sync_pending": plan.store.get("calendar_dirty")=="1",
            "sync_error": plan.store.get("calendar_error") not in (None,"none")}


def update_own_color(plan, actor, choice, expected_revision):
    from pplan.access import parent_for_actor
    from pplan.core import PlanError, SpecialEventConflict
    if not isinstance(choice, str) or len(choice)>64 or type(expected_revision) is not int or expected_revision<0:
        raise PlanError("Invalid parent color selection or revision.")
    with plan.store.transaction():
        parent = parent_for_actor(plan, actor)
        if parent is None:
            raise PlanError("Configure the owner's parent identity first.")
        option = next((c for c in parent_color_options(plan) if c["key"] == choice), None)
        if option is None:
            raise PlanError("Choose a color available in this calendar.")
        current = own_color_data(plan, actor)
        if current["choice"] == choice:
            return current
        if current["revision"] != expected_revision:
            raise SpecialEventConflict("Your color changed elsewhere. Reopen My color and try again.")
        if any(color==option["color"] for p,color in current["parent_colors"].items() if p!=parent):
            raise PlanError("Choose a different color from the other parent.")
        plan.store.db.execute("INSERT INTO parent_color_preferences(parent,calendar_id,color,label_id,color_id,revision) VALUES(?,?,?,?,?,?) "
                              "ON CONFLICT(parent) DO UPDATE SET color=excluded.color,label_id=excluded.label_id,color_id=excluded.color_id,revision=excluded.revision",
                              (parent,plan.store.get("google_calendar_id") or "",option["color"],option.get("label_id"),option.get("id"),expected_revision+1))
        plan._audit("parent-color:"+parent,actor,"parent_color_updated")
        return own_color_data(plan, actor)
