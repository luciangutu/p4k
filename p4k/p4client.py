"""Thin wrapper around the ``p4`` command line client.

All structured queries use ``p4 -G`` (Python marshal output), which is
stable across server versions and does not require text parsing. Diffs are
read as plain text. Connection settings (P4PORT, P4USER, P4CLIENT, ...) are
taken from the environment / ``p4 set``, exactly like running ``p4`` by hand.
"""

from __future__ import annotations

import io
import marshal
import os
import signal
import subprocess
import threading
from datetime import datetime

from .models import Change, DiffLine, FileChange, FileDiff

# Upper bound on diff lines shown for a single file, to keep the UI responsive.
MAX_DIFF_LINES = 20_000

# p4 actions that create a new file revision with no meaningful predecessor
# in the same path: show the whole content as added.
_ADD_ACTIONS = {"add", "branch", "move/add", "import"}
# Actions that remove a file: show the previous revision as removed.
_DELETE_ACTIONS = {"delete", "move/delete"}
# Actions where the file content is gone from the server.
_NO_CONTENT_ACTIONS = {"purge", "archive"}


class P4Error(Exception):
    """Raised when a p4 command fails or returns an error record."""


def _decode(value: object) -> object:
    """Convert bytes from marshal output to str (lossy for invalid UTF-8)."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


class P4Client:
    """Runs p4 commands and caches results that never change.

    Submitted changelists are immutable, so describe and diff results can be
    cached for the lifetime of the process. Methods are blocking and are
    meant to be called from worker threads.
    """

    def __init__(self, p4_bin: str = "p4") -> None:
        self.p4_bin = p4_bin
        self._describe_cache: dict[int, tuple[Change, list[FileChange]]] = {}
        self._diff_cache: dict[tuple[str, int, str], FileDiff] = {}
        self._lock = threading.Lock()
        # p4 processes currently running, so close() can kill them.
        self._procs: set[subprocess.Popen[bytes]] = set()
        self._closed = False

    def close(self) -> None:
        """Kill any running p4 commands and refuse to start new ones.

        Called when the app exits: worker threads blocked on a slow server
        (e.g. over VPN) would otherwise keep the process alive until p4
        returns, since Python waits for those threads at shutdown.
        """
        with self._lock:
            self._closed = True
            procs = list(self._procs)
        for proc in procs:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass  # already exited

    # ------------------------------------------------------------------
    # Low-level command execution
    # ------------------------------------------------------------------

    def _run_raw(self, args: list[str]) -> bytes:
        """Run ``p4 <args>`` and return stdout as bytes."""
        with self._lock:
            if self._closed:
                raise P4Error("p4 client closed")
            try:
                proc = subprocess.Popen(
                    [self.p4_bin, *args],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    # Own process group, so close() also kills children of
                    # wrapper scripts passed via --p4.
                    start_new_session=True,
                )
            except FileNotFoundError as exc:
                raise P4Error(f"p4 executable not found: {self.p4_bin}") from exc
            self._procs.add(proc)
        try:
            stdout, stderr = proc.communicate()
        finally:
            with self._lock:
                self._procs.discard(proc)
        if self._closed:
            raise P4Error("p4 client closed")
        if proc.returncode != 0 and not stdout:
            msg = stderr.decode("utf-8", errors="replace").strip()
            raise P4Error(msg or f"p4 {' '.join(args)} failed ({proc.returncode})")
        return stdout

    def _run_tagged(self, args: list[str]) -> list[dict[str, object]]:
        """Run ``p4 -G <args>`` and return the decoded records.

        Error records are turned into a P4Error, except warnings
        (severity <= 2) that come alongside real data, and the common
        "no such file(s)" style warnings that simply mean "no results".
        """
        out = self._run_raw(["-G", *args])
        stream = io.BytesIO(out)
        records: list[dict[str, object]] = []
        errors: list[tuple[int, str]] = []
        while True:
            try:
                raw = marshal.load(stream)
            except EOFError:
                break
            rec = {_decode(k): _decode(v) for k, v in raw.items()}
            if rec.get("code") == "error":
                errors.append((int(rec.get("severity", 3)), str(rec.get("data", "")).strip()))
            else:
                records.append(rec)

        fatal = [msg for sev, msg in errors if sev >= 3]
        if fatal:
            raise P4Error("\n".join(fatal))
        if errors and not records:
            msgs = [msg for _, msg in errors]
            if all("no such file" in m or "no file(s)" in m for m in msgs):
                return []
            raise P4Error("\n".join(msgs))
        return records

    # ------------------------------------------------------------------
    # Changelists
    # ------------------------------------------------------------------

    def changes(
        self,
        path: str = "//...",
        max_count: int = 200,
        before: int | None = None,
        user: str | None = None,
    ) -> list[Change]:
        """Return submitted changelists, newest first.

        ``before`` limits results to changelists strictly older than the
        given number, which is how paging is implemented.
        """
        args = ["changes", "-s", "submitted", "-l", "-m", str(max_count)]
        if user:
            args += ["-u", user]
        spec = path
        if before is not None:
            if before <= 1:
                return []
            spec = f"{path}@{before - 1}"
        args.append(spec)
        return [_change_from_record(r) for r in self._run_tagged(args)]

    def describe(self, number: int) -> tuple[Change, list[FileChange]]:
        """Return the changelist metadata and the list of affected files."""
        with self._lock:
            cached = self._describe_cache.get(number)
        if cached:
            return cached

        records = self._run_tagged(["describe", "-s", str(number)])
        if not records:
            raise P4Error(f"Changelist {number} not found")
        rec = records[0]
        change = _change_from_record(rec)
        files: list[FileChange] = []
        i = 0
        # describe returns files as indexed fields: depotFile0, action0, ...
        while f"depotFile{i}" in rec:
            files.append(
                FileChange(
                    depot_path=str(rec[f"depotFile{i}"]),
                    rev=int(rec.get(f"rev{i}", 0) or 0),
                    action=str(rec.get(f"action{i}", "")),
                    file_type=str(rec.get(f"type{i}", "")),
                )
            )
            i += 1

        result = (change, files)
        with self._lock:
            self._describe_cache[number] = result
        return result

    # ------------------------------------------------------------------
    # Diffs
    # ------------------------------------------------------------------

    def file_diff(self, fc: FileChange) -> FileDiff:
        """Return the diff introduced by ``fc`` (one file revision)."""
        key = (fc.depot_path, fc.rev, fc.action)
        with self._lock:
            cached = self._diff_cache.get(key)
        if cached:
            return cached

        diff = self._compute_diff(fc)
        with self._lock:
            self._diff_cache[key] = diff
        return diff

    def _compute_diff(self, fc: FileChange) -> FileDiff:
        if fc.action in _NO_CONTENT_ACTIONS:
            return FileDiff(message=f"File content not available ({fc.action}).")
        if fc.is_binary and fc.action not in _DELETE_ACTIONS:
            return FileDiff(message=f"Binary file ({fc.file_type}), {fc.action} #{fc.rev}.")

        if fc.action in ("move/add", "branch"):
            source = self._source_revision(fc)
            if source:
                return self._diff_against_source(fc, source)

        if fc.action in _ADD_ACTIONS or fc.rev <= 1:
            text = self._print(f"{fc.depot_path}#{fc.rev}")
            return _whole_file_diff(text, "+", f"New file ({fc.action}) #{fc.rev}")

        if fc.action in _DELETE_ACTIONS:
            if fc.is_binary:
                return FileDiff(message=f"Binary file deleted ({fc.action}).")
            text = self._print(f"{fc.depot_path}#{fc.rev - 1}")
            return _whole_file_diff(text, "-", f"Deleted file ({fc.action}), was #{fc.rev - 1}")

        # edit, integrate, and anything else with a previous revision.
        out = self._run_raw(
            ["diff2", "-du", f"{fc.depot_path}#{fc.rev - 1}", f"{fc.depot_path}#{fc.rev}"]
        )
        return _parse_unified_diff(out.decode("utf-8", errors="replace"))

    def _source_revision(self, fc: FileChange) -> str | None:
        """For a moved/branched revision, return the source as ``file#rev``."""
        try:
            records = self._run_tagged(["filelog", "-m1", f"{fc.depot_path}#{fc.rev}"])
        except P4Error:
            return None
        if not records:
            return None
        rec = records[0]
        # Integration records of revision 0 are indexed as "how0,N", "file0,N".
        j = 0
        while f"how0,{j}" in rec:
            if rec[f"how0,{j}"] in ("moved from", "branch from"):
                return f"{rec[f'file0,{j}']}{rec.get(f'erev0,{j}', '')}"
            j += 1
        return None

    def _diff_against_source(self, fc: FileChange, source: str) -> FileDiff:
        out = self._run_raw(["diff2", "-du", source, f"{fc.depot_path}#{fc.rev}"])
        diff = _parse_unified_diff(out.decode("utf-8", errors="replace"))
        verb = "Moved" if fc.action == "move/add" else "Branched"
        if diff.message:  # no +/- lines: content identical to the source
            diff.message = f"{verb} from {source} (content identical)."
        else:
            diff.message = f"{verb} from {source}."
        return diff

    def _print(self, file_spec: str) -> str:
        """Return the content of a file revision."""
        out = self._run_raw(["print", "-q", file_spec])
        return out.decode("utf-8", errors="replace")


