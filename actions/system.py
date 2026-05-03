"""
System-info handlers: /status, /clipboard, /paste, /volume, /apps, /search,
/tts, /processes, /battery, /wifi, /focus.

All Mac-native; works across every chat adapter.
"""
from __future__ import annotations

import asyncio
import platform
import shutil
import sys
import time
from pathlib import Path

from kernel.runner import Context

# Module-load timestamp used by /status as the "Alfred uptime" approximation.
_START_TIME = time.time()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _run(*cmd: str, input_text: str | None = None, timeout: float = 10) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if input_text is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(input_text.encode() if input_text else None),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "", "timeout"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


def _fmt_uptime(secs: int) -> str:
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    return f"{secs // 86400}d {(secs % 86400) // 3600}h"


def _bar(pct: float, width: int = 10) -> str:
    pct = max(0.0, min(100.0, pct))
    fill = int(round(width * pct / 100))
    return "█" * fill + "░" * (width - fill)


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------
async def cmd_status(ctx: Context) -> None:
    if sys.platform != "darwin":
        await ctx.reply(f"/status: only macOS is supported (this host: {platform.system()})")
        return

    rc, out, _ = await _run(
        "bash", "-c",
        'echo "HOST:$(hostname)"; '
        'echo "UPTIME:$(uptime | sed \'s/.*up //;s/,.*load.*//\' | xargs)"; '
        'echo "DISK:$(df -h / | tail -1 | awk \'{print $5,$4,$2}\')"; '
        'echo "MEM_FREE_MB:$(vm_stat | awk \'/Pages free/{free=$3} /Pages inactive/{inact=$3} END{printf "%d", (free+inact)*4096/1048576}\')"; '
        'echo "MEM_TOTAL_MB:$(sysctl -n hw.memsize | awk \'{printf "%d", $1/1048576}\')"; '
        'echo "CPU_PCT:$(top -l 2 -n 0 | grep "CPU usage" | tail -1 | awk \'{print $3}\' | tr -d "%")"; '
        'echo "IP:$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo N/A)"',
        timeout=15,
    )
    if rc != 0:
        await ctx.reply("Could not gather system info.")
        return

    info: dict[str, str] = {}
    for line in out.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            info[k.strip()] = v.strip()

    disk_parts = info.get("DISK", "0% ? ?").split()
    disk_pct = float(disk_parts[0].rstrip("%")) if disk_parts and disk_parts[0].endswith("%") else 0
    disk_free = disk_parts[1] if len(disk_parts) > 1 else "?"
    disk_total = disk_parts[2] if len(disk_parts) > 2 else "?"

    try:
        mem_free = int(info.get("MEM_FREE_MB", "0"))
        mem_total = int(info.get("MEM_TOTAL_MB", "1"))
        mem_pct = ((mem_total - mem_free) / mem_total * 100) if mem_total else 0
    except ValueError:
        mem_free, mem_total, mem_pct = 0, 0, 0.0

    try:
        cpu_pct = float(info.get("CPU_PCT", "0").split()[0])
    except (ValueError, IndexError):
        cpu_pct = 0.0

    bot_uptime = int(time.time() - _START_TIME)

    lines = [
        f"🖥  {info.get('HOST', 'mac')}",
        f"   {info.get('IP', 'N/A')}  ·  up {info.get('UPTIME', '?')}",
        "",
        f"CPU  {_bar(cpu_pct)}  {cpu_pct:5.1f}%",
        f"MEM  {_bar(mem_pct)}  {mem_pct:5.1f}%  ({mem_free} MB free / {mem_total} MB)",
        f"DISK {_bar(disk_pct)}  {disk_pct:5.1f}%  ({disk_free} free of {disk_total})",
        "",
        f"Alfred uptime: {_fmt_uptime(bot_uptime)}  ·  adapter: {ctx.adapter.name}",
    ]
    await ctx.reply("\n".join(lines))


# ---------------------------------------------------------------------------
# /clipboard, /paste
# ---------------------------------------------------------------------------
async def cmd_clipboard(ctx: Context) -> None:
    """Show the Mac's clipboard contents.

    Usage: /clipboard           → read current clipboard
           /clipboard <text>    → set clipboard to <text>
    """
    msg = ctx.message
    args = (msg.command_args or "").strip() if msg else ""

    if args:
        await _run("pbcopy", input_text=args, timeout=5)
        await ctx.reply(f"Clipboard set ({len(args)} chars).")
        return

    rc, text, _ = await _run("pbpaste", timeout=5)
    if rc != 0:
        await ctx.reply("pbpaste failed.")
        return
    if not text.strip():
        await ctx.reply("(clipboard is empty)")
        return
    if len(text) > 3500:
        await ctx.reply(text[:3500] + f"\n\n… ({len(text) - 3500} more chars)")
    else:
        await ctx.reply(text)


