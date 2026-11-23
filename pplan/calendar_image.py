"""Local calendar rendering. Model output never draws or invents assignments."""

import calendar
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path

from pplan.core import PlanError

MONTHS = ("", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
COLORS = (("#17695e", "#e3f3ed"), ("#ad4c32", "#fcebe2"))
SPECIAL_COLOR = "#7845a6"


def default_calendar_range(today):
    next_month = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    return today, next_month.replace(day=calendar.monthrange(next_month.year, next_month.month)[1])


def calendar_pages(start, end):
    """Fill complete visible weeks, starting with the requested date's week."""
    if end < start or (end - start).days > 365:
        raise PlanError("Choose an ordered calendar range of at most 366 days.")
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


def calendar_bounds(start, end):
    pages = calendar_pages(start, end)
    return pages[0][1][0][0], pages[-1][1][-1][-1]


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


def image_layout(months, header=178):
    """Use the fewest month columns that keep height at most 1.5 × width."""
    margin, grid_width, gap, footer = 42, 7 * 148, 42, 76
    panel_heights = [120 + len(weeks) * 98 for _, weeks in months]
    for columns in range(1, len(months) + 1):
        row_heights = [max(panel_heights[i:i + columns]) for i in range(0, len(months), columns)]
        width = margin * 2 + columns * grid_width + (columns - 1) * gap
        height = header + sum(row_heights) + footer
        if height * 2 <= width * 3:
            break
    positions = []
    for index, (month, weeks) in enumerate(months):
        positions.append((margin + (index % columns) * (grid_width + gap),
                          header + sum(row_heights[:index // columns]), month, weeks))
    return {"width": width, "height": height, "columns": columns, "panels": positions}


def render_calendar(plan, start: date, end: date):
    from PIL import Image, ImageDraw, ImageFont

    months = calendar_pages(start, end)
    visible_start, visible_end = calendar_bounds(start, end)
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
    layout = image_layout(months, header)
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

    label(margin, 28, "Parenting Plan", 34, bold=True)
    times = {*plan.config.school_handover_times.values(), plan.config.non_school_handover_time}
    time_note = f"   ·   Handover {next(iter(times))}" if len(times) == 1 and plan.config.handover_confirmed else ""
    label(margin, 77, f"{visible_start.isoformat()}  →  {visible_end.isoformat()}   ·   {plan.config.timezone}{time_note}", 17, color="#526775", max_width=width - margin * 2)
    parent_colors = {parent: COLORS[index] for index, parent in enumerate(plan.config.parents)}
    legend_width = (width - margin * 2 - 18) // 2
    for index, (parent, name) in enumerate(plan.config.parents.items()):
        x = margin + index * (legend_width + 18)
        foreground, background = parent_colors[parent]
        draw.rounded_rectangle((x, 114, x + legend_width, 154), radius=12, fill=background)
        draw.ellipse((x + 15, 128, x + 27, 140), fill=foreground)
        label(x + 39, 122, name, 18, foreground, bold=True, max_width=legend_width - 53)
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
        label(panel_x, y + 6, f"{MONTHS[month.month]} {month.year}", 25, bold=True)
        for column, day_name in enumerate(WEEKDAYS):
            label(panel_x + column * cell + 12, y + 56, day_name, 13, "#657b89", bold=True)
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
                label(x + 12, box_y + 7, str(day.day), 19, "#162d3d" if selected else "#8fa0ab", bold=True)
                if not in_month:
                    label(x + 43, box_y + 11, MONTHS[day.month][:3], 11, "#657b89")
                if not selected:
                    continue
                for group_index, group in enumerate(groups):
                    parent = group["after"]
                    foreground, background = parent_colors[parent]
                    if together:
                        if group["before"] == parent:
                            label(x + 15, box_y + 48, plan.config.parents[parent], 15, foreground, bold=True, max_width=cell - 34)
                    else:
                        chip_y = box_y + 34 + group_index * 25
                        child_name = plan.config.children[group["children"][0]]
                        label(x + 10, chip_y + 3, child_name, 11, max_width=51)
                        chip = (x + 66, chip_y, x + cell - 13, chip_y + 21)
                        if group["before"] != parent:
                            split_fill(chip, group["before"], parent, 5)
                        else:
                            draw.rounded_rectangle(chip, radius=5, fill=background)
                            label(x + 70, chip_y + 3, plan.config.parents[parent], 11, foreground, max_width=cell - 88)
        for event in events:
            event_start, event_end = date.fromisoformat(event["start"]), date.fromisoformat(event["end"])
            for week_index, week in enumerate(weeks):
                columns = [index for index, day in enumerate(week) if event_start <= day <= event_end]
                if columns:
                    box_y = top + week_index * row
                    bounds = (panel_x + columns[0] * cell, box_y - 2,
                              panel_x + (columns[-1] + 1) * cell - 3, box_y + row - 5)
                    draw.rounded_rectangle(bounds, radius=12, outline=SPECIAL_COLOR, width=3)
    label(margin, height - 60, "Split colors: left before handover, right after handover.", 15, "#526775")
    if events:
        label(margin, height - 33, "Purple frames mark special dates and continuous multi-day events.", 14, SPECIAL_COLOR)
    else:
        label(margin, height - 33, "Children are grouped together unless their parent assignments differ.", 14, "#657b89")
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
