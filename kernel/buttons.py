"""
Platform-agnostic inline button + keyboard model.

A `Keyboard` is a list of rows; each row is a list of `Button`s. Adapters
translate this to whatever their platform calls it (Telegram inline keyboard,
Discord component row, Slack block actions, web HTML, ...).

A button is one of:
  - callback button: tapping it sends a CallbackPress with `data` payload
  - url button: opens a URL in the user's browser / client
  - webapp button: opens an embedded web view (Telegram-only today; falls
    back to url on platforms that don't support it)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Button:
    label: str
    data: Optional[str] = None        # callback payload, mutually exclusive with url/webapp
    url: Optional[str] = None         # opens external URL
    webapp_url: Optional[str] = None  # opens embedded web view (falls back to url)

    def __post_init__(self):
        provided = sum(x is not None for x in (self.data, self.url, self.webapp_url))
        if provided != 1:
            raise ValueError("Button must have exactly one of: data, url, webapp_url")


# Type alias for a row of buttons (just a list, no wrapper class needed).
InlineRow = list[Button]


@dataclass(frozen=True)
class Keyboard:
    """A grid of inline buttons. Adapters render this as their native widget."""
    rows: tuple[tuple[Button, ...], ...]

    @classmethod
    def of(cls, *rows: list[Button] | tuple[Button, ...]) -> "Keyboard":
        return cls(tuple(tuple(r) for r in rows))

    @classmethod
    def grid(cls, buttons: list[Button], cols: int = 2) -> "Keyboard":
        rows = [tuple(buttons[i:i + cols]) for i in range(0, len(buttons), cols)]
        return cls(tuple(rows))

    def is_empty(self) -> bool:
        return not self.rows or all(not r for r in self.rows)
