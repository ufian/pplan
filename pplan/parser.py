"""Explicit fallback syntax that remains available without Codex."""

from datetime import date, datetime, timedelta
import re
from pplan.core import PlanError


HELP = """Commands:
/calendar — open the interactive calendar Mini App
/schedule — current week through next month's calendar
/schedule YYYY-MM-DD YYYY-MM-DD — calendar and night totals for up to 700 days
/requests — recent changes and confirmation buttons
/overwrite <dates or pasted From/Until list> — replace selected days after confirmation
/special YYYY-MM-DD [YYYY-MM-DD] [title] — frame a date or one multi-day event
/special list — list marked events
/special remove <id> — remove an event frame
/improve <request> — request a change to the app
/improvements — app improvement status
/status — calendar sync status
/history — owner: list saved family-plan versions
/restore VERSION — owner: preview and confirm a restoration
/invite [parent] — owner: create a one-time second-parent code
/join CODE — connect using an invitation
/access — owner: show connected parent
/revoke — owner: revoke second-parent access and invitations
/whoami — your Telegram user ID

Send a natural-language request when AI is connected.
One message may contain several periods and a new repeating schedule.
The bot previews every action and applies the whole list only after confirmation.
Explicit format (works without AI):
Assign YYYY-MM-DD through YYYY-MM-DD to <parent> for both
Assign YYYY-MM-DD to <parent> for <child>
Overwrite YYYY-MM-DD through YYYY-MM-DD to <parent> for both

Use configured names or IDs. Dates are inclusive handover dates.
Each period ends at the next day's handover.
Review the exact times and children, then press Confirm. Only the request author can confirm their changes."""


def parse_request(text, config):
    aliases = {alias.casefold(): key for key, name in config.parents.items() for alias in (key, name)}
    child_aliases = {alias.casefold(): key for key, name in config.children.items() for alias in (key, name)}
    parent = "(" + "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True)) + ")"
    child = "(" + "|".join(re.escape(a) for a in ["both", *sorted(child_aliases, key=len, reverse=True)]) + ")"
    day = r"(\d{4}-\d{2}-\d{2})"
    pattern = r"(assign|overwrite)\s+" + day + r"(?:\s+through\s+" + day + r")?\s+to\s+" + parent + r"\s+for\s+" + child
    match = re.fullmatch(pattern, text.strip(), re.IGNORECASE)
    if not match:
        raise PlanError("Please use an exact request with dates, parent, and children.\n\n" + HELP)
    verb, start, end, name, children = match.groups()
    try:
        result = {"action": "change", "start": date.fromisoformat(start).isoformat(),
                "end": date.fromisoformat(end or start).isoformat(), "parent": aliases[name.casefold()],
                "children": list(config.children) if children.casefold() == "both" else [child_aliases[children.casefold()]],
                "question": None}
        if verb.casefold() == "overwrite":
            return {"action": "changes", "actions": [{"type": "overwrite", "start": result["start"],
                    "end": result["end"], "parent": result["parent"], "children": result["children"], "cycle": []}],
                    "start": None, "end": None, "parent": None, "children": [], "question": None}
        return result
    except ValueError as exc:
        raise PlanError("That date is invalid. Use YYYY-MM-DD.") from exc


def parse_preview_overwrite(text, config):
    """Read all copied From/Until blocks without asking a model to reinterpret dates."""
    lines = []
    for raw in text.splitlines():
        line = raw.strip().rstrip("\\").strip().replace("\\.", ".")
        if not line:
            continue
        if line.startswith("Timezone:"):
            if line.partition(":")[2].strip() != config.timezone:
                raise PlanError("The copied timezone must match the family's timezone.")
            continue
        if line.startswith("Change preview ·") or line.startswith("No changes applied."):
            continue
        if line == "Overwrites the selected days' existing assignments, including matching ones.":
            continue
        lines.append(line)
    if not lines or len(lines) % 3:
        raise PlanError("Paste complete numbered parent/children, From, and Until blocks after /overwrite.")
    parents = {alias.casefold(): key for key, name in config.parents.items() for alias in (key, name)}
    children = {alias.casefold(): key for key, name in config.children.items() for alias in (key, name)}
    actions = []
    for index in range(0, len(lines), 3):
        header = re.fullmatch(r"\d+\.\s+(.+?)\s+with\s+(.+)", lines[index], re.IGNORECASE)
        start_match = re.fullmatch(r"From:\s*(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2})", lines[index + 1], re.IGNORECASE)
        end_match = re.fullmatch(r"Until:\s*(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2})", lines[index + 2], re.IGNORECASE)
        if not header or not start_match or not end_match:
            raise PlanError("Each copied action must have its children and parent, From, and Until.")
        names = [name.strip().casefold() for name in header[1].split(",")]
        if header[2].casefold() not in parents or any(name not in children for name in names):
            raise PlanError("Use configured parent and child names in the copied list.")
        try:
            start, until = datetime.fromisoformat(start_match[1]), datetime.fromisoformat(end_match[1])
        except ValueError as exc:
            raise PlanError("Invalid date or time in copied action.") from exc
        if start != config.boundary(start.date()).replace(tzinfo=None) or until != config.boundary(until.date()).replace(tzinfo=None):
            raise PlanError("From and Until must use the configured local handover times.")
        if until <= start:
            raise PlanError("Until must be later than From.")
        actions.append({"type": "overwrite", "start": start.date().isoformat(),
                        "end": (until.date() - timedelta(days=1)).isoformat(), "parent": parents[header[2].casefold()],
                        "children": [children[name] for name in names], "cycle": []})
    return actions