# `paste` is a slightly nicer alias that always reads.
async def cmd_paste(ctx: Context) -> None:
    rc, text, _ = await _run("pbpaste", timeout=5)
    await ctx.reply(text or "(clipboard is empty)")


# ---------------------------------------------------------------------------
# /volume
# ---------------------------------------------------------------------------
async def cmd_volume(ctx: Context) -> None:
    """Show or set system output volume.

    Usage: /volume       → show current
           /volume 50    → set to 50% (0–100)
           /volume mute  → toggle mute
    """
    msg = ctx.message
    args = (msg.command_args or "").strip().lower() if msg else ""

    if not args:
        rc, vol, _ = await _run(
            "osascript", "-e", "output volume of (get volume settings)", timeout=5
        )
        rc2, muted, _ = await _run(
            "osascript", "-e", "output muted of (get volume settings)", timeout=5
        )
        muted_str = "(muted)" if muted.strip().lower() == "true" else ""
        await ctx.reply(f"🔊 Volume: {vol.strip() or '?'}% {muted_str}".strip())
        return

    if args in ("mute", "unmute", "toggle"):
        rc, current, _ = await _run(
            "osascript", "-e", "output muted of (get volume settings)", timeout=5
        )
        new = "false" if current.strip().lower() == "true" else "true"
        await _run(
            "osascript", "-e", f"set volume {'with' if new == 'false' else 'without'} output muted",
            timeout=5,
        )
        await ctx.reply(f"🔇 Mute → {new}")
        return

    try:
        level = max(0, min(100, int(args)))
    except ValueError:
        await ctx.reply("Usage: /volume [0-100|mute]")
        return
    await _run("osascript", "-e", f"set volume output volume {level}", timeout=5)
    await ctx.reply(f"🔊 Volume → {level}%")


# ---------------------------------------------------------------------------
# /apps
# ---------------------------------------------------------------------------
async def cmd_apps(ctx: Context) -> None:
    """List currently visible apps."""
    if sys.platform != "darwin":
        await ctx.reply("/apps: macOS only.")
        return
    rc, out, _ = await _run(
        "osascript", "-e",
        'tell application "System Events" to get name of every process '
        "whose background only is false",
        timeout=10,
    )
    if rc != 0:
        await ctx.reply("Failed to list apps.")
        return
    apps = [a.strip() for a in out.split(",") if a.strip()]
    apps.sort()
    body = "\n".join(f"  • {a}" for a in apps[:50])
    extra = f"\n\n  … and {len(apps) - 50} more" if len(apps) > 50 else ""
    await ctx.reply(f"📂 Open apps ({len(apps)}):\n{body}{extra}")


# ---------------------------------------------------------------------------
# /search — Spotlight
# ---------------------------------------------------------------------------
async def cmd_search(ctx: Context) -> None:
    """Spotlight search by filename. Usage: /search <query>"""
    msg = ctx.message
    args = (msg.command_args or "").strip() if msg else ""
    if not args:
        await ctx.reply("Usage: /search <query>")
        return
    home = str(Path.home())
    rc, out, _ = await _run("mdfind", "-name", args, "-onlyin", home, timeout=15)
    if rc != 0:
        await ctx.reply("Spotlight search failed.")
        return
    paths = [p for p in out.splitlines() if p.strip()][:20]
    if not paths:
        await ctx.reply(f"No files matching '{args}' under {home}.")
        return
    body = "\n".join(f"  {p}" for p in paths)
    await ctx.reply(f"Found {len(paths)} (first 20):\n{body}")


# ---------------------------------------------------------------------------
# /tts — text-to-speech
# ---------------------------------------------------------------------------
async def cmd_tts(ctx: Context) -> None:
    """Speak text aloud via macOS `say`. Usage: /tts [-v Voice] <text>"""
    msg = ctx.message
    args = (msg.command_args or "").strip() if msg else ""
    if not args:
        await ctx.reply("Usage: /tts <text>  (try /tts -v Samantha hello)")
        return
    if not shutil.which("say"):
        await ctx.reply("`say` not found — macOS only.")
        return
    # Allow "-v Voice rest" passthrough
    if args.startswith("-v "):
        parts = args.split(maxsplit=2)
        if len(parts) < 3:
            await ctx.reply("Usage: /tts -v <Voice> <text>")
            return
        cmd = ["say", "-v", parts[1], parts[2]]
    else:
        cmd = ["say", args]
    await _run(*cmd, timeout=60)
    await ctx.reply(f"🔊 spoke {len(args)} chars")


# ---------------------------------------------------------------------------
# /processes — top CPU consumers
# ---------------------------------------------------------------------------
async def cmd_processes(ctx: Context) -> None:
    rc, out, _ = await _run("bash", "-c", "ps -Aceo pcpu,rss,comm -r | head -15", timeout=10)
    if rc != 0:
        await ctx.reply("ps failed.")
        return
    await ctx.reply(f"Top processes by CPU:\n```\n{out.strip()}\n```")


