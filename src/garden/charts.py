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


def burnup_svg(events: list[dict[str, Any]], total_tasks: int, width: int = 640, height: int = 220) -> str:
    """Cumulative tasks done over time (single series, area) against the total (reference line)."""
    done = sorted(e["at"] for e in events if e.get("kind") == "transition" and e.get("to") == "done")
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


def sparkline_svg(values: list[float], width: int = 120, height: int = 28) -> str:
    if len(values) < 2:
        return ""
    vmax = max(values) or 1.0
    step = width / (len(values) - 1)
    pts = " ".join(f"{i * step:.1f},{height - 3 - (height - 6) * v / vmax:.1f}" for i, v in enumerate(values))
    return (f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" class="spark">'
            f'<polyline fill="none" stroke="var(--viz-1)" stroke-width="1.5" points="{pts}"/></svg>')
