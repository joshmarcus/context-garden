"""The Inbox: what needs a person, and the phase burn-up."""

from __future__ import annotations

from collections import defaultdict
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from ...charts import burnup_svg, tier_bars_svg
from ...events import EventLog, digest, parse_since
from ...github import pull_request_number
from ...inbox import build_inbox, decisions, merge_queue_view
from ...model import Task
from ...store import Store
from ..common import Site, tier_rows

_MAX_GITHUB_LIST_URL = 1800
_MAX_GITHUB_SEARCH_HEADS = 6


def open_pr_destinations(tasks: list[Task], store: Store) -> list[dict[str, str | int]]:
    """Build configured GitHub PR-list routes for tracked open task PRs.

    A task's PR URL remains the authority for its repository and number; the configured
    route prevents a stale or hand-edited URL from sending an operator to another host.
    GitHub's pull-request search supports an open-PR and head-branch filter, which keeps
    unrelated repository PRs out of the list. Search URLs are bounded and split by
    repository when necessary; hosts are never combined because GitHub search is host-local.
    """
    branches: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    for task in tasks:
        if task.status.terminal or task.status.value == "cancelled" or not task.pr or not task.branch:
            continue
        route = store.config.product_github(task.product)
        slug, host = route.get("slug", ""), route.get("host", "")
        if slug and host and pull_request_number(task.pr, slug, host):
            key = (host.lower().rstrip("."), slug)
            branch = (task.branch, task.pr)
            if branch not in branches[key]:
                branches[key].append(branch)

    destinations: list[dict[str, str | int]] = []
    for (host, slug), branches_in_repo in sorted(branches.items()):
        prefix = f"https://{host}/{slug}/pulls?q="
        base = "is:open is:pr"
        chunk: list[str] = []
        for branch, pr_url in sorted(branches_in_repo):
            head = _head_filter(branch)
            if len(_search_url(prefix, base, [head])) > _MAX_GITHUB_LIST_URL:
                if chunk:
                    destinations.append(_pr_destination(prefix, base, chunk, host, slug))
                    chunk = []
                destinations.append(_single_pr_destination(pr_url, host, slug))
                continue
            candidate = [*chunk, head]
            if chunk and (
                len(candidate) > _MAX_GITHUB_SEARCH_HEADS
                or len(_search_url(prefix, base, candidate)) > _MAX_GITHUB_LIST_URL
            ):
                destinations.append(_pr_destination(prefix, base, chunk, host, slug))
                chunk = [head]
            else:
                chunk = candidate
        if chunk:
            destinations.append(_pr_destination(prefix, base, chunk, host, slug))
    return destinations


def _pr_destination(prefix: str, base: str, heads: list[str], host: str, slug: str) -> dict[str, str | int]:
    return {"url": _search_url(prefix, base, heads), "label": f"{host}/{slug}", "count": len(heads)}


def _single_pr_destination(pr_url: str, host: str, slug: str) -> dict[str, str | int]:
    """Keep an overlong search filter exact by linking to its tracked pull request."""
    return {"url": pr_url, "label": f"{host}/{slug}", "count": 1}


def _search_url(prefix: str, base: str, heads: list[str]) -> str:
    return prefix + quote(f"{base} ({' OR '.join(heads)})", safe="")


def _head_filter(branch: str) -> str:
    """Quote a git branch for GitHub's search grammar before URL encoding it."""
    return 'head:"' + branch.replace("\\", "\\\\").replace('"', '\\"') + '"'


def register(app: FastAPI, site: Site) -> None:
    hub, templates, ctx = site.hub, site.templates, site.ctx

    @app.get("/", response_class=HTMLResponse)
    @app.get("/inbox", response_class=HTMLResponse, include_in_schema=False)
    def inbox_page(request: Request):
        from ...inbox import GROUPS

        s = hub.fresh()
        sched = hub.reader()
        items = build_inbox(s, sched)
        owner = request.query_params.get("owner")
        if owner is not None:
            owner = "" if owner == "-" else owner
            items = [item for item in items if not item.get("task") or item.get("owner", "") == owner]
        owner_task_items = [item for item in items if item.get("task")]
        tasks = s.tasks()
        evs = EventLog(s.config.garden_dir / "events.jsonl")
        all_events = evs.read()
        open_tasks = [t for t in tasks.values() if not t.status.terminal and t.status.value != "cancelled"]
        pr_destinations = open_pr_destinations(open_tasks, s)
        prs_open = sum(int(destination["count"]) for destination in pr_destinations)
        in_scope = [t for t in tasks.values() if t.status.value != "cancelled"]
        since_24h = parse_since("24h")
        spent_24h = digest([event for event in all_events if event.get("at", "") >= since_24h])["cost_usd"]
        from ...suggestions import has_pending

        suggestions_pending = sum(1 for t in open_tasks if has_pending(t.body))
        merge_queue = merge_queue_view(
            s,
            sched.state,
            [event for event in all_events if event.get("kind") == "merge_head"],
        )
        investigation_scopes = [(ph.key, f"{prod.name} / {ph.name}")
                                for prod in s.products() for ph in prod.phases if not ph.closed]
        manual_reservations = {
            task_id: sched.state.get(task_id).get("manual_reservation")
            for task_id in tasks
            if sched.state.get(task_id).get("manual_reservation")
        }
        return templates.TemplateResponse(request, "inbox.html", ctx(
            request, page="inbox", items=items, groups=GROUPS, owner_filter=owner,
            owner_task_items=owner_task_items, inbox_count=len(decisions(items)), prs_open=prs_open,
            pr_destinations=pr_destinations,
            tool_build=sched.upgrade_status(),
            spent_24h=spent_24h, suggestions_pending=suggestions_pending, merge_queue=merge_queue,
            burnup=burnup_svg(all_events, len(in_scope), done_ids={t.id for t in in_scope if t.status.value == 'done'}),
            tiers=tier_bars_svg(tier_rows(s, tasks, all_events)), history=all_events,
            investigation_scopes=investigation_scopes, manual_reservations=manual_reservations))