# ---------------------------------------------------------------------------
# /battery — laptop only, no-op on desktop
# ---------------------------------------------------------------------------
async def cmd_battery(ctx: Context) -> None:
    rc, out, _ = await _run("pmset", "-g", "batt", timeout=5)
    if rc != 0:
        await ctx.reply("pmset failed.")
        return
    if "InternalBattery" not in out:
        await ctx.reply("No internal battery (likely a desktop Mac).")
        return
    await ctx.reply(out.strip())


# ---------------------------------------------------------------------------
# /wifi
# ---------------------------------------------------------------------------
async def cmd_wifi(ctx: Context) -> None:
    rc, out, _ = await _run(
        "bash", "-c",
        'iface=$(networksetup -listallhardwareports | awk "/Wi-Fi/{getline; print \\$2}"); '
        'networksetup -getairportnetwork "$iface" 2>/dev/null',
        timeout=5,
    )
    if rc != 0 or not out.strip():
        await ctx.reply("Couldn't read WiFi. (No interface, or off.)")
        return
    await ctx.reply(out.strip())


# ---------------------------------------------------------------------------
# /focus — Do Not Disturb / Focus mode
# ---------------------------------------------------------------------------
async def cmd_focus(ctx: Context) -> None:
    """Toggle macOS Do Not Disturb / Focus.

    Best-effort: uses Shortcuts app's built-in "Set Focus" if available;
    falls back to a friendly message. macOS removed direct AppleScript
    control of DND in Monterey+; Shortcuts is the supported path.
    """
    rc, out, _ = await _run(
        "shortcuts", "run", "Toggle Do Not Disturb",
        timeout=10,
    )
    if rc == 0:
        await ctx.reply("🌙 Toggled Do Not Disturb")
    else:
        await ctx.reply(
            "Couldn't toggle Focus automatically.\n"
            "Create a Shortcut named 'Toggle Do Not Disturb' that uses the "
            "'Set Focus' action, then re-run /focus."
        )


# ---------------------------------------------------------------------------
# /ip
# ---------------------------------------------------------------------------
async def cmd_ip(ctx: Context) -> None:
    rc, out, _ = await _run(
        "bash", "-c",
        "ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo N/A",
        timeout=5,
    )
    rc2, public, _ = await _run("curl", "-s", "--max-time", "3", "https://api.ipify.org", timeout=4)
    local = out.strip() or "N/A"
    public = public.strip() or "?"
    await ctx.reply(f"Local: {local}\nPublic: {public}")


# ---------------------------------------------------------------------------
# /uptime
# ---------------------------------------------------------------------------
async def cmd_uptime(ctx: Context) -> None:
    rc, out, _ = await _run("uptime", timeout=5)
    await ctx.reply(out.strip() or "uptime failed")


# ---------------------------------------------------------------------------
# /shortcut — run a Siri Shortcut by name
# ---------------------------------------------------------------------------
async def cmd_shortcut(ctx: Context) -> None:
    """Run a Siri Shortcut. Usage: /shortcut <name>"""
    msg = ctx.message
    args = (msg.command_args or "").strip() if msg else ""
    if not args:
        rc, out, _ = await _run("shortcuts", "list", timeout=10)
        names = [n for n in out.splitlines() if n.strip()]
        body = "\n".join(f"  • {n}" for n in names[:30])
        extra = f"\n  … and {len(names) - 30} more" if len(names) > 30 else ""
        await ctx.reply(f"Available shortcuts:\n{body}{extra}\n\nUsage: /shortcut <name>")
        return
    rc, out, err = await _run("shortcuts", "run", args, timeout=60)
    if rc == 0:
        await ctx.reply(f"✓ ran shortcut '{args}'" + (f"\n{out.strip()}" if out.strip() else ""))
    else:
        await ctx.reply(f"shortcut failed: {err.strip() or 'unknown'}")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register(dispatcher) -> None:
    dispatcher.command("status", cmd_status)
    dispatcher.command("clipboard", cmd_clipboard)
    dispatcher.command("paste", cmd_paste)
    dispatcher.command("volume", cmd_volume)
    dispatcher.command("apps", cmd_apps)
    dispatcher.command("search", cmd_search)
    dispatcher.command("tts", cmd_tts)
    dispatcher.command("processes", cmd_processes)
    dispatcher.command("battery", cmd_battery)
    dispatcher.command("wifi", cmd_wifi)
    dispatcher.command("focus", cmd_focus)
    dispatcher.command("ip", cmd_ip)
    dispatcher.command("uptime", cmd_uptime)
    dispatcher.command("shortcut", cmd_shortcut)
