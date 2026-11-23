"""Shared Google Calendar event palette, independent of provider credentials.

Standard color IDs use the Calendar v3 event palette. Google clients can theme
these swatches differently; colorId is the portable identity, not a custom label.
"""
from colorsys import rgb_to_hsv

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
