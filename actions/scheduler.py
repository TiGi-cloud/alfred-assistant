"""
Scheduler commands: /remind, /timer, /schedule, /alert.

Backed by `kernel.scheduler.Scheduler`. The shared instance is supplied via
`register(dispatcher, scheduler)` so the same scheduler drives every adapter.
"""
from __future__ import annotations

import time

from kernel.runner import Context
from kernel.scheduler import parse_when


_SHARED: dict = {}


def _scheduler():
    return _SHARED.get("scheduler")


def _fmt_when(ts: float) -> str:
    delta = ts - time.time()
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    if delta < 60:
        rel = f"{int(delta)}s"
    elif delta < 3600:
        rel = f"{int(delta // 60)}m"
    elif delta < 86400:
        rel = f"{int(delta // 3600)}h"
    else:
        rel = f"{int(delta // 86400)}d"
    return f"{when} ({rel} from now)"


# ---------------------------------------------------------------------------
# /remind
# ---------------------------------------------------------------------------
async def cmd_remind(ctx: Context) -> None:
    """One-shot reminder.

    /remind                     — list pending reminders
    /remind <when> <text>       — schedule a one-shot reminder
    /remind delete <id>         — cancel a reminder

    Examples:
        /remind in 10 min check the oven
        /remind at 7pm call mom
        /remind 2026-05-04 09:00 morning standup
    """
    sched = _scheduler()
    if sched is None:
        await ctx.reply("(scheduler not running.)")
        return

    msg = ctx.message
    args = (msg.command_args or "").strip() if msg else ""

    if not args:
        jobs = sched.list_jobs(ctx, kind="reminder")
        if not jobs:
            await ctx.reply(
                "No pending reminders.\n\n"
                "Examples:\n"
                "  /remind in 10 min check the oven\n"
                "  /remind at 7pm call mom"
            )
            return
        lines = [f"⏰ Reminders ({len(jobs)}):"]
        for j in sorted(jobs, key=lambda x: x.get("fires_at", 0)):
            lines.append(f"  • {j['id']} — {_fmt_when(j['fires_at'])} — {j['text'][:80]}")
        await ctx.reply("\n".join(lines))
        return

    parts = args.split(maxsplit=1)
    if parts[0].lower() in ("delete", "remove", "rm", "cancel"):
        if len(parts) < 2:
            await ctx.reply("Usage: /remind delete <id>")
            return
        if sched.remove_job(ctx, parts[1].strip()):
            await ctx.reply(f"🗑 cancelled reminder {parts[1].strip()}")
        else:
            await ctx.reply(f"No reminder with id {parts[1]!r}")
        return

    # Try progressively shorter prefixes as "<when>"
    when_ts = None
    text = ""
    for split_at in range(min(60, len(args)), 0, -1):
        candidate_when = args[:split_at].strip()
        ts = parse_when(candidate_when)
        if ts is not None and ts > time.time():
            when_ts = ts
            text = args[split_at:].strip()
            break

    if when_ts is None:
        await ctx.reply(
            "Couldn't parse the time. Try:\n"
            "  /remind in 10 min …\n"
            "  /remind at 7pm …\n"
            "  /remind 2026-05-04 09:00 …"
        )
        return
    if not text:
        await ctx.reply("Reminder needs some text. Example: /remind in 10 min check the oven")
        return

    job = sched.add_reminder(ctx, when_ts, text)
    await ctx.reply(f"✓ Reminder set for {_fmt_when(when_ts)}\n  ({job['id']}) {text}")


# ---------------------------------------------------------------------------
# /timer  (alias of /remind in N minutes)
# ---------------------------------------------------------------------------
async def cmd_timer(ctx: Context) -> None:
    """Quick timer. /timer 5  → "ding!" reminder 5 min from now."""
    sched = _scheduler()
    if sched is None:
        await ctx.reply("(scheduler not running.)")
        return
    msg = ctx.message
    args = (msg.command_args or "").strip().split() if msg else []
    if not args:
        await ctx.reply("Usage: /timer <minutes> [label]")
        return
    try:
        minutes = float(args[0])
    except ValueError:
        await ctx.reply("Usage: /timer <minutes> [label]")
        return
    if minutes <= 0 or minutes > 24 * 60:
        await ctx.reply("Minutes must be between 0 and 1440 (24 hours).")
        return
    label = " ".join(args[1:]).strip() or "⏱ timer done"
    fires = time.time() + minutes * 60
    job = sched.add_reminder(ctx, fires, label)
    await ctx.reply(f"⏱ timer for {minutes:g}m  ({_fmt_when(fires)})  ({job['id']})")


