"""
/project — manage per-user projects (cwd + env + model).

A "project" is a working-directory + environment that Alfred uses when it
spawns Claude. Switching project is how you flip Alfred between, say, your
work codebase and your personal scripts without restarting anything.
"""
from __future__ import annotations

from kernel.runner import Context

_SHARED: dict = {}


def _registry():
    return _SHARED.get("registry")


async def cmd_project(ctx: Context) -> None:
    """Manage projects.

    /project                                list projects + active
    /project <name>                         switch active project
    /project add <name> <cwd>               add (use ~ for home)
    /project remove <name>                  remove
    /project model <name> <model>           set default Claude model
    /project local                          deactivate (use repo root)
    """
    reg = _registry()
    if reg is None:
        await ctx.reply("(project registry not configured.)")
        return

    msg = ctx.message
    args = (msg.command_args or "").strip().split() if msg else []

    if not args:
        projects = reg.list_projects(ctx)
        active = reg.active(ctx)
        lines = []
        if active:
            lines.append(f"📂 Active: {active}")
        else:
            lines.append("📂 Active: (none — using bot's repo root)")
        lines.append("")
        if not projects:
            lines.append("No projects yet.")
            lines.append("")
            lines.append("Examples:")
            lines.append("  /project add work ~/Code/myapp")
            lines.append("  /project work")
        else:
            lines.append(f"Projects ({len(projects)}):")
            for name, info in sorted(projects.items()):
                star = "★ " if name == active else "  "
                model = f" · {info['model']}" if info.get("model") else ""
                env_count = len(info.get("env", {}))
                env_str = f" · {env_count} env vars" if env_count else ""
                lines.append(f"  {star}{name} → {info['cwd']}{model}{env_str}")
        await ctx.reply("\n".join(lines))
        return

    sub = args[0].lower()

    if sub == "local":
        reg.set_active(ctx, None)
        await ctx.reply("📂 No active project — Claude runs from the bot's repo root.")
        return

    if sub == "add":
        if len(args) < 3:
            await ctx.reply("Usage: /project add <name> <cwd>")
            return
        name = args[1]
        cwd = " ".join(args[2:])
        try:
            info = reg.add(ctx, name, cwd=cwd)
        except ValueError as e:
            await ctx.reply(f"Error: {e}")
            return
        await ctx.reply(f"✓ Added project '{name}' → {info['cwd']}")
        return

    if sub in ("remove", "delete", "rm"):
        if len(args) < 2:
            await ctx.reply("Usage: /project remove <name>")
            return
        if reg.remove(ctx, args[1]):
            await ctx.reply(f"🗑 Removed '{args[1]}'")
        else:
            await ctx.reply(f"No project named '{args[1]}'")
        return

    if sub == "model":
        if len(args) < 3:
            await ctx.reply("Usage: /project model <name> <model>")
            return
        name = args[1]
        model = args[2]
        if reg.set_model(ctx, name, model):
            await ctx.reply(f"✓ '{name}' → model {model}")
        else:
            await ctx.reply(f"No project named '{name}'")
        return

    if sub == "env":
        # /project env <name> <KEY>=<VALUE>   or   /project env <name> <KEY>
        if len(args) < 3:
            await ctx.reply("Usage: /project env <name> KEY=VALUE  (or KEY to remove)")
            return
        name = args[1]
        kv = " ".join(args[2:])
        if "=" in kv:
            key, _, value = kv.partition("=")
            if reg.set_env(ctx, name, key.strip(), value.strip()):
                await ctx.reply(f"✓ '{name}' env {key.strip()} set")
            else:
                await ctx.reply(f"No project named '{name}'")
        else:
            if reg.set_env(ctx, name, kv.strip(), None):
                await ctx.reply(f"✓ '{name}' env {kv.strip()} removed")
            else:
                await ctx.reply(f"No project named '{name}'")
        return

    # Fallback: switch active project
    name = args[0]
    if reg.set_active(ctx, name):
        info = reg.list_projects(ctx).get(name) or {}
        cwd = info.get("cwd", "?")
        await ctx.reply(f"📂 Active project → {name} ({cwd})")
    else:
        await ctx.reply(f"No project named '{name}'. List with /project.")


def register(dispatcher, registry=None) -> None:
    if registry is not None:
        _SHARED["registry"] = registry
    dispatcher.command("project", cmd_project)
