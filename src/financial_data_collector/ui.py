"""Terminal presentation for the fdc commands: the only module that knows about styling.

Output degrades to plain text automatically when stdout is not a terminal (scheduled
task logs, pipes, pytest capture) and honours NO_COLOR.
"""
from __future__ import annotations

import io
import math
import sys
from typing import Iterable, Sequence

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# A wide console for logs and pipes so table rows do not wrap; the terminal decides its own width.
console = Console(width=None if sys.stdout.isatty() else 200, highlight=False)
err_console = Console(stderr=True, highlight=False)

STATUS_STYLES = {"ok": "green", "skipped": "yellow", "error": "red", "dry-run": "cyan", "never": "dim"}


def is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def fmt_number(v) -> str:
    """Thousands separators; two decimals above 1,000; up to four trimmed decimals below; blank for null."""
    if v is None:
        return ""
    if not is_number(v):
        return str(v)
    if isinstance(v, int):
        return f"{v:,}"
    if math.isnan(v):
        return ""
    if abs(v) >= 1000:
        return f"{v:,.2f}"
    if v.is_integer():
        return f"{int(v):,}"
    return f"{v:.4f}".rstrip("0").rstrip(".")


def table(columns: Sequence[str], rows: Iterable[Sequence], title: str | None = None,
          footer: str | None = None) -> Table:
    rows = [list(r) for r in rows]
    t = Table(title=title, caption=footer, box=box.SIMPLE_HEAD, header_style="bold", show_edge=False,
              pad_edge=False, caption_justify="left", title_justify="left",
              min_width=min(60, max(24, len(footer or ""), len(title or ""))))
    for i, col in enumerate(columns):
        values = [r[i] for r in rows if i < len(r) and r[i] is not None]
        numeric = bool(values) and all(is_number(v) for v in values)
        t.add_column(str(col), justify="right" if numeric else "left", no_wrap=numeric)
    for r in rows:
        t.add_row(*[v if isinstance(v, Text) else fmt_number(v) for v in r])
    return t


def badge(status: str) -> Text:
    return Text(str(status), style=STATUS_STYLES.get(status, ""))


def panel(body: str, title: str) -> Panel:
    return Panel(body, title=title, title_align="left", border_style="dim", expand=False)


def error(message: str) -> None:
    err_console.print(Text("error: ", style="bold red") + Text(str(message)))


def hint(message: str) -> None:
    console.print(Text("hint: ", style="dim") + Text(str(message)))


def to_text(renderable, width: int = 120) -> str:
    """Render to plain text (tests and log files)."""
    buf = io.StringIO()
    Console(file=buf, width=width, force_terminal=False, color_system=None, highlight=False).print(renderable)
    return buf.getvalue()
