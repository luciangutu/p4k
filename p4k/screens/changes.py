"""Main screen: scrollable list of submitted changelists."""

from __future__ import annotations

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input
from textual.worker import get_current_worker

from ..models import Change
from ..p4client import P4Client, P4Error
from .detail import DetailScreen

# Start loading the next page when the cursor gets this close to the end.
PREFETCH_MARGIN = 10
# Longest description summary shown in the table (full text is in details).
SUMMARY_WIDTH = 160


class ChangesScreen(Screen):
    """Lists changelists newest-first, loading older pages on demand."""

    DEFAULT_CSS = """
    ChangesScreen DataTable { height: 1fr; }
    ChangesScreen #search { dock: bottom; display: none; }
    ChangesScreen #search.visible { display: block; }
    """

    BINDINGS = [
        Binding("q", "app.quit", "Quit"),
        Binding("enter", "open", "Details", show=True),
        Binding("slash", "search", "Search"),
        Binding("n", "search_next", "Next match"),
        Binding("N", "search_prev", "Prev match", show=False),
        Binding("r", "refresh", "Refresh"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("g", "first", show=False),
        Binding("G", "last", show=False),
    ]

    def __init__(self, client: P4Client, path: str, page_size: int, user: str | None) -> None:
        super().__init__()
        self.client = client
        self.path = path
        self.page_size = page_size
        self.user = user
        self.changes: list[Change] = []
        self.loading_page = False
        self.exhausted = False  # True once the oldest changelist was loaded
        self.search_text = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(id="changes", cursor_type="row", zebra_stripes=True)
        yield Input(id="search", placeholder="Search description / user / CL (Enter to find, Esc to cancel)")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("CL", key="cl")
        table.add_column("Date", key="date")
        table.add_column("User", key="user")
        table.add_column("Description", key="desc")
        table.focus()
        self.load_page()

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _update_title(self) -> None:
        suffix = "" if self.exhausted else "+"
        user = f" · user {self.user}" if self.user else ""
        self.sub_title = f"{self.path}{user} · {len(self.changes)}{suffix} changelists"

    @work(thread=True, exclusive=True, group="changes")
    def load_page(self) -> None:
        """Fetch the next page of changelists (older than the last loaded)."""
        if self.exhausted:
            return
        self.loading_page = True
        self.app.call_from_thread(setattr, self, "sub_title", "Loading…")
        before = self.changes[-1].number if self.changes else None
        try:
            page = self.client.changes(self.path, self.page_size, before=before, user=self.user)
        except P4Error as exc:
            self.app.call_from_thread(self.notify, str(exc), title="p4 error", severity="error", timeout=10)
            page = None
        if get_current_worker().is_cancelled:
            return
        self.app.call_from_thread(self._append_page, page)

    def _append_page(self, page: list[Change] | None) -> None:
        self.loading_page = False
        if page is None:  # error already reported
            self._update_title()
            return
        if len(page) < self.page_size:
            self.exhausted = True
        table = self.query_one(DataTable)
        for ch in page:
            summary = ch.summary
            if len(summary) > SUMMARY_WIDTH:
                summary = summary[: SUMMARY_WIDTH - 1] + "…"
            table.add_row(
                str(ch.number),
                ch.date.strftime("%Y-%m-%d %H:%M"),
                ch.user,
                summary,
                key=str(ch.number),
            )
        self.changes.extend(page)
        self._update_title()

    def action_refresh(self) -> None:
        """Reload the list from scratch (picks up newly submitted CLs)."""
        self.changes.clear()
        self.exhausted = False
        self.query_one(DataTable).clear()
        self.load_page()

    @on(DataTable.RowHighlighted)
    def _maybe_load_more(self, event: DataTable.RowHighlighted) -> None:
        table = event.data_table
        if (
            not self.loading_page
            and not self.exhausted
            and event.cursor_row >= table.row_count - PREFETCH_MARGIN
        ):
            self.load_page()

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    @on(DataTable.RowSelected)
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        self._open_row(event.cursor_row)

    def action_open(self) -> None:
        self._open_row(self.query_one(DataTable).cursor_row)

    def _open_row(self, row: int) -> None:
        if 0 <= row < len(self.changes):
            self.app.push_screen(DetailScreen(self.client, self.changes[row]))

    def action_cursor_down(self) -> None:
        self.query_one(DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one(DataTable).action_cursor_up()

    def action_first(self) -> None:
        self.query_one(DataTable).move_cursor(row=0)

    def action_last(self) -> None:
        table = self.query_one(DataTable)
        table.move_cursor(row=table.row_count - 1)

    # ------------------------------------------------------------------
    # Search (within loaded changelists)
    # ------------------------------------------------------------------

    def action_search(self) -> None:
        search = self.query_one("#search", Input)
        search.add_class("visible")
        search.value = self.search_text
        search.focus()

    def _close_search(self) -> None:
        self.query_one("#search", Input).remove_class("visible")
        self.query_one(DataTable).focus()

    @on(Input.Submitted, "#search")
    def _search_submitted(self, event: Input.Submitted) -> None:
        self.search_text = event.value.strip()
        self._close_search()
        if self.search_text:
            self._find(forward=True, include_current=True)

    def key_escape(self) -> None:
        if self.query_one("#search", Input).has_class("visible"):
            self._close_search()

    def action_search_next(self) -> None:
        self._find(forward=True)

    def action_search_prev(self) -> None:
        self._find(forward=False)

    def _matches(self, ch: Change) -> bool:
        q = self.search_text.lower()
        if q.isdigit():  # a bare number means "go to this changelist"
            return q == str(ch.number)
        return q in ch.description.lower() or q in ch.user.lower()

    def _find(self, forward: bool, include_current: bool = False) -> None:
        if not self.search_text or not self.changes:
            return
        table = self.query_one(DataTable)
        n = len(self.changes)
        start = table.cursor_row
        offsets = range(0 if include_current else 1, n)
        for off in offsets:
            row = (start + off) % n if forward else (start - off) % n
            if self._matches(self.changes[row]):
                table.move_cursor(row=row)
                return
        self.notify(f"'{self.search_text}' not found in loaded changelists", severity="warning")
