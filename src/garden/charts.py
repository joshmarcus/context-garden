"""Server-side SVG charts, no JS: a burn-up of tasks over time and a per-tier bar chart.

Rules followed: one scale per chart, thin marks, recessive grid, direct labels on the
endpoints only, chart text in theme ink (CSS variables), native <title> hover on marks.
Colors come from the page's CSS tokens (--viz-1, --viz-ord-1..3, --ink, --muted, --line).
"""

from __future__ import annotations

import datetime as dt
from typing import Any


def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s)


def burnup_svg(events: list[dict[str, Any]], total_tasks: int, width: int = 640, height: int = 220,
               done_ids: set[str] | None = None) -> str:
    """Cumulative tasks done over time (single series, area) against the total (reference line).

    With `done_ids`, only tasks that are done *now* count, each at its latest done transition, so the
    chart agrees with the task table even when a task was reopened after a merge.
    """
    done_events = [e for e in events if e.get("kind") == "transition" and e.get("to") == "done"]
    if done_ids is None:
        done = sorted(e["at"] for e in done_events)
    else:
        latest: dict[str, str] = {}
        for e in done_events:
            if e.get("task") in done_ids:
                latest[e["task"]] = max(latest.get(e["task"], ""), e["at"])
        done = sorted(latest.values())
    opened = sorted(e["at"] for e in events if e.get("kind") == "pr_opened")
    if not done and not opened:
        return '<div class="empty">No merges yet. The burn-up starts with the first merged PR.</div>'
    starts = [e["at"] for e in events if e.get("kind") == "dispatch"]
    t0 = _ts(min(starts + opened + done))
    t1 = max(_ts(max(done + opened)), dt.datetime.now(dt.UTC))
    span = max((t1 - t0).total_seconds(), 3600)
    ml, mr, mt, mb = 36, 16, 30, 28
    pw, ph = width - ml - mr, height - mt - mb
    ymax = max(total_tasks, len(done), 1)

    def x(t: dt.datetime) -> float:
        return ml + pw * (t - t0).total_seconds() / span

    def y(v: float) -> float:
        return mt + ph * (1 - v / ymax)

    def series(times: list[str]) -> list[tuple[float, float]]:
        pts = [(x(t0), y(0))]
        for i, at in enumerate(times, 1):
            t = _ts(at)
            pts.append((x(t), y(i - 1)))
            pts.append((x(t), y(i)))
        pts.append((x(t1), y(len(times))))
        return pts

    out = [f'<svg class="chart" viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="Tasks merged over time">']
    ticks = sorted({0, ymax // 2, ymax})
    for v in ticks:
        out.append(f'<line x1="{ml}" x2="{width - mr}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="var(--line)" stroke-width="1"/>')
        out.append(f'<text x="{ml - 6}" y="{y(v) + 4:.1f}" text-anchor="end" fill="var(--muted)" font-size="11">{v}</text>')
    # total as a reference line
    out.append(f'<line x1="{ml}" x2="{width - mr}" y1="{y(total_tasks):.1f}" y2="{y(total_tasks):.1f}" stroke="var(--muted)" stroke-width="1" stroke-dasharray="4,4"/>')
    out.append(f'<text x="{width - mr}" y="{y(total_tasks) + 13:.1f}" text-anchor="end" fill="var(--muted)" font-size="11">{total_tasks} in scope</text>')
    # PRs opened (secondary, thin line)
    if opened:
        pts = series(opened)
        out.append('<polyline fill="none" stroke="var(--viz-2)" stroke-width="1.5" stroke-dasharray="2,3" points="' + " ".join(f"{a:.1f},{b:.1f}" for a, b in pts) + '"/>')
    # merged (primary, area + line)
    if done:
        pts = series(done)
        area = f"M {ml},{y(0):.1f} " + " ".join(f"L {a:.1f},{b:.1f}" for a, b in pts) + f" L {x(t1):.1f},{y(0):.1f} Z"
        out.append(f'<path d="{area}" fill="var(--viz-1)" opacity="0.12"/>')
        out.append('<polyline fill="none" stroke="var(--viz-1)" stroke-width="2" stroke-linejoin="round" points="' + " ".join(f"{a:.1f},{b:.1f}" for a, b in pts) + '"/>')
        ex, ey = pts[-1]
        out.append(f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="4" fill="var(--viz-1)" stroke="var(--surface)" stroke-width="2"><title>{len(done)} merged</title></circle>')
        out.append(f'<text x="{ex - 8:.1f}" y="{ey - 8:.1f}" text-anchor="end" fill="var(--ink)" font-size="12" font-weight="600">{len(done)} merged</text>')
        for i, at in enumerate(done, 1):
            t = _ts(at)
            out.append(f'<circle cx="{x(t):.1f}" cy="{y(i):.1f}" r="7" fill="transparent"><title>{_esc(at[:16])}: {i} merged</title></circle>')
    # x labels: start and end
    out.append(f'<text x="{ml}" y="{height - 8}" fill="var(--muted)" font-size="11">{t0.strftime("%b %d")}</text>')
    out.append(f'<text x="{width - mr}" y="{height - 8}" text-anchor="end" fill="var(--muted)" font-size="11">{t1.strftime("%b %d %H:%M")}</text>')
    # legend: its own row above the plot
    out.append(f'<g font-size="11" fill="var(--muted)"><rect x="{ml}" y="8" width="12" height="3" fill="var(--viz-1)"/><text x="{ml + 16}" y="12">merged ({len(done)})</text>'
               f'<rect x="{ml + 96}" y="8" width="12" height="3" fill="var(--viz-2)"/><text x="{ml + 112}" y="12">PRs opened ({len(opened)})</text></g>')
    out.append("</svg>")
    return "\n".join(out)


def tier_bars_svg(rows: list[dict[str, Any]], value: str = "cost_usd", fmt: str = "${:.2f}", width: int = 360, height: int = 150) -> str:
    """Ordered categories (easy < medium < hard) on an ordinal ramp; one scale; direct labels."""
    tiers = [r for r in rows if r.get("tasks")]
    if not tiers:
        return '<div class="empty">No runs recorded yet.</div>'
    vmax = max(float(r.get(value) or 0) for r in tiers) or 1.0
    ml, mr, mt, mb = 64, 60, 10, 10
    pw = width - ml - mr
    bh = 22
    gap = 10
    h = mt + mb + len(tiers) * (bh + gap) - gap
    out = [f'<svg class="chart" viewBox="0 0 {width} {h}" width="100%" role="img" aria-label="By difficulty tier">']
    for i, r in enumerate(tiers):
        v = float(r.get(value) or 0)
        w = pw * v / vmax
        yy = mt + i * (bh + gap)
        slot = {"easy": 1, "medium": 2, "hard": 3}.get(str(r.get("tier")), 2)
        out.append(f'<text x="{ml - 8}" y="{yy + bh / 2 + 4:.1f}" text-anchor="end" fill="var(--ink)" font-size="12">{_esc(r.get("tier", ""))}</text>')
        out.append(f'<rect x="{ml}" y="{yy}" width="{max(w, 2):.1f}" height="{bh}" rx="3" fill="var(--viz-ord-{slot})"><title>{_esc(r.get("tier", ""))}: {fmt.format(v)} over {r.get("tasks")} task(s)</title></rect>')
        label = fmt.format(v) + f" · {r.get('tasks')} task" + ("s" if r.get("tasks") != 1 else "")
        out.append(f'<text x="{ml + max(w, 2) + 8:.1f}" y="{yy + bh / 2 + 4:.1f}" fill="var(--muted)" font-size="11">{_esc(label)}</text>')
    out.append("</svg>")
    return "\n".join(out)


def cost_stack_svg(series: dict[str, Any], width: int = 640, height: int = 220, max_groups: int = 7) -> str:
    """Spend per bucket (day or hour), stacked by group, from `costs.cost_series`.

    Colors are the fixed eight-slot categorical order (`--viz-1`, `--viz-2`,
    `--viz-cat-3`..`--viz-cat-8`): grouped by activity, the slots follow the activity
    vocabulary's own fixed order (`costs.ACTIVITIES`, "other" last); grouped by anything
    else, slots go to the highest-cost groups and the rest fold into "other" so the palette
    never grows past its eight slots no matter how many models/phases/tasks appear.
    """
    from .costs import ACTIVITIES

    buckets = series.get("buckets") or []
    totals = series.get("totals") or {}
    if not buckets or not totals:
        return '<div class="empty">No cost recorded yet.</div>'
    if series.get("group_by") == "activity":
        order = [g for g in ACTIVITIES if g in totals]
        if "other" in totals:
            order.append("other")
    else:
        order = list(series.get("groups") or [])
        if len(order) > max_groups:
            order = order[: max_groups - 1] + ["other"]
    kept_real = set(order) - {"other"}
    palette = ["var(--viz-1)", "var(--viz-2)", "var(--viz-cat-3)", "var(--viz-cat-4)",
               "var(--viz-cat-5)", "var(--viz-cat-6)", "var(--viz-cat-7)", "var(--viz-cat-8)"]
    colors = {g: palette[i % len(palette)] for i, g in enumerate(g for g in order if g != "other")}
    colors["other"] = palette[-1]
    other_total = sum(r["cost_usd"] for g, r in totals.items() if g not in kept_real) if "other" in order else 0.0

    # per-bucket cost per kept group; anything outside `order` (only possible when a
    # non-activity grouping was truncated to max_groups) collapses into "other"
    rows: list[dict[str, float]] = []
    stack_max = 0.0
    for b in buckets:
        row = dict.fromkeys(order, 0.0)
        for g, r in b["groups"].items():
            key = g if g in kept_real else "other"
            row[key] = row.get(key, 0.0) + float(r.get("cost_usd") or 0.0)
        rows.append(row)
        stack_max = max(stack_max, sum(row.values()))
    ymax = stack_max or 1.0

    entry_w = 108
    per_row = max(1, int((width - 40 - 16) // entry_w))
    legend_rows = [order[i:i + per_row] for i in range(0, len(order), per_row)]
    legend_h = 16 * len(legend_rows) + 4

    ml, mr, mt, mb = 40, 16, legend_h + 16, 30
    pw, ph = width - ml - mr, height - mt - mb
    n = len(buckets)
    bw = pw / n
    bar_w = max(bw * 0.72, 2)

    def y(v: float) -> float:
        return mt + ph * (1 - v / ymax)

    out = [f'<svg class="chart" viewBox="0 0 {width} {mt + ph + mb}" width="100%" role="img" '
           f'aria-label="Cost by {_esc(series.get("group_by", ""))} over time">']
    for v in sorted({0, ymax / 2, ymax}):
        out.append(f'<line x1="{ml}" x2="{width - mr}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="var(--line)" stroke-width="1"/>')
        out.append(f'<text x="{ml - 6}" y="{y(v) + 4:.1f}" text-anchor="end" fill="var(--muted)" font-size="11">${v:.0f}</text>')
    for i, (b, row) in enumerate(zip(buckets, rows, strict=True)):
        x0 = ml + i * bw + (bw - bar_w) / 2
        acc = 0.0
        for g in order:
            v = row.get(g, 0.0)
            if v <= 0:
                continue
            y0, y1 = y(acc), y(acc + v)
            out.append(f'<rect x="{x0:.1f}" y="{y1:.1f}" width="{bar_w:.1f}" height="{max(y0 - y1, 0.5):.1f}" '
                       f'fill="{colors[g]}"><title>{_esc(b["bucket"])} · {_esc(g)}: ${v:.2f}</title></rect>')
            acc += v
    out.append(f'<text x="{ml}" y="{mt + ph + mb - 6}" fill="var(--muted)" font-size="11">{_esc(buckets[0]["bucket"])}</text>')
    out.append(f'<text x="{width - mr}" y="{mt + ph + mb - 6}" text-anchor="end" fill="var(--muted)" font-size="11">{_esc(buckets[-1]["bucket"])}</text>')
    ly = 12
    out.append('<g font-size="11" fill="var(--muted)">')
    for row_groups in legend_rows:
        for j, g in enumerate(row_groups):
            gx = ml + j * entry_w
            total = totals[g]["cost_usd"] if g in kept_real and g in totals else other_total
            out.append(f'<rect x="{gx}" y="{ly - 8}" width="10" height="10" fill="{colors[g]}"/>'
                       f'<text x="{gx + 14}" y="{ly}">{_esc(g)} (${total:.2f})</text>')
        ly += 16
    out.append("</g>")
    out.append("</svg>")
    return "\n".join(out)


def sparkline_svg(values: list[float], width: int = 120, height: int = 28) -> str:
    if len(values) < 2:
        return ""
    vmax = max(values) or 1.0
    step = width / (len(values) - 1)
    pts = " ".join(f"{i * step:.1f},{height - 3 - (height - 6) * v / vmax:.1f}" for i, v in enumerate(values))
    return (f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" class="spark">'
            f'<polyline fill="none" stroke="var(--viz-1)" stroke-width="1.5" points="{pts}"/></svg>')
