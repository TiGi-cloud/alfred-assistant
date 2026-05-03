"""Project and global-env command handlers (extracted from bot.py)."""

import time
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import bot_state as st
from config import MODELS_FILE, GLOBAL_ENV_FILE, CLAUDE_MODEL
from core import (
    is_allowed,
    deny,
    user_key,
    save_sessions,
    save_projects,
    build_back_button,
)
from persistence import save_json
from utils.formatting import E, fmt_section
from utils.ui import build_back_close, project_status, fmt_time_ago


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _detect_git_remote(cwd: str) -> dict:
    """Auto-detect git org/repo from the remote origin of a directory."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        url = result.stdout.strip()
        if not url:
            return {}
        # Parse git@github.com:org/repo.git or https://github.com/org/repo.git
        import re as _re
        m = _re.search(r'[:/]([^/]+)/([^/]+?)(?:\.git)?$', url)
        if m:
            return {"org": m.group(1), "repo": m.group(2)}
    except Exception:
        pass
    return {}


async def _save_current_project(ukey: str):
    """Save current session_id back to the active project before switching."""
    proj_name = st.active_project.get(ukey)
    if not proj_name or ukey not in st.projects:
        return
    proj = st.projects[ukey].get(proj_name)
    if not proj:
        return
    session_id = st.user_sessions.get(ukey)
    if session_id:
        proj["session_id"] = session_id
    proj["last_used"] = time.time()
    save_projects()


async def _get_pending_summary(ukey: str) -> str:
    """Ask Claude to summarize pending work in the current session."""
    session_id = st.user_sessions.get(ukey)
    if not session_id:
        return ""
    try:
        import claude_runner
        result = await claude_runner.run_claude(
            "Summarize any pending or unfinished work from our conversation in one short line. "
            "If nothing is pending, reply with just: NONE",
            ukey,
        )
        if result and "NONE" not in result.upper()[:20]:
            return result.strip()[:200]
    except Exception:
        pass
    return ""


async def _switch_to_project(ukey: str, name: str):
    """Switch to a named project, restoring its session/model."""
    proj = st.projects[ukey][name]
    # Restore session
    if proj.get("session_id"):
        st.user_sessions[ukey] = proj["session_id"]
    else:
        st.user_sessions.pop(ukey, None)
    save_sessions()
    # Restore per-project model (if set)
    if proj.get("model"):
        st.user_models[ukey] = proj["model"]
        save_json(MODELS_FILE, st.user_models)
    st.active_project[ukey] = name
    proj["last_used"] = time.time()
    save_projects()


def _mask_value(val: str) -> str:
    """Mask a secret value, showing first 3 and last 3 chars."""
    if len(val) <= 8:
        return "***"
    return val[:3] + "***" + val[-3:]


def _fmt_time_ago(ts: float) -> str:
    """Format a timestamp as 'X ago'."""
    diff = time.time() - ts
    if diff < 60:
        return "just now"
    if diff < 3600:
        return f"{int(diff / 60)}m ago"
    if diff < 86400:
        return f"{int(diff / 3600)}h ago"
    return f"{int(diff / 86400)}d ago"


# ---------------------------------------------------------------------------
# /env — global environment variables
# ---------------------------------------------------------------------------

async def cmd_env(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manage global environment variables shared across all projects."""
    if not is_allowed(update):
        return await deny(update)

    if not context.args:
        # List global env vars
        if not st.global_env:
            return await update.message.reply_text(
                "\U0001f511 <b>No global env vars.</b>\n\n"
                "<code>/env KEY VALUE</code> — set a global var\n"
                "Global vars are available in ALL projects (e.g. CLOUDFLARE_API_TOKEN).\n"
                "Project-specific vars override globals.",
                parse_mode=ParseMode.HTML, reply_markup=build_back_button(),
            )
        lines = [f"  <code>{E(k)}</code> = <code>{_mask_value(v)}</code>" for k, v in st.global_env.items()]
        await update.message.reply_text(
            "\U0001f511 <b>Global env vars:</b>\n\n" + "\n".join(lines) +
            "\n\n<i>Available in all projects. Project vars override these.</i>",
            parse_mode=ParseMode.HTML, reply_markup=build_back_button(),
        )
        return

    if context.args[0].lower() == "remove":
        if len(context.args) < 2:
            return await update.message.reply_text("Usage: <code>/env remove KEY</code>", parse_mode=ParseMode.HTML)
        key = context.args[1]
        st.global_env.pop(key, None)
        save_json(GLOBAL_ENV_FILE, st.global_env)
        await update.message.reply_text(f"\u2705 Removed global <code>{E(key)}</code>", parse_mode=ParseMode.HTML)
        return

    if len(context.args) < 2:
        return await update.message.reply_text("Usage: <code>/env KEY VALUE</code>", parse_mode=ParseMode.HTML)

    key = context.args[0]
    value = " ".join(context.args[1:])
    st.global_env[key] = value
    save_json(GLOBAL_ENV_FILE, st.global_env)
    await update.message.reply_text(
        f"\u2705 Global <code>{E(key)}</code> = <code>{_mask_value(value)}</code>",
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# /project — project management
# ---------------------------------------------------------------------------

async def cmd_project(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manage project conversations — switch, create, configure."""
    if not is_allowed(update):
        return await deny(update)
    ukey = user_key(update)

    if not context.args:
        # List all projects with switch buttons
        user_projects = st.projects.get(ukey, {})
        active = st.active_project.get(ukey)
        if not user_projects:
            return await update.message.reply_text(
                "\U0001f4c2 <b>No projects yet.</b>\n\n"
                "<code>/project new &lt;name&gt; [description]</code> — create one\n"
                "Each project keeps its own conversation, model, env vars, and deploy config.",
                parse_mode=ParseMode.HTML, reply_markup=build_back_button(),
            )
        lines = []
        buttons = []
        for name, proj in sorted(user_projects.items(), key=lambda x: x[1].get("last_used", 0), reverse=True):
            marker = "\u25b8 " if name == active else "  "
            active_tag = " (active)" if name == active else ""
            desc = proj.get("description", "")
            desc_str = f" — {E(desc)}" if desc else ""
            ago = _fmt_time_ago(proj.get("last_used", proj.get("created", 0)))
            pending = proj.get("pending", "")
            pending_str = f"\n    Pending: {E(pending)}" if pending else ""
            lines.append(f"<code>{marker}{E(name)}</code>{active_tag}{desc_str} \u00b7 {ago}{pending_str}")
            if name != active:
                cb = f"proj_sw:{name[:50]}"
                buttons.append([InlineKeyboardButton(f"\u2192 {name}", callback_data=cb)])
        buttons.append([InlineKeyboardButton("\u2190 Back", callback_data="menu:main")])
        text = "\U0001f4c2 <b>Projects:</b>\n\n" + "\n".join(lines)
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))
        return

    action = context.args[0].lower()

    # /project new <name> [description]
    if action == "new":
        if len(context.args) < 2:
            return await update.message.reply_text(
                "Usage: <code>/project new &lt;name&gt; [description]</code>",
                parse_mode=ParseMode.HTML,
            )
        name = context.args[1]
        desc = " ".join(context.args[2:]) if len(context.args) > 2 else ""

        # Save current project first
        await _save_current_project(ukey)

        # Auto-detect cwd and git
        desktop_path = Path.home() / "Desktop" / name
        cwd = str(desktop_path) if desktop_path.is_dir() else ""
        git_info = await _detect_git_remote(cwd) if cwd else {}

        # Create project
        if ukey not in st.projects:
            st.projects[ukey] = {}
        st.projects[ukey][name] = {
            "session_id": "",
            "model": "",
            "cwd": cwd,
            "created": time.time(),
            "last_used": time.time(),
            "description": desc,
            "env": {},
            "git": git_info,
            "deploy": {},
            "pending": "",
            "msg_count_today": 0,
        }
        # Clear current session for fresh conversation
        st.user_sessions.pop(ukey, None)
        save_sessions()
        st.active_project[ukey] = name
        save_projects()

        auto_lines = []
        if cwd:
            auto_lines.append(f"  CWD: <code>{E(cwd)}</code>")
        if git_info:
            auto_lines.append(f"  Git: {E(git_info.get('org', ''))}/{E(git_info.get('repo', ''))}")
        auto_str = "\n".join(auto_lines)
        auto_block = f"\n\n<i>Auto-detected:</i>\n{auto_str}" if auto_lines else ""

        await update.message.reply_text(
            f"\U0001f4c2 <b>Created project:</b> <code>{E(name)}</code>\n"
            f"Starting fresh conversation.{auto_block}\n\n"
            f"Configure: <code>/project set cwd|model|desc</code>\n"
            f"Env vars: <code>/project env KEY VALUE</code>\n"
            f"Deploy: <code>/project deploy mac-mini|blue|aws</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    # /project delete <name>
    if action == "delete":
        if len(context.args) < 2:
            return await update.message.reply_text("Usage: <code>/project delete &lt;name&gt;</code>", parse_mode=ParseMode.HTML)
        name = context.args[1]
        if name not in st.projects.get(ukey, {}):
            return await update.message.reply_text(f"No project: <code>{E(name)}</code>", parse_mode=ParseMode.HTML)
        safe_name = name[:45]
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("\u2705 Yes, delete", callback_data=f"proj_del_y:{safe_name}"),
            InlineKeyboardButton("\u274c Cancel", callback_data="proj_del_n"),
        ]])
        await update.message.reply_text(
            f"Delete project <code>{E(name)}</code>? This removes the saved conversation and all config.",
            parse_mode=ParseMode.HTML, reply_markup=kb,
        )
        return

    # /project rename <old> <new>
    if action == "rename":
        if len(context.args) < 3:
            return await update.message.reply_text("Usage: <code>/project rename &lt;old&gt; &lt;new&gt;</code>", parse_mode=ParseMode.HTML)
        old, new = context.args[1], context.args[2]
        if old not in st.projects.get(ukey, {}):
            return await update.message.reply_text(f"No project: <code>{E(old)}</code>", parse_mode=ParseMode.HTML)
        st.projects[ukey][new] = st.projects[ukey].pop(old)
        if st.active_project.get(ukey) == old:
            st.active_project[ukey] = new
        save_projects()
        await update.message.reply_text(f"\u2705 Renamed <code>{E(old)}</code> \u2192 <code>{E(new)}</code>", parse_mode=ParseMode.HTML)
        return

    # /project set <key> <value>
    if action == "set":
        if len(context.args) < 3:
            return await update.message.reply_text(
                "Usage: <code>/project set model|cwd|desc &lt;value&gt;</code>",
                parse_mode=ParseMode.HTML,
            )
        active = st.active_project.get(ukey)
        if not active or active not in st.projects.get(ukey, {}):
            return await update.message.reply_text("No active project. Switch to one first.")
        key = context.args[1].lower()
        value = " ".join(context.args[2:])
        proj = st.projects[ukey][active]
        if key == "model":
            proj["model"] = value
            st.user_models[ukey] = value
            save_json(MODELS_FILE, st.user_models)
        elif key == "cwd":
            proj["cwd"] = value
        elif key in ("desc", "description"):
            proj["description"] = value
        else:
            return await update.message.reply_text(f"Unknown key: <code>{E(key)}</code>. Use model, cwd, or desc.", parse_mode=ParseMode.HTML)
        save_projects()
        await update.message.reply_text(f"\u2705 Set <code>{E(key)}</code> = <code>{E(value)}</code> for <b>{E(active)}</b>", parse_mode=ParseMode.HTML)
        return

    # /project env [KEY] [VALUE] | /project env remove <KEY>
    if action == "env":
        active = st.active_project.get(ukey)
        if not active or active not in st.projects.get(ukey, {}):
            return await update.message.reply_text("No active project. Switch to one first.")
        proj = st.projects[ukey][active]
        if "env" not in proj:
            proj["env"] = {}

        if len(context.args) == 1:
            # List env vars
            env = proj["env"]
            if not env:
                return await update.message.reply_text(
                    f"\U0001f511 No env vars for <b>{E(active)}</b>.\n\n"
                    f"<code>/project env KEY VALUE</code> to add one.",
                    parse_mode=ParseMode.HTML,
                )
            lines = [f"  <code>{E(k)}</code> = <code>{_mask_value(v)}</code>" for k, v in env.items()]
            await update.message.reply_text(
                f"\U0001f511 <b>Env for {E(active)}:</b>\n\n" + "\n".join(lines),
                parse_mode=ParseMode.HTML,
            )
            return

        if context.args[1].lower() == "remove":
            if len(context.args) < 3:
                return await update.message.reply_text("Usage: <code>/project env remove KEY</code>", parse_mode=ParseMode.HTML)
            key = context.args[2]
            proj["env"].pop(key, None)
            save_projects()
            await update.message.reply_text(f"\u2705 Removed <code>{E(key)}</code> from <b>{E(active)}</b>", parse_mode=ParseMode.HTML)
            return

        if len(context.args) < 3:
            return await update.message.reply_text("Usage: <code>/project env KEY VALUE</code>", parse_mode=ParseMode.HTML)
        key = context.args[1]
        value = " ".join(context.args[2:])
        proj["env"][key] = value
        save_projects()
        await update.message.reply_text(
            f"\u2705 Set <code>{E(key)}</code> = <code>{_mask_value(value)}</code> for <b>{E(active)}</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    # /project git <org> [repo]
    if action == "git":
        active = st.active_project.get(ukey)
        if not active or active not in st.projects.get(ukey, {}):
            return await update.message.reply_text("No active project. Switch to one first.")
        if len(context.args) < 2:
            return await update.message.reply_text("Usage: <code>/project git &lt;org&gt; [repo]</code>", parse_mode=ParseMode.HTML)
        proj = st.projects[ukey][active]
        proj["git"] = {
            "org": context.args[1],
            "repo": context.args[2] if len(context.args) > 2 else active,
        }
        save_projects()
        await update.message.reply_text(
            f"\u2705 Git: <code>{E(proj['git']['org'])}/{E(proj['git']['repo'])}</code> for <b>{E(active)}</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    # /project deploy <target> | /project deploy cmd <command>
    if action == "deploy":
        active = st.active_project.get(ukey)
        if not active or active not in st.projects.get(ukey, {}):
            return await update.message.reply_text("No active project. Switch to one first.")
        if len(context.args) < 2:
            return await update.message.reply_text(
                "Usage:\n<code>/project deploy mac-mini|blue|aws</code>\n"
                "<code>/project deploy cmd &lt;command&gt;</code>",
                parse_mode=ParseMode.HTML,
            )
        proj = st.projects[ukey][active]
        if "deploy" not in proj:
            proj["deploy"] = {}

        if context.args[1].lower() == "cmd":
            proj["deploy"]["deploy_cmd"] = " ".join(context.args[2:])
            save_projects()
            await update.message.reply_text(f"\u2705 Deploy command set for <b>{E(active)}</b>", parse_mode=ParseMode.HTML)
            return

        target = context.args[1].lower()
        proj["deploy"]["target"] = target
        # If it's a remote machine, try to map to existing /machine name
        if target not in ("mac-mini", "local"):
            proj["deploy"]["machine"] = target
        save_projects()
        await update.message.reply_text(
            f"\u2705 Deploy target: <b>{E(target)}</b> for <b>{E(active)}</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    # /project info
    if action == "info":
        active = st.active_project.get(ukey)
        if not active or active not in st.projects.get(ukey, {}):
            return await update.message.reply_text("No active project. Use /project to list.")
        proj = st.projects[ukey][active]
        desc = proj.get("description", "")
        desc_str = f" — {E(desc)}" if desc else ""
        cwd = proj.get("cwd", "not set")
        git = proj.get("git", {})
        git_str = f"{git['org']}/{git['repo']}" if git.get("org") else "not set"
        deploy = proj.get("deploy", {})
        deploy_str = deploy.get("target", "not set")
        if deploy.get("deploy_cmd"):
            deploy_str += f" ({E(deploy['deploy_cmd'][:40])})"
        model = proj.get("model") or st.user_models.get(ukey) or CLAUDE_MODEL or "default"
        model_label = "(inherited)" if not proj.get("model") else ""
        env_keys = list(proj.get("env", {}).keys())
        env_str = ", ".join(env_keys) if env_keys else "none"
        pending = proj.get("pending", "")
        pending_str = f"\n  Pending: {E(pending)}" if pending else ""

        await update.message.reply_text(
            f"\U0001f4c2 <b>{E(active)}</b>{desc_str}\n\n"
            f"  CWD:    <code>{E(cwd)}</code>\n"
            f"  Git:    <code>{E(git_str)}</code>\n"
            f"  Deploy: <code>{E(deploy_str)}</code>\n"
            f"  Model:  <code>{E(model)}</code> {model_label}\n"
            f"  Env:    {E(env_str)}{pending_str}",
            parse_mode=ParseMode.HTML, reply_markup=build_back_button(),
        )
        return

    # /project dashboard
    if action == "dashboard":
        user_projects = st.projects.get(ukey, {})
        if not user_projects:
            return await update.message.reply_text("No projects yet. <code>/project new &lt;name&gt;</code>", parse_mode=ParseMode.HTML)
        active = st.active_project.get(ukey)
        lines = [fmt_section("PROJECTS")]
        buttons = []
        for name, proj in sorted(user_projects.items(), key=lambda x: x[1].get("last_used", 0), reverse=True):
            icon, label = project_status(proj)
            marker = "\u25b8" if name == active else " "
            ago = fmt_time_ago(proj.get("last_used", proj.get("created", 0)))
            msg_count = proj.get("msg_count_today", 0)
            msgs = f"{msg_count} msgs" if msg_count else "\u2014"
            pending = proj.get("pending", "")
            pending_flag = "  \u231b" if pending else ""
            lines.append(f"<code>{icon} {marker} {E(name):<12s} {ago:<8s} {msgs}{pending_flag}</code>")
            if pending:
                lines.append(f"<code>    \u231b {E(pending[:50])}</code>")
        # Switch buttons — 2 per row
        inactive = [(n, p) for n, p in sorted(user_projects.items(), key=lambda x: x[1].get("last_used", 0), reverse=True) if n != active]
        row = []
        for name, _ in inactive:
            row.append(InlineKeyboardButton(f"\u2192 {name}", callback_data=f"proj_sw:{name[:50]}"))
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        buttons.append([InlineKeyboardButton("\U0001f5d1 Clear Sessions", callback_data="proj_clear_all")])
        buttons.append(build_back_close())
        await update.message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    # /project off
    if action == "off":
        await _save_current_project(ukey)
        st.active_project.pop(ukey, None)
        st.user_sessions.pop(ukey, None)
        save_sessions()
        save_projects()
        await update.message.reply_text(
            "\U0001f4c2 Project mode deactivated. Back to default conversation.",
            reply_markup=build_back_button(),
        )
        return

    # /project <name> — quick switch
    name = context.args[0]
    if name not in st.projects.get(ukey, {}):
        return await update.message.reply_text(
            f"No project: <code>{E(name)}</code>. Use <code>/project new {E(name)}</code> to create.",
            parse_mode=ParseMode.HTML,
        )
    # Save current + get pending summary
    old_project = st.active_project.get(ukey)
    if old_project and old_project != name and old_project in st.projects.get(ukey, {}):
        pending = await _get_pending_summary(ukey)
        if pending:
            st.projects[ukey][old_project]["pending"] = pending
        await _save_current_project(ukey)

    await _switch_to_project(ukey, name)
    proj = st.projects[ukey][name]
    ago = _fmt_time_ago(proj.get("last_used", proj.get("created", 0)))
    desc = proj.get("description", "")
    desc_str = f" — {E(desc)}" if desc else ""
    has_session = "Conversation restored." if proj.get("session_id") else "Fresh conversation."
    await update.message.reply_text(
        f"\U0001f4c2 <b>Switched to:</b> <code>{E(name)}</code>{desc_str}\n"
        f"{has_session}",
        parse_mode=ParseMode.HTML,
    )
