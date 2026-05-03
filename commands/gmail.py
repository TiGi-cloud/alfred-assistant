"""Alfred /gmail command — read and send emails from Telegram."""
from __future__ import annotations

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from core import is_allowed, deny, user_key, check_cmd_rate, build_back_button
from utils.formatting import E
from utils.gmail import read_emails, send_email, list_labels, HAS_GMAIL


async def cmd_gmail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /gmail command."""
    if not is_allowed(update):
        return await deny(update)
    if not check_cmd_rate(update, "gmail"):
        return

    if not HAS_GMAIL:
        await update.message.reply_text(
            "📧 Gmail not configured.\n\n"
            "Set these env vars (via <code>/env</code>):\n"
            "• <code>GMAIL_ADDRESS</code> — your Gmail\n"
            "• <code>GMAIL_APP_PASSWORD</code> — App Password\n\n"
            "Get an App Password at:\nmyaccount.google.com → Security → 2-Step → App passwords",
            parse_mode=ParseMode.HTML,
        )
        return

    args = context.args or []

    if not args:
        return await _show_inbox(update)

    sub = args[0].lower()

    if sub == "read" or sub == "inbox":
        count = 5
        if len(args) >= 2 and args[1].isdigit():
            count = min(int(args[1]), 20)
        return await _show_inbox(update, count)

    elif sub == "search" and len(args) >= 2:
        query = " ".join(args[1:])
        return await _search_mail(update, query)

    elif sub == "send" and len(args) >= 4:
        # /gmail send to@email.com Subject | Body
        to = args[1]
        rest = " ".join(args[2:])
        if "|" in rest:
            subject, body = rest.split("|", 1)
        else:
            subject = rest
            body = ""
        result = send_email(to.strip(), subject.strip(), body.strip())
        if result == "sent":
            await update.message.reply_text(f"✅ Email sent to {E(to)}", parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text(f"❌ Failed: {E(result)}", parse_mode=ParseMode.HTML)

    elif sub == "labels":
        labels = list_labels()
        text = "📧 <b>Gmail Labels</b>\n" + "\n".join(f"• {E(l)}" for l in labels[:30])
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    else:
        await update.message.reply_text(
            "📧 <b>Gmail</b>\n"
            "<code>/gmail</code> — show inbox (5 recent)\n"
            "<code>/gmail read 10</code> — read N emails\n"
            "<code>/gmail search &lt;query&gt;</code> — search emails\n"
            "<code>/gmail send to@email Subject | Body</code> — send\n"
            "<code>/gmail labels</code> — list folders",
            parse_mode=ParseMode.HTML,
        )


async def _show_inbox(update: Update, count: int = 5):
    msg = await update.message.reply_text("📧 Fetching inbox...")
    emails = read_emails(count)

    if not emails:
        await msg.edit_text("📧 Inbox is empty.")
        return

    if emails and "error" in emails[0]:
        await msg.edit_text(f"❌ {emails[0]['error']}")
        return

    lines = [f"📧 <b>Inbox</b> ({len(emails)} recent)\n"]
    for i, em in enumerate(emails):
        sender = em["from"]
        # Shorten sender
        if "<" in sender:
            name = sender.split("<")[0].strip().strip('"')
            if name:
                sender = name
        lines.append(
            f"<b>{i+1}.</b> {E(sender[:30])}\n"
            f"   📋 {E(em['subject'][:60])}\n"
            f"   📅 {E(em['date'][:25])}\n"
            f"   {E(em['body'][:100])}{'...' if len(em['body'])>100 else ''}\n"
        )

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n..."

    await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=build_back_button())


async def _search_mail(update: Update, query: str):
    msg = await update.message.reply_text(f"🔍 Searching for '{E(query)}'...", parse_mode=ParseMode.HTML)
    emails = read_emails(10, search=query)

    if not emails:
        await msg.edit_text(f"No emails matching '{E(query)}'.", parse_mode=ParseMode.HTML)
        return

    if emails and "error" in emails[0]:
        await msg.edit_text(f"❌ {emails[0]['error']}")
        return

    lines = [f"🔍 <b>Results for '{E(query)}'</b> ({len(emails)})\n"]
    for i, em in enumerate(emails):
        sender = em["from"]
        if "<" in sender:
            name = sender.split("<")[0].strip().strip('"')
            if name:
                sender = name
        lines.append(
            f"<b>{i+1}.</b> {E(sender[:30])} — {E(em['subject'][:50])}\n"
            f"   {E(em['body'][:80])}{'...' if len(em['body'])>80 else ''}\n"
        )

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n..."
    await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=build_back_button())
