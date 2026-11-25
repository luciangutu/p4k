"""Detail screen: changelist info, affected files (left) and diff (right)."""

from __future__ import annotations

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Header, OptionList, RichLog, Static
from textual.widgets.option_list import Option
from textual.worker import get_current_worker

from ..models import Change, FileChange, FileDiff
from ..p4client import P4Client, P4Error

# Short marker and color per p4 action, shown in the file list.
ACTION_STYLE: dict[str, tuple[str, str]] = {
    "add": ("A", "green"),
    "edit": ("M", "yellow"),
    "delete": ("D", "red"),
    "branch": ("B", "cyan"),
    "integrate": ("I", "magenta"),
    "move/add": ("R", "blue"),
    "move/delete": ("r", "bright_black"),
    "import": ("A", "green"),
    "purge": ("P", "red"),
    "archive": ("X", "bright_black"),
}

DIFF_STYLE = {
    "header": "bold cyan",
    "hunk": "magenta",
    "add": "green",
    "del": "red",
    "context": "",
}

# Narrowest the file pane may get, in terminal cells.
MIN_FILES_WIDTH = 20


def _common_dir(paths: list[str]) -> str:
    """Longest common directory prefix (ending in '/') of depot paths."""
    if not paths:
        return ""
    parts = [p.split("/")[:-1] for p in paths]
    common: list[str] = []
    for segs in zip(*parts):
        if all(s == segs[0] for s in segs):
            common.append(segs[0])
        else:
            break
    prefix = "/".join(common)
    # Keep at least "//depot/" so stripping still leaves a readable path.
    return prefix + "/" if prefix.count("/") >= 3 else ""


