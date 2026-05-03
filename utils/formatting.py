"""HTML formatting helpers for Telegram messages."""

import re
import html

E = html.escape  # shorthand


def md_to_html(text: str) -> str:
    """Convert Claude's markdown-ish output to Telegram HTML."""
    escaped = E(text)

    # Extract code blocks and inline code first (replace with placeholders so
    # subsequent bold/italic regexes don't corrupt code content).
    placeholders: list[str] = []

    def _stash(html_fragment: str) -> str:
        idx = len(placeholders)
        placeholders.append(html_fragment)
        return f"\x00PH{idx}\x00"

    # Code blocks: ```lang\n...\n```
    def _code_block(m):
        lang = m.group(1)
        code = m.group(2)
        if lang:
            fragment = f'<pre><code class="language-{lang}">{code}</code></pre>'
        else:
            fragment = f'<pre>{code}</pre>'
        return _stash(fragment)

    result = re.sub(r'```([^\n]*)\n(.*?)```', _code_block, escaped, flags=re.DOTALL)

    # Inline code
    result = re.sub(r'`([^`\n]+)`', lambda m: _stash(f'<code>{m.group(1)}</code>'), result)

    # Bold **text** (safe now — no code content left in result)
    result = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', result, flags=re.DOTALL)

    # Strikethrough ~~text~~
    result = re.sub(r'~~(.+?)~~', r'<s>\1</s>', result, flags=re.DOTALL)

    # Italic *text* or _text_ (single star/underscore, not double)
    result = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', result, flags=re.DOTALL)
    result = re.sub(r'(?<!_)_(?!_)(.+?)(?<!_)_(?!_)', r'<i>\1</i>', result, flags=re.DOTALL)

    # Markdown headings ## → bold line
    result = re.sub(r'^#{1,6}\s+(.+)$', r'<b>\1</b>', result, flags=re.MULTILINE)

    # Restore placeholders
    for idx, fragment in enumerate(placeholders):
        result = result.replace(f"\x00PH{idx}\x00", fragment)

    return result


def fmt_expandable(text: str, threshold: int = 800) -> str:
    """Wrap long text in an expandable blockquote."""
    if len(text) <= threshold:
        return text
    return f'<blockquote expandable>{text}</blockquote>'


def fmt_output(text: str, threshold: int = 600) -> str:
    """Format command output — short as <pre>, long as expandable."""
    escaped = E(text)
    if len(escaped) <= threshold:
        return f'<pre>{escaped}</pre>'
    return f'<blockquote expandable><pre>{escaped}</pre></blockquote>'


def fmt_spoiler(text: str) -> str:
    """Wrap text in spoiler tags."""
    return f'<tg-spoiler>{E(text)}</tg-spoiler>'


def progress_bar(percent: float, width: int = 10) -> str:
    filled = int(width * percent / 100)
    return "\u2588" * filled + "\u2591" * (width - filled)


def _safe_html_chunks(text: str, max_len: int = 4000) -> list:
    """Split HTML text at tag boundaries to avoid mid-tag cuts.
    Default 4000 (not 4096) to leave room for pagination prefixes."""
    chunks = []
    while len(text) > max_len:
        # Try to split at a closing tag boundary
        split_at = text.rfind('>', 0, max_len)
        if split_at == -1 or split_at < max_len // 2:
            # Try splitting at a newline
            split_at = text.rfind('\n', 0, max_len)
        if split_at == -1 or split_at < max_len // 2:
            split_at = max_len
        else:
            split_at += 1
        chunks.append(text[:split_at])
        text = text[split_at:]
    if text:
        chunks.append(text)
    return chunks


def fmt_elapsed(seconds: int) -> str:
    """Format elapsed seconds as a human-readable string (e.g. '2h 15m 30s')."""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m {s}s"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s"


# ---------------------------------------------------------------------------
# Section headers & visual structure for dashboards
# ---------------------------------------------------------------------------

def fmt_section(title: str) -> str:
    """Format a bold section header with box-drawing line."""
    return f"<b>━━ {E(title)} ━━━━━━━━━━━━━</b>"


def fmt_subsection(title: str) -> str:
    """Format a bold sub-section header with arrow."""
    return f"<b>▸ {E(title)}</b>"


def fmt_kv(key: str, value: str) -> str:
    """Format a key-value pair in monospace."""
    return f"<code>{E(key):.<12s} {E(value)}</code>"


def fmt_status_line(icon: str, label: str, bar: str, pct: float, detail: str = "") -> str:
    """Format a status line with icon, label, bar, percentage, and optional detail."""
    detail_str = f"  {E(detail)}" if detail else ""
    return f"<code>{icon} {label:<4s} {bar} {pct:>3.0f}%{detail_str}</code>"


def fmt_alert(text: str) -> str:
    """Format an alert/warning using blockquote."""
    return f"<blockquote>{text}</blockquote>"


def fmt_muted(text: str) -> str:
    """Format secondary/muted text."""
    return f"<i>{E(text)}</i>"


def fmt_conclusion_with_detail(conclusion: str, detail: str, detail_threshold: int = 200) -> str:
    """Show conclusion prominently, with detail in expandable blockquote."""
    if len(detail) <= detail_threshold:
        return f"{conclusion}\n\n{detail}"
    return f"{conclusion}\n\n<blockquote expandable>{detail}</blockquote>"
