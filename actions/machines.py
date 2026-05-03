"""
/machine and /wake — manage SSH targets + Wake-on-LAN.

The bot itself runs on `local`. Other machines are addresses Claude can SSH
into (the active machine is auto-injected into the system prompt by the
ClaudeRunner so Claude knows where to operate).
"""
from __future__ import annotations

from kernel.runner import Context

_SHARED: dict = {}


def _registry():
    return _SHARED.get("registry")


# ---------------------------------------------------------------------------
# /machine
# ---------------------------------------------------------------------------
async def cmd_machine(ctx: Context) -> None:
    """Manage SSH-target machines.

    /machine                                 list machines + active selection
    /machine local                           switch back to this Mac
    /machine <name>                          switch active machine
    /machine add <name> <host> [<MAC>]       add a machine
    /machine remove <name>                   remove
    """
    reg = _registry()
    if reg is None:
        await ctx.reply("(machine registry not configured.)")
        return

    msg = ctx.message
    args = (msg.command_args or "").strip().split() if msg else []

    if not args:
        machines = reg.list_machines()
        active = reg.get_active(ctx)
        lines = [f"🖥 Active: {active}"]
        lines.append("")
        lines.append(f"Available ({len(machines) + 1}):")
        lines.append("  • local — this Mac")
        for name, info in sorted(machines.items()):
            mac = f"  ({info.get('mac', 'no MAC')})" if info.get("mac") else ""
            lines.append(f"  • {name} — {info['host']}{mac}")
        lines.append("")
        lines.append("Examples:")
        lines.append("  /machine add prod alice@prod.example.com")
        lines.append("  /machine add server 192.168.1.10 AA:BB:CC:DD:EE:FF")
        lines.append("  /machine prod")
        await ctx.reply("\n".join(lines))
        return

    sub = args[0].lower()

    # /machine local
    if sub == "local":
        reg.set_active(ctx, "local")
        await ctx.reply("🖥 Active machine → local (this Mac)")
        return

    # /machine add <name> <host> [<MAC>]
    if sub == "add":
        if len(args) < 3:
            await ctx.reply("Usage: /machine add <name> <host> [<MAC>]")
            return
        name = args[1]
        host = args[2]
        mac = args[3] if len(args) > 3 else None
        try:
            reg.add(name, host=host, mac=mac)
        except ValueError as e:
            await ctx.reply(f"Error: {e}")
            return
        suffix = f" (MAC {mac})" if mac else " (no MAC — /wake won't work)"
        await ctx.reply(f"✓ Added '{name}' → {host}{suffix}")
        return

    # /machine remove <name>
    if sub in ("remove", "delete", "rm"):
        if len(args) < 2:
            await ctx.reply("Usage: /machine remove <name>")
            return
        if reg.remove(args[1]):
            await ctx.reply(f"🗑 Removed '{args[1]}'")
        else:
            await ctx.reply(f"No machine named '{args[1]}'")
        return

    # /machine <name>  → switch
    name = args[0]
    if reg.set_active(ctx, name):
        info = reg.get(name) or {}
        host = info.get("host", "?")
        await ctx.reply(f"🖥 Active machine → {name} ({host})")
    else:
        await ctx.reply(f"No machine named '{name}'. List with /machine.")


# ---------------------------------------------------------------------------
# /wake
# ---------------------------------------------------------------------------
async def cmd_wake(ctx: Context) -> None:
    """Send Wake-on-LAN magic packet. Usage: /wake <name>"""
    reg = _registry()
    if reg is None:
        await ctx.reply("(machine registry not configured.)")
        return

    msg = ctx.message
    args = (msg.command_args or "").strip().split() if msg else []
    if not args:
        machines_with_mac = [n for n, i in reg.list_machines().items() if i.get("mac")]
        if not machines_with_mac:
            await ctx.reply(
                "No machines with MAC addresses configured. "
                "Re-add: /machine add <name> <host> <MAC>"
            )
            return
        await ctx.reply(
            "Usage: /wake <name>\n"
            f"Available: {', '.join(machines_with_mac)}"
        )
        return

    try:
        mac = reg.wake(args[0])
    except KeyError as e:
        await ctx.reply(str(e))
        return
    except ValueError as e:
        await ctx.reply(str(e))
        return
    except Exception as e:
        await ctx.reply(f"Wake failed: {e}")
        return

    await ctx.reply(f"📡 Magic packet sent to {args[0]} ({mac})")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register(dispatcher, registry=None) -> None:
    if registry is not None:
        _SHARED["registry"] = registry
    dispatcher.command("machine", cmd_machine)
    dispatcher.command("wake", cmd_wake)