# ----------------------------------------------------------------------
# Parsing helpers
# ----------------------------------------------------------------------


def _change_from_record(rec: dict[str, object]) -> Change:
    return Change(
        number=int(rec["change"]),
        date=datetime.fromtimestamp(int(rec.get("time", 0) or 0)),
        user=str(rec.get("user", "")),
        client=str(rec.get("client", "")),
        description=str(rec.get("desc", "")).rstrip(),
    )


def _whole_file_diff(text: str, sign: str, header: str) -> FileDiff:
    """Build a diff that shows an entire file as added or removed."""
    kind = "add" if sign == "+" else "del"
    lines = [DiffLine("header", header)]
    content = text.splitlines()
    for line in content[:MAX_DIFF_LINES]:
        lines.append(DiffLine(kind, sign + line))
    return FileDiff(lines=lines, truncated=len(content) > MAX_DIFF_LINES)


def _parse_unified_diff(text: str) -> FileDiff:
    """Classify each line of ``p4 diff2 -du`` output for coloring."""
    lines: list[DiffLine] = []
    raw = text.splitlines()
    for line in raw[:MAX_DIFF_LINES]:
        if line.startswith("===="):
            lines.append(DiffLine("header", line))
        elif line.startswith("@@"):
            lines.append(DiffLine("hunk", line))
        elif line.startswith("+"):
            lines.append(DiffLine("add", line))
        elif line.startswith("-"):
            lines.append(DiffLine("del", line))
        elif line.startswith("(... files differ ...)"):
            lines.append(DiffLine("header", line))
        else:
            lines.append(DiffLine("context", line))
    if not any(dl.kind in ("add", "del") for dl in lines):
        return FileDiff(lines=lines, message="No content changes (e.g. type or integration-only change).")
    return FileDiff(lines=lines, truncated=len(raw) > MAX_DIFF_LINES)