# ---------------------------------------------------------------------------
# /schedule
# ---------------------------------------------------------------------------
async def cmd_schedule(ctx: Context) -> None:
    """Recurring schedule.

    /schedule                                — list schedules
    /schedule "<when>" "<text>"              — add a recurring schedule
    /schedule remove <id>                    — remove

    Examples:
        /schedule "every day at 9am" morning summary
        /schedule "*/5 * * * *" check disk space
        /schedule "every weekday" daily standup
    """
    sched = _scheduler()
    if sched is None:
        await ctx.reply("(scheduler not running.)")
        return

    msg = ctx.message
    args = (msg.command_args or "").strip() if msg else ""

    if not args:
        jobs = sched.list_jobs(ctx, kind="schedule")
        if not jobs:
            await ctx.reply(
                "No schedules.\n\n"
                "Examples:\n"
                "  /schedule \"every day at 9am\" morning summary\n"
                "  /schedule \"*/5 * * * *\" check disk space"
            )
            return
        lines = [f"🗓 Schedules ({len(jobs)}):"]
        for j in jobs:
            lines.append(f"  • {j['id']} — {j.get('natural') or j.get('cron')} — {j['text'][:60]}")
        await ctx.reply("\n".join(lines))
        return

    if args.startswith(("remove ", "delete ", "rm ", "cancel ")):
        _, _, jid = args.partition(" ")
        if sched.remove_job(ctx, jid.strip()):
            await ctx.reply(f"🗑 removed schedule {jid.strip()}")
        else:
            await ctx.reply(f"No schedule with id {jid.strip()!r}")
        return

    # Parse: "expr in quotes" "rest of text" OR <expr> <text>
    expr = ""; text = ""
    if args.startswith('"'):
        end = args.find('"', 1)
        if end > 0:
            expr = args[1:end]
            text = args[end + 1:].strip().strip('"')
    if not expr:
        # Fallback: first word(s) up to a comma
        if "," in args:
            expr, text = (s.strip() for s in args.split(",", 1))
        else:
            # Try first 3 words as expr (e.g. "every day at 9am")
            words = args.split()
            expr = " ".join(words[:5]) if len(words) > 5 else args
            text = " ".join(words[5:]).strip()

    if not expr or not text:
        await ctx.reply(
            'Usage: /schedule "<when>" "<text>"\n'
            'Example: /schedule "every day at 9am" morning summary'
        )
        return

    try:
        job = sched.add_schedule(ctx, expr, text)
    except ValueError as e:
        await ctx.reply(f"Could not parse schedule: {e}")
        return
    await ctx.reply(f"✓ Schedule set: {expr} → {text}\n  ({job['id']})")


# ---------------------------------------------------------------------------
# /alert
# ---------------------------------------------------------------------------
async def cmd_alert(ctx: Context) -> None:
    """Threshold alerts.

    /alert                       — list active alerts
    /alert cpu 90                — fire when CPU >= 90%
    /alert disk 85               — fire when disk >= 85%
    /alert memory 80             — fire when memory >= 80%
    /alert process nginx         — fire when nginx stops
    /alert remove <id>           — remove alert
    """
    sched = _scheduler()
    if sched is None:
        await ctx.reply("(scheduler not running.)")
        return

    msg = ctx.message
    args = (msg.command_args or "").strip().split() if msg else []

    if not args:
        jobs = sched.list_jobs(ctx, kind="alert")
        if not jobs:
            await ctx.reply(
                "No alerts.\n\n"
                "Examples:\n"
                "  /alert cpu 90\n"
                "  /alert disk 85\n"
                "  /alert process nginx"
            )
            return
        lines = [f"🚨 Alerts ({len(jobs)}):"]
        for j in jobs:
            metric = j.get("metric", "?")
            if metric == "process":
                lines.append(f"  • {j['id']} — process {j.get('label')} stopped")
            else:
                lines.append(f"  • {j['id']} — {metric} ≥ {j.get('threshold')}%")
        await ctx.reply("\n".join(lines))
        return

    sub = args[0].lower()
    if sub in ("remove", "delete", "rm", "cancel"):
        if len(args) < 2:
            await ctx.reply("Usage: /alert remove <id>")
            return
        if sched.remove_job(ctx, args[1]):
            await ctx.reply(f"🗑 removed alert {args[1]}")
        else:
            await ctx.reply(f"No alert with id {args[1]!r}")
        return

    if sub in ("cpu", "disk", "memory"):
        if len(args) < 2:
            await ctx.reply(f"Usage: /alert {sub} <threshold-percent>")
            return
        try:
            threshold = float(args[1])
        except ValueError:
            await ctx.reply(f"Usage: /alert {sub} <threshold-percent>")
            return
        if not 0 < threshold <= 100:
            await ctx.reply("Threshold must be between 0 and 100.")
            return
        try:
            job = sched.add_alert(ctx, sub, threshold)
        except ValueError as e:
            await ctx.reply(f"Error: {e}")
            return
        await ctx.reply(f"🚨 Alert added: {sub} ≥ {threshold:g}%  ({job['id']})")
        return

    if sub == "process":
        if len(args) < 2:
            await ctx.reply("Usage: /alert process <name>")
            return
        name = " ".join(args[1:])
        try:
            job = sched.add_alert(ctx, "process", label=name)
        except ValueError as e:
            await ctx.reply(f"Error: {e}")
            return
        await ctx.reply(f"🚨 Alert added: process {name} stopped  ({job['id']})")
        return

    await ctx.reply(
        "Usage:\n"
        "  /alert cpu <percent>\n"
        "  /alert disk <percent>\n"
        "  /alert memory <percent>\n"
        "  /alert process <name>\n"
        "  /alert remove <id>"
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register(dispatcher, scheduler=None) -> None:
    if scheduler is not None:
        _SHARED["scheduler"] = scheduler
    dispatcher.command("remind", cmd_remind)
    dispatcher.command("timer", cmd_timer)
    dispatcher.command("schedule", cmd_schedule)
    dispatcher.command("alert", cmd_alert)
