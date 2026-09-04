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
from ..inbox import GROUP_KIND, attention_view, decisions
from ..model import Status
from ..runs import RunStore
from ..scheduler import Scheduler, State
from ..store import Store


def _fmt_tui_event(ev: dict) -> str:
    t = ev.get("type", "")
    parts = (ev.get("message") or {}).get("content") or []
    if t == "assistant":
        for p in parts:
            if isinstance(p, dict) and p.get("type") == "tool_use":
                inp = p.get("input") or {}
                detail = inp.get("command") or inp.get("file_path") or str(inp)[:80]
                return f"**tool** {p.get('name', '')} — {str(detail)[:100]}"
        text = next((p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"), "")
        return f"**text** {text[:100]}" if text else ""
    if t == "user":
        for p in parts:
            if isinstance(p, dict) and p.get("type") == "tool_result":
                c = p.get("content") or []
                if isinstance(c, str):
                    text = c
                else:
                    text = next((x.get("text", "") for x in c if isinstance(x, dict) and x.get("text")), "")
                return f"**result** {text[:100]}" if text else "**result**"
        return ""
    if t == "result":
        return f"**result** {ev.get('subtype', '')}"
    return ""


STATUS_COLOR = {
    "draft": "grey70", "blocked": "yellow", "ready": "cyan", "running": "blue", "in_review": "magenta",
    "changes_requested": "dark_orange", "waiting_human": "deep_pink3", "awaiting_triage": "medium_purple", "done": "green", "failed": "red", "wont_do": "tan", "cancelled": "grey50",
}


class GardenTUI(App):
    TITLE = "context-garden"
    CSS = """
    Screen { background: #121614; }
    Header { background: #1a201c; color: #e6ece7; }
    Footer { background: #1a201c; }
    #left { width: 60%; }
    #right { width: 40%; border-left: solid #2c3630; padding: 0 1; }
    #detail { height: 1fr; }
    #status { height: 1; color: #98a59e; padding: 0 1; }
    #answer, #note { display: none; }
    #answer.visible, #note.visible { display: block; }
    DataTable { height: 1fr; background: #121614; }
    DataTable > .datatable--cursor { background: #1f3a2c; color: #e6ece7; }
    DataTable > .datatable--header { background: #1a201c; color: #98a59e; text-style: bold; }
    TabbedContent { height: 1fr; }
    TabPane { padding: 0; }
    Tabs { background: #1a201c; }
    Tab.-active { color: #5cc493; }
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
        Binding("c", "accept", "Accept call"),
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
        self._inbox_by_key: dict[str, dict] = {}
        self._inbox_decisions = 0

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
        self._inbox_decisions = len(decisions(items))
        keys = {"question": "w answer", "decision": "c accept · w reject", "triage": "y ready · n send back", "review": "open PR", "attention": "e continue · x cancel",
                "approve": "a approve · x drop", "budget": "garden.yaml"}
        self._inbox_by_key = {}
        for it in items:
            # notices (retrying, upgrade available) render dimmed and don't count toward "needs you"
            color = "grey50" if GROUP_KIND.get(it["group"]) == "notice" else STATUS_COLOR.get(it["status"], "white")
            row_key = it.get("decision") or it["task"] or it["title"]
            do = "a accept · x reject" if it.get("decision") else keys.get(it["group"], "")
            inbox.add_row(it["task"] or "—", f"[{color}]{it['group_title'][:22]}[/{color}]", it["title"][:40], it["why"][:48], do, key=row_key)
            self._inbox_by_key[row_key] = it
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
                _st = State(self.store.config.garden_dir / "state.json").get(t.id)
                dec = _st.get("decision")
                extra = (f"{dec.get('kind')}: {dec.get('reason', '')}" if dec else "Q: " + str(_st.get("question", "")))[:40]
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
        self._set_status(f"{self._inbox_decisions} need you   {summary}   runs {tot['runs']} ${tot['cost_usd']:.2f}   {self._msg}")
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

    def _selected_decision(self) -> dict | None:
        """The inbox item under the cursor if it is a decision card, else None. Its first cell
        is the *target* task id, so actions key off the row's own key (the decision id)."""
        try:
            tc = self.query_one(TabbedContent)
            if tc.active != "inbox-pane":
                return None
            inbox = self.query_one("#inbox", DataTable)
            if inbox.row_count == 0 or inbox.cursor_row is None:
                return None
            key = inbox.coordinate_to_cell_key(inbox.cursor_coordinate).row_key.value
        except Exception:  # noqa: BLE001
            return None
        it = self._inbox_by_key.get(key)
        return it if it and it.get("decision") else None

    def _resolve_decision(self, dec: dict, accept: bool) -> None:
        try:
            self._sched().resolve_decision(str(dec["decision"]), accept)
            self._msg = f"decision {'accepted' if accept else 'rejected'} on {dec.get('task') or '?'}"
        except Exception as e:  # noqa: BLE001
            self._msg = f"decision failed: {e}"
        self.action_refresh()

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
            if r.status == "running":
                evs = r.stdout_events(n=10)
                if evs:
                    head.append("\n## Live output")
                    for ev in evs:
                        line = _fmt_tui_event(ev)
                        if line:
                            head.append(f"- {line}")
        if st.get("stack_parent"):
            head.append(f"stacked on: {st['stack_parent']} (PR targets {st.get('pr_base')})")
        if t.discovered_from:
            head.append(f"discovered by: {t.discovered_from}")
        if st.get("decision"):
            dec = st["decision"]
            head.append(f"\n## The worker asks you to decide ({dec.get('kind')})\n\n{dec.get('reason', '')}\n\n(press `c` to accept, `w` to reject with a note)")
            if dec.get("final"):
                head.append("### The worker's full message\n\n" + str(dec["final"]))
        elif st.get("question"):
            head.append(f"\n## Waiting for your answer\n\n{st['question']}\n\n(press `w` to answer)")
        att = attention_view(t, st, RunStore(self.store.config.garden_dir))
        if att:
            head.append(f"\n**Needs a decision — {att['kind_title'].lower()}:** {att['reason']} (press `e` to continue)")
            for a in att["actions"]:
                if a.get("command"):
                    head.append(f"- `{a['command']}` — {a['detail']}")
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
        dec = self._selected_decision()
        if dec:
            self._resolve_decision(dec, accept=True)
            return
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
        dec = self._selected_decision()
        if dec:
            self._resolve_decision(dec, accept=False)
            return
        t = self._current()
        if t and not t.status.terminal:
            self._sched().cancel(t, "cancelled (tui)")
        self.action_refresh()

    def action_retry(self) -> None:
        if self._selected_decision():
            self._msg = "decision selected: a to accept, x to reject"
            self.action_refresh()
            return
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
        if self._sched().pending_decision(t):
            box.placeholder = "why you disagree · enter to send back · esc to cancel"
        box.add_class("visible")
        box.focus()

    def action_accept(self) -> None:
        t = self._current()
        if not t or not self._sched().pending_decision(t):
            self._msg = "select a task with a pending worker decision first"
            self.action_refresh()
            return
        try:
            self._sched().accept_decision(t)
            self._msg = f"{t.id}: accepted"
        except Exception as e:  # noqa: BLE001
            self._msg = f"failed: {e}"
        self.action_refresh()

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
            elif self._sched().pending_decision(t):
                self._sched().reject_decision(t, text)
                self._msg = f"{t.id}: rejected; revise run will follow"
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
