"""Terminal UI (Textual): task table on the left, detail on the right, keys for actions.

Thin over Store/Scheduler, like the web UI. Refreshes every few seconds so a `garden watch`
or `garden serve` running elsewhere shows up live.
"""

from __future__ import annotations

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Markdown,
    Static,
    TabbedContent,
    TabPane,
)

from ..graph import blockers, effective_status
from ..model import Status
from ..runs import RunStore
from ..scheduler import Scheduler, State
from ..store import Store

STATUS_COLOR = {
    "draft": "grey70", "blocked": "yellow", "ready": "cyan", "running": "blue", "in_review": "magenta",
    "changes_requested": "dark_orange", "waiting_human": "deep_pink3", "awaiting_triage": "medium_purple", "done": "green", "failed": "red", "cancelled": "grey50",
}


class GardenTUI(App):
    TITLE = "context-garden"
    CSS = """
    Screen { background: #151714; }
    Header { background: #1d201c; color: #e9e9e0; }
    Footer { background: #1d201c; }
    #left { width: 60%; }
    #right { width: 40%; border-left: solid #3a3f38; padding: 0 1; }
    #detail { height: 1fr; }
    #status { height: 1; color: #a6a99e; padding: 0 1; }
    #answer, #note { display: none; }
    #answer.visible, #note.visible { display: block; }
    DataTable { height: 1fr; background: #151714; }
    DataTable > .datatable--cursor { background: #33382f; color: #e9e9e0; }
    DataTable > .datatable--header { background: #1d201c; color: #a6a99e; text-style: bold; }
    TabbedContent { height: 1fr; }
    TabPane { padding: 0; }
    Tabs { background: #1d201c; }
    Tab.-active { color: #e07a6e; }
    """
    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("t", "tick", "Tick"),
        Binding("a", "approve", "Approve"),
        Binding("d", "dispatch", "Dispatch"),
        Binding("x", "cancel", "Cancel"),
        Binding("e", "retry", "Reset→ready"),
        Binding("b", "brief", "Brief size"),
        Binding("l", "log", "Log"),
        Binding("f", "filter", "Filter open/all"),
        Binding("w", "answer", "Answer"),
        Binding("y", "triage_ready", "Ready for review"),
        Binding("n", "triage_changes", "Send back"),
        Binding("i", "inbox_tab", "Inbox/Tasks"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, store: Store):
        super().__init__()
        self.store = store
        self.only_open = True
        self._msg = ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="left"):
                with TabbedContent(initial="inbox-pane"):
                    with TabPane("Inbox", id="inbox-pane"):
                        yield DataTable(id="inbox", cursor_type="row", zebra_stripes=True)
                    with TabPane("Tasks", id="tasks-pane"):
                        yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
                yield Input(placeholder="answer for the waiting worker · enter to send · esc to cancel", id="answer")
                yield Input(placeholder="what to change (send back) · enter to send · esc to cancel", id="note")
                yield Static("", id="status")
            with Vertical(id="right"):
                yield Markdown("", id="detail")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "context garden"
        self.sub_title = self.store.config.get("name") or ""
        table = self.query_one("#table", DataTable)
        table.add_columns("id", "status", "p", "title", "phase", "deps/pr")
        inbox = self.query_one("#inbox", DataTable)
        inbox.add_columns("id", "needs you", "title", "why", "do")
        self.action_refresh()
        self.set_interval(5, self.action_refresh)

    def _refresh_inbox(self) -> None:
        from ..inbox import build_inbox

        inbox = self.query_one("#inbox", DataTable)
        selected = self._selected_id()
        inbox.clear()
        try:
            items = build_inbox(self.store, self._sched())
        except Exception as e:  # noqa: BLE001
            self._msg = f"inbox error: {e}"
            return
        keys = {"question": "w answer", "triage": "y ready · n send back", "review": "open PR", "attention": "e continue · x cancel",
                "approve": "a approve · x drop", "budget": "garden.yaml"}
        for it in items:
            color = STATUS_COLOR.get(it["status"], "white")
            inbox.add_row(it["task"] or "—", f"[{color}]{it['group_title'][:22]}[/{color}]", it["title"][:40], it["why"][:48], keys.get(it["group"], ""), key=it["task"] or it["title"])
        if selected:
            try:
                inbox.move_cursor(row=inbox.get_row_index(selected))
            except Exception:  # noqa: BLE001
                pass

    # ---- data --------------------------------------------------------------
    def action_refresh(self) -> None:
        self.store.invalidate()
        try:
            tasks = self.store.tasks()
        except Exception as e:  # noqa: BLE001
            self._set_status(f"error: {e}")
            return
        self._refresh_inbox()
        table = self.query_one("#table", DataTable)
        selected = self._selected_id()
        table.clear()
        rs = RunStore(self.store.config.garden_dir)
        active = {r.task_id: r for r in rs.active()}
        stack = bool(self.store.config.get("stack", True))
        for t in sorted(tasks.values(), key=lambda t: (t.status.terminal, t.product, t.phase, t.priority, t.id)):
            eff = effective_status(t, tasks, stack)
            if self.only_open and eff in ("done", "cancelled"):
                continue
            extra = ""
            if eff == "blocked":
                extra = "waits " + ",".join(blockers(t, tasks, stack))
            elif eff == "waiting_human":
                extra = "Q: " + str(State(self.store.config.garden_dir / "state.json").get(t.id).get("question", ""))[:40]
            elif eff == "running" and t.id in active:
                extra = f"{active[t.id].elapsed_minutes():.0f} min"
            elif t.pr:
                extra = t.pr.rsplit("/", 1)[-1] and "PR #" + t.pr.rsplit("/", 1)[-1]
            elif t.depends_on:
                extra = ",".join(t.depends_on)
            color = STATUS_COLOR.get(eff, "white")
            table.add_row(t.id, f"[{color}]{eff}[/{color}]", str(t.priority), t.title[:48], t.phase, extra, key=t.id)
        if selected and selected in tasks:
            try:
                idx = table.get_row_index(selected)
                table.move_cursor(row=idx)
            except Exception:  # noqa: BLE001
                pass
        tot = rs.totals()
        counts: dict[str, int] = {}
        for t in tasks.values():
            e = effective_status(t, tasks, stack)
            counts[e] = counts.get(e, 0) + 1
        summary = "  ".join(f"{k}:{v}" for k, v in sorted(counts.items()))
        self._set_status(f"{summary}   runs {tot['runs']} ${tot['cost_usd']:.2f}   {self._msg}")
        self._show_detail()

    def _active_table(self) -> DataTable:
        try:
            active = self.query_one(TabbedContent).active
        except Exception:  # noqa: BLE001
            active = "tasks-pane"
        return self.query_one("#inbox" if active == "inbox-pane" else "#table", DataTable)

    def _selected_id(self) -> str | None:
        table = self._active_table()
        if table.row_count == 0 or table.cursor_row is None:
            return None
        try:
            tid = str(table.get_row_at(table.cursor_row)[0])
            return tid if tid != "—" else None
        except Exception:  # noqa: BLE001
            return None

    def action_inbox_tab(self) -> None:
        tc = self.query_one(TabbedContent)
        tc.active = "tasks-pane" if tc.active == "inbox-pane" else "inbox-pane"
        self._show_detail()

    def on_tabbed_content_tab_activated(self, _event) -> None:
        self._show_detail()

    def action_triage_ready(self) -> None:
        t = self._current()
        if not t or t.status != Status.AWAITING_TRIAGE:
            self._msg = "select an awaiting_triage task first"
            self.action_refresh()
            return
        try:
            self._sched().triage(t, ready=True)
            self._msg = f"{t.id}: ready for review"
        except Exception as e:  # noqa: BLE001
            self._msg = f"triage failed: {e}"
        self.action_refresh()

    def action_triage_changes(self) -> None:
        t = self._current()
        if not t or not t.pr:
            self._msg = "select a task with a PR first"
            self.action_refresh()
            return
        box = self.query_one("#note", Input)
        box.add_class("visible")
        box.focus()

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def on_data_table_row_highlighted(self, _event) -> None:
        self._show_detail()

    def _show_detail(self) -> None:
        tid = self._selected_id()
        detail = self.query_one("#detail", Markdown)
        if not tid:
            detail.update("_no task selected_")
            return
        try:
            t = self.store.task(tid)
        except KeyError:
            return
        st = State(self.store.config.garden_dir / "state.json").get(t.id)
        runs = RunStore(self.store.config.garden_dir).runs_for(t.id)
        head = [f"# {t.id} {t.title}", f"**{t.status.value}** · p{t.priority} · {t.key}"]
        if t.depends_on:
            head.append(f"depends on: {', '.join(t.depends_on)}")
        if t.branch:
            head.append(f"branch: `{t.branch}`")
        if t.pr:
            head.append(f"PR: {t.pr}" + (f" ({st.get('review_decision') or ''} {st.get('checks') or ''})".rstrip() if st else ""))
        if t.reading:
            head.append("reading: " + ", ".join(f"`{r}`" for r in t.reading))
        if runs:
            r = runs[-1]
            cost = f" ${r.cost_usd:.2f}" if r.cost_usd is not None else ""
            head.append(f"last run: {r.run_id} {r.status}{cost} ({len(runs)} total)")
            if r.error:
                head.append(f"error: {r.error[:200]}")
        if st.get("stack_parent"):
            head.append(f"stacked on: {st['stack_parent']} (PR targets {st.get('pr_base')})")
        if t.discovered_from:
            head.append(f"discovered by: {t.discovered_from}")
        if st.get("question"):
            head.append(f"\n## Waiting for your answer\n\n{st['question']}\n\n(press `w` to answer)")
        if st.get("needs_human"):
            head.append(f"\n**Needs a human:** {st['needs_human']} (press `e` to continue)")
        if st.get("pending_feedback"):
            head.append("\n## Pending feedback\n\n" + st["pending_feedback"])
        detail.update("\n\n".join(head) + "\n\n---\n\n" + t.body)

    # ---- actions -----------------------------------------------------------
    def _sched(self) -> Scheduler:
        self.store.invalidate()
        return Scheduler(self.store, log=self._note)

    def _note(self, msg: str) -> None:
        self._msg = msg[-80:]

    def _current(self):
        tid = self._selected_id()
        return self.store.task(tid) if tid else None

    @work(thread=True, exclusive=True, group="tick")
    def action_tick(self) -> None:
        rep = self._sched().tick()
        self._msg = rep.summary()[-100:]
        self.call_from_thread(self.action_refresh)

    def action_approve(self) -> None:
        t = self._current()
        if t and t.status == Status.DRAFT:
            t.status = Status.READY
            t.log("approved (tui)")
            self.store.save(t)
            self._msg = f"{t.id} approved"
        self.action_refresh()

    @work(thread=True, exclusive=True, group="dispatch")
    def action_dispatch(self) -> None:
        t = self._current()
        if not t:
            return
        try:
            mode = "revise" if t.status == Status.CHANGES_REQUESTED else "work"
            self._sched().dispatch(t, mode=mode)
        except Exception as e:  # noqa: BLE001
            self._msg = f"dispatch failed: {e}"
        self.call_from_thread(self.action_refresh)

    def action_cancel(self) -> None:
        t = self._current()
        if t and not t.status.terminal:
            self._sched().cancel(t, "cancelled (tui)")
        self.action_refresh()

    def action_retry(self) -> None:
        t = self._current()
        if t:
            self._sched().retry(t)
        self.action_refresh()

    def action_brief(self) -> None:
        from ..brief import build_brief

        t = self._current()
        if t:
            b = build_brief(self.store, t)
            self._msg = f"{t.id} brief ~{b.tokens:,} tokens; " + ", ".join(f"{k} {v}" for k, v in b.sections.items())
        self.action_refresh()

    def action_log(self) -> None:
        t = self._current()
        if not t:
            return
        r = RunStore(self.store.config.garden_dir).latest(t.id)
        detail = self.query_one("#detail", Markdown)
        if not r:
            detail.update("_no runs_")
            return
        final = (r.path / "final.md").read_text() if (r.path / "final.md").exists() else ""
        stderr = r.stderr_text()[-3000:]
        detail.update(f"# {t.id} run {r.run_id}\n\nstatus {r.status} · {r.dir}\n\n## Final message\n\n{final or '_none_'}\n\n## stderr\n\n```\n{stderr}\n```")

    def action_answer(self) -> None:
        t = self._current()
        if not t or t.status != Status.WAITING_HUMAN:
            self._msg = "select a waiting_human task first"
            self.action_refresh()
            return
        box = self.query_one("#answer", Input)
        box.add_class("visible")
        box.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        box = event.input
        text = event.value.strip()
        box.value = ""
        box.remove_class("visible")
        self._active_table().focus()
        t = self._current()
        if not text or not t:
            return
        try:
            if box.id == "note":
                self._sched().triage(t, changes=text)
                self._msg = f"{t.id}: sent back"
            else:
                run = self._sched().answer(t, text)
                self._msg = f"{t.id}: {'resumed' if run.session_id else 'fresh run with answer'}"
        except Exception as e:  # noqa: BLE001
            self._msg = f"failed: {e}"
        self.action_refresh()

    def on_key(self, event) -> None:
        if event.key == "escape":
            for bid in ("#answer", "#note"):
                box = self.query_one(bid, Input)
                if box.has_class("visible"):
                    box.remove_class("visible")
                    box.value = ""
                    self._active_table().focus()

    def action_filter(self) -> None:
        self.only_open = not self.only_open
        self.action_refresh()
