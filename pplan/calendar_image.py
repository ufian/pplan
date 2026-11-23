"""Local calendar rendering. Model output never draws or invents assignments."""

import calendar
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path

from pplan.core import PlanError
from pplan.colors import BY_ID, DEFAULT_SPECIAL, PARENT_IDS

MONTHS = ("", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
COLORS = tuple(("#162d3d", BY_ID[key]["color"]) for key in PARENT_IDS)
SPECIAL_COLOR = DEFAULT_SPECIAL
MAX_CALENDAR_DAYS = 700
NIGHTS_BLOCK_HEIGHT = 212
NIGHTS_BLOCK_GAP = 16
NIGHTS_SECTION_HEIGHT = NIGHTS_BLOCK_HEIGHT + 74


def default_calendar_range(today):
    next_month = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    return today, next_month.replace(day=calendar.monthrange(next_month.year, next_month.month)[1])


def calendar_pages(start, end):
    """Fill complete visible weeks, starting with the requested date's week."""
    if end < start or (end - start).days >= MAX_CALENDAR_DAYS:
        raise PlanError(f"Choose an ordered calendar range of at most {MAX_CALENDAR_DAYS} days.")
    first_week = start - timedelta(days=start.weekday())
    pages, seen = [], set()
    current = start.replace(day=1)
    while current <= end:
        weeks = []
        for week in calendar.Calendar(firstweekday=0).monthdatescalendar(current.year, current.month):
            if week[0] >= first_week and week[0] not in seen:
                weeks.append(week)
                seen.add(week[0])
        if weeks:
            pages.append((current, weeks))
        current = date(current.year + (current.month == 12), current.month % 12 + 1, 1)
    return pages


def assignments_for_day(plan, day):
    groups = {}
    for child in plan.config.children:
        parent = plan.assignment(child, day)["parent"]
        groups.setdefault(parent, []).append(child)
    return [{"parent": parent, "children": children} for parent, children in groups.items()]


def day_segments(plan, day):
    """Group children by the owners before and after the day's handover."""
    groups = {}
    previous = day - timedelta(days=1)
    for child in plan.config.children:
        before = plan.assignment(child, previous)["parent"] if previous >= plan.config.anchor_date else None
        after = plan.assignment(child, day)["parent"]
        groups.setdefault((before, after), []).append(child)
    return [{"before": before, "after": after, "children": children}
            for (before, after), children in groups.items()]


def image_layout(months, header=178, *, has_events=False):
    """Use the fewest month columns that keep height at most 1.5 × width."""
    margin, grid_width, gap = 42, 7 * 148, 42
    footer = NIGHTS_SECTION_HEIGHT + (36 if has_events else 0)
    for columns in range(1, len(months) + 1):
        width = margin * 2 + columns * grid_width + (columns - 1) * gap
        months_per_column, extra = divmod(len(months), columns)
        positions = []
        offset, bottom = 0, header
        for column in range(columns):
            count = months_per_column + (column < extra)
            x = margin + column * (grid_width + gap)
            y = header
            for month, weeks in months[offset:offset + count]:
                positions.append((x, y, month, weeks))
                y += 120 + len(weeks) * 98
            offset += count
            bottom = max(bottom, y)
        height = bottom + footer
        if height * 2 <= width * 3:
            break
    return {"width": width, "height": height, "columns": columns, "panels": positions}


def render_calendar(plan, start: date, end: date):
    from PIL import Image, ImageDraw, ImageFont

    months = calendar_pages(start, end)
    visible_start, visible_end = months[0][1][0][0], months[-1][1][-1][-1]
    events = plan.special_events(visible_start, visible_end)

    font_roots = [Path("/usr/share/fonts/truetype/dejavu"), Path("/System/Library/Fonts/Supplemental")]
    fonts = {}
    def font(size, bold=False):
        key = size, bold
        if key not in fonts:
            names = ("DejaVuSans-Bold.ttf", "Arial Bold.ttf") if bold else ("DejaVuSans.ttf", "Arial.ttf")
            for root in font_roots:
                for name in names:
                    if (root / name).exists():
                        fonts[key] = ImageFont.truetype(str(root / name), size)
                        return fonts[key]
            fonts[key] = ImageFont.load_default(size=size)
        return fonts[key]

    margin, cell, row = 42, 148, 98
    header = 216 if not plan.config.handover_confirmed else 178
    layout = image_layout(months, header, has_events=bool(events))
    width, height = layout["width"], layout["height"]
    image = Image.new("RGB", (width, height), "#f3f6f8")
    draw = ImageDraw.Draw(image)

    def label(x, y, text, size=16, color="#162d3d", bold=False, max_width=None):
        chosen = font(size, bold)
        if max_width is not None:
            while size > 11 and draw.textlength(text, font=chosen) > max_width:
                size -= 1
                chosen = font(size, bold)
            while text and draw.textlength(text, font=chosen) > max_width:
                text = text[:-2].rstrip("…") + "…" if len(text) > 2 else ""
        draw.text((x, y), text, font=chosen, fill=color)

    label(margin, 22, "Parenting Plan", 42, bold=True)
    times = {*plan.config.school_handover_times.values(), plan.config.non_school_handover_time}
    time_note = f"   ·   Handover {next(iter(times))}" if len(times) == 1 and plan.config.handover_confirmed else ""
    label(margin, 77, f"{visible_start.isoformat()}  →  {visible_end.isoformat()}{time_note}", 22, color="#3f5360", max_width=width - margin * 2)
    parent_colors = {parent: COLORS[index] for index, parent in enumerate(plan.config.parents)}
    legend_width = (width - margin * 2 - 18) // 2
    for index, (parent, name) in enumerate(plan.config.parents.items()):
        x = margin + index * (legend_width + 18)
        foreground, background = parent_colors[parent]
        draw.rounded_rectangle((x, 114, x + legend_width, 154), radius=12, fill=background)
        draw.ellipse((x + 15, 128, x + 27, 140), fill=foreground)
        label(x + 39, 118, name, 24, foreground, bold=True, max_width=legend_width - 53)
    if not plan.config.handover_confirmed:
        draw.rounded_rectangle((margin, 167, width - margin, 200), radius=8, fill="#fff2ce")
        label(margin + 12, 175, "Setup pending: handover times are examples until confirmed.", 14, "#725519")

    def parent_background(parent):
        return parent_colors[parent][1] if parent is not None else "#eaf0f3"

    def split_fill(bounds, before, after, radius):
        x0, y0, x1, y1 = bounds
        size = (x1 - x0 + 1, y1 - y0 + 1)
        patch = Image.new("RGB", size, parent_background(before))
        ImageDraw.Draw(patch).rectangle((size[0] // 2, 0, size[0] - 1, size[1] - 1), fill=parent_background(after))
        mask = Image.new("L", size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
        image.paste(patch, (x0, y0), mask)

    for panel_x, y, month, weeks in layout["panels"]:
        label(panel_x, y + 3, f"{MONTHS[month.month]} {month.year}", 34, bold=True)
        for column, day_name in enumerate(WEEKDAYS):
            label(panel_x + column * cell + 12, y + 54, day_name, 19, "#455c69", bold=True)
        top = y + 88
        for week_index, week in enumerate(weeks):
            for column, day in enumerate(week):
                x, box_y = panel_x + column * cell, top + week_index * row
                in_month = day.month == month.month
                selected = day >= plan.config.anchor_date
                groups = day_segments(plan, day) if selected else []
                together = len(groups) == 1
                fill = parent_background(groups[0]["after"]) if together else "#ffffff" if selected else "#eaf0f3"
                border = "#3575b6" if selected and day == plan.today() else "#dfe7ec"
                bounds = (x + 2, box_y, x + cell - 5, box_y + row - 7)
                draw.rounded_rectangle(bounds, radius=10, fill=fill)
                if together and groups[0]["before"] != groups[0]["after"]:
                    split_fill(bounds, groups[0]["before"], groups[0]["after"], 10)
                draw.rounded_rectangle(bounds, radius=10, outline=border, width=2)
                label(x + 12, box_y + 1, str(day.day), 36, "#162d3d" if selected else "#657b89", bold=True)
                if not in_month:
                    label(x + 70, box_y + 14, MONTHS[day.month][:3], 18, "#455c69", bold=True)
                if not selected:
                    continue
                for group_index, group in enumerate(groups):
                    parent = group["after"]
                    foreground, background = parent_colors[parent]
                    if together:
                        if group["before"] == parent:
                            label(x + 12, box_y + 50, plan.config.parents[parent], 23, foreground, bold=True, max_width=cell - 26)
                    else:
                        chip_y = box_y + 43 + group_index * 22
                        child_name = plan.config.children[group["children"][0]]
                        label(x + 10, chip_y, child_name, 16, max_width=60)
                        chip = (x + 74, chip_y, x + cell - 9, chip_y + 19)
                        if group["before"] != parent:
                            split_fill(chip, group["before"], parent, 5)
                        else:
                            draw.rounded_rectangle(chip, radius=5, fill=background)
                            label(x + 78, chip_y, plan.config.parents[parent], 16, foreground, max_width=cell - 90)
        for event in events:
            event_start, event_end = date.fromisoformat(event["start"]), date.fromisoformat(event["end"])
            for week_index, week in enumerate(weeks):
                columns = [index for index, day in enumerate(week) if event_start <= day <= event_end]
                if columns:
                    box_y = top + week_index * row
                    bounds = (panel_x + columns[0] * cell, box_y - 2,
                              panel_x + (columns[-1] + 1) * cell - 3, box_y + row - 5)
                    draw.rounded_rectangle(bounds, radius=12, outline=event["color"], width=3)
    summary_top = height - NIGHTS_SECTION_HEIGHT
    if events:
        label(margin, summary_top - 36, "Colored frames mark special events.", 21, SPECIAL_COLOR)

    summary_width = width - 2 * margin
    label(margin, summary_top, "Scheduled nights by child", 36, bold=True)
    block_width = (summary_width - NIGHTS_BLOCK_GAP) // 2
    column_width = (block_width - 36) // 3
    block_top = summary_top + 50
    periods = ((start, end), (date(end.year, 1, 1), end))
    for index, (period_start, period_end) in enumerate(periods):
        totals = plan.overnight_totals(period_start, period_end)
        block_x = margin + index * (block_width + NIGHTS_BLOCK_GAP)
        content_x = block_x + 18
        draw.rounded_rectangle((block_x, block_top, block_x + block_width, block_top + NIGHTS_BLOCK_HEIGHT),
                               radius=16, fill="#ffffff", outline="#dfe7ec", width=2)
        label(content_x, block_top + 12, f"{period_start.isoformat()} → {period_end.isoformat()}",
              32, bold=True, max_width=block_width - 36)
        for parent_index, (parent, parent_name) in enumerate(plan.config.parents.items(), 1):
            parent_x = content_x + parent_index * column_width
            color, fill = parent_colors[parent]
            draw.rounded_rectangle((parent_x, block_top + 51, parent_x + column_width - 8, block_top + 194),
                                   radius=10, fill=fill)
            label(parent_x + 10, block_top + 61, parent_name, 23, color, bold=True, max_width=column_width - 28)
        for child_index, (child, child_name) in enumerate(plan.config.children.items()):
            row_y = block_top + 95 + child_index * 48
            label(content_x, row_y + 5, child_name, 24, bold=True, max_width=column_width - 12)
            for parent_index, parent in enumerate(plan.config.parents, 1):
                x = content_x + parent_index * column_width + 10
                label(x, row_y, str(totals[child][parent]), 32, parent_colors[parent][0], bold=True)
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
