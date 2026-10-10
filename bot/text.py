"""Shared text helpers for building Telegram HTML messages."""
import html


def esc(value) -> str:
    """Escape a value for safe interpolation into HTML-mode message text.

    Only &, < and > are escaped (quote=False) so the result is safe as HTML
    text content without affecting quote characters inside it.
    """
    return html.escape(str(value), quote=False)
