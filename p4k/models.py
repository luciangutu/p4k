"""Plain data objects shared between the p4 layer and the UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class Change:
    """A submitted changelist."""

    number: int
    date: datetime
    user: str
    client: str
    description: str

    @property
    def summary(self) -> str:
        """First non-empty line of the description."""
        for line in self.description.splitlines():
            if line.strip():
                return line.strip()
        return ""


@dataclass(frozen=True)
class FileChange:
    """One file revision affected by a changelist."""

    depot_path: str
    rev: int
    action: str  # add, edit, delete, branch, integrate, move/add, ...
    file_type: str  # text, binary, ktext, utf8, ...

    @property
    def is_binary(self) -> bool:
        base = self.file_type.split("+", 1)[0]
        return base in ("binary", "ubinary", "apple", "resource") or base.endswith("binary")


DiffKind = Literal["header", "hunk", "add", "del", "context"]


@dataclass(frozen=True)
class DiffLine:
    kind: DiffKind
    text: str


@dataclass
class FileDiff:
    """Diff of a single file, ready to be rendered."""

    lines: list[DiffLine] = field(default_factory=list)
    message: str | None = None  # informational note (binary, no changes, ...)
    truncated: bool = False