class DetailScreen(Screen):
    """Shows one changelist: description, file list and per-file diff."""

    DEFAULT_CSS = """
    DetailScreen #info {
        height: auto;
        max-height: 10;
        border: round $primary;
        padding: 0 1;
    }
    DetailScreen #panes { height: 1fr; }
    DetailScreen #files {
        /* Actual width is set from the longest file name, see _layout_file_list. */
        width: 30;
        height: 1fr;
        border: round $secondary;
    }
    DetailScreen #diff {
        width: 1fr;
        height: 1fr;
        border: round $secondary;
    }
    DetailScreen #files:focus, DetailScreen #diff:focus { border: round $accent; }
    """

    BINDINGS = [
        Binding("escape,q", "app.pop_screen", "Back"),
        Binding("tab", "app.focus_next", "Switch pane"),
        Binding("n,right_square_bracket", "next_file", "Next file"),
        Binding("p,left_square_bracket", "prev_file", "Prev file"),
        Binding("j", "down", show=False),
        Binding("k", "up", show=False),
    ]

    def __init__(self, client: P4Client, change: Change) -> None:
        super().__init__()
        self.client = client
        self.change = change
        self.files: list[FileChange] = []
        self._prefix = ""  # common directory stripped from file names
        self._files_layout: tuple[int, int] | None = None  # (pane width, entry room)
        self._shown_file: FileChange | None = None  # file whose diff is displayed

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="info"):
            yield Static(self._info_text(self.change), id="info-text")
        with Horizontal(id="panes"):
            yield OptionList(id="files")
            yield RichLog(id="diff", wrap=False, highlight=False, markup=False, auto_scroll=False)
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"CL {self.change.number}"
        self.query_one("#info").border_title = f"CL {self.change.number}"
        self.query_one("#files").border_title = "Files"
        self.query_one("#diff").border_title = "Diff"
        self.query_one("#files", OptionList).focus()
        self.load_describe()

    @staticmethod
    def _info_text(ch: Change) -> Text:
        text = Text()
        text.append(f"{ch.user}", style="bold")
        text.append(f"@{ch.client}", style="dim")
        text.append(f"  {ch.date:%Y-%m-%d %H:%M:%S}\n", style="dim")
        text.append(ch.description.strip("\n"))
        return text

    # ------------------------------------------------------------------
    # File list
    # ------------------------------------------------------------------

    @work(thread=True, exclusive=True, group="describe")
    def load_describe(self) -> None:
        try:
            change, files = self.client.describe(self.change.number)
        except P4Error as exc:
            self.app.call_from_thread(self.notify, str(exc), title="p4 error", severity="error", timeout=10)
            return
        if not get_current_worker().is_cancelled:
            self.app.call_from_thread(self._show_files, change, files)

    def _show_files(self, change: Change, files: list[FileChange]) -> None:
        self.change = change
        self.query_one("#info-text", Static).update(self._info_text(change))
        self.files = files
        self._prefix = _common_dir([f.depot_path for f in files])
        file_list = self.query_one("#files", OptionList)
        file_list.border_title = f"Files ({len(files)})"
        file_list.border_subtitle = self._prefix.rstrip("/") or None
        self._layout_file_list()
        if files:
            file_list.highlighted = 0
        else:
            self.query_one("#diff", RichLog).write(Text("No files in this changelist.", style="dim"))

    def on_resize(self) -> None:
        if self.files:
            self._layout_file_list()

    def _layout_file_list(self) -> None:
        """Size the file pane to its longest entry so the diff gets the rest.

        The pane never takes more than half of the screen; paths that do not
        fit are shortened from the left so the file name stays visible.
        Called on load and on every terminal resize.
        """
        file_list = self.query_one("#files", OptionList)
        styles = file_list.styles
        # Horizontal border + padding, plus room for the vertical scrollbar
        # when there are more files than visible rows.
        chrome = styles.border.spacing.width + styles.padding.width
        if len(self.files) > file_list.scrollable_content_region.height:
            chrome += styles.scrollbar_size_vertical
        max_width = max(MIN_FILES_WIDTH, self.size.width // 2)
        avail = max_width - chrome  # cells available for one entry

        prompts = [self._file_prompt(fc, avail) for fc in self.files]
        longest = max((p.cell_len for p in prompts), default=0)
        title_len = len(str(file_list.border_title or "")) + 4  # corners + spaces
        width = min(max_width, max(MIN_FILES_WIDTH, longest + chrome, title_len))
        if (width, avail) == self._files_layout:
            return  # nothing changed, avoid rebuilding the list
        self._files_layout = (width, avail)

        highlighted = file_list.highlighted
        file_list.clear_options()
        file_list.add_options(Option(p, id=str(i)) for i, p in enumerate(prompts))
        styles.width = width
        if highlighted is not None:
            file_list.highlighted = highlighted

    def _file_prompt(self, fc: FileChange, avail: int) -> Text:
        """File list entry: action marker, path relative to the prefix, rev."""
        marker, color = ACTION_STYLE.get(fc.action, ("?", "white"))
        path = fc.depot_path[len(self._prefix):]
        rev = f"#{fc.rev}"
        room = avail - 2 - len(rev)  # 2 = marker + space
        if len(path) > room > 1:
            path = "…" + path[-(room - 1):]
        prompt = Text(no_wrap=True, overflow="ellipsis")
        prompt.append(f"{marker} ", style=f"bold {color}")
        prompt.append(path)
        prompt.append(rev, style="dim")
        return prompt

    @on(OptionList.OptionHighlighted, "#files")
    def _file_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        idx = int(event.option.id or 0)
        fc = self.files[idx]
        if fc == self._shown_file:
            return  # e.g. list rebuilt after a resize
        self._shown_file = fc
        diff_log = self.query_one("#diff", RichLog)
        diff_log.border_title = f"Diff · {fc.depot_path}#{fc.rev} ({fc.action}, {fc.file_type})"
        diff_log.clear()
        diff_log.write(Text("Loading…", style="dim"))
        self.load_diff(fc)

    @on(OptionList.OptionSelected, "#files")
    def _file_selected(self, event: OptionList.OptionSelected) -> None:
        # Enter on a file moves focus to the diff for scrolling.
        self.query_one("#diff", RichLog).focus()

    # ------------------------------------------------------------------
    # Diff
    # ------------------------------------------------------------------

    @work(thread=True, exclusive=True, group="diff")
    def load_diff(self, fc: FileChange) -> None:
        try:
            diff = self.client.file_diff(fc)
        except P4Error as exc:
            diff = FileDiff(message=f"p4 error: {exc}")
        if not get_current_worker().is_cancelled:
            self.app.call_from_thread(self._show_diff, fc, diff)

    def _show_diff(self, fc: FileChange, diff: FileDiff) -> None:
        # Ignore stale results if the selection moved on meanwhile.
        file_list = self.query_one("#files", OptionList)
        if file_list.highlighted is None or self.files[file_list.highlighted] != fc:
            return
        diff_log = self.query_one("#diff", RichLog)
        diff_log.clear()
        text = Text()
        if diff.message:
            text.append(diff.message + "\n", style="italic yellow")
        for line in diff.lines:
            text.append(line.text + "\n", style=DIFF_STYLE[line.kind])
        if diff.truncated:
            text.append("\n… diff truncated …\n", style="italic yellow")
        diff_log.write(text)
        diff_log.scroll_home(animate=False)

    # ------------------------------------------------------------------
    # Key actions
    # ------------------------------------------------------------------

    def _move_file(self, delta: int) -> None:
        file_list = self.query_one("#files", OptionList)
        if not self.files:
            return
        current = file_list.highlighted or 0
        file_list.highlighted = max(0, min(len(self.files) - 1, current + delta))

    def action_next_file(self) -> None:
        self._move_file(+1)

    def action_prev_file(self) -> None:
        self._move_file(-1)

    def _focused_action(self, *names: str) -> None:
        """Call the first matching action on the focused widget (vim keys)."""
        widget = self.focused
        for name in names:
            method = getattr(widget, name, None)
            if method:
                method()
                return

    def action_down(self) -> None:
        self._focused_action("action_cursor_down", "action_scroll_down")

    def action_up(self) -> None:
        self._focused_action("action_cursor_up", "action_scroll_up")
