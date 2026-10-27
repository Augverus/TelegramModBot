import asyncio
import json
import logging
import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from telegram import Update
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ChatMemberHandler,
    MessageHandler,
    filters,
)

BOT_TOKEN = "8807435099:AAE2BWVh1jrrBA9J8KCXurV1pNArTGiGeMA"
STATE_FILE = Path(os.getenv("STATE_FILE", "abuse_state.json"))
THRESHOLD = int(os.getenv("THRESHOLD", "5"))
WINDOW_HOURS = int(os.getenv("WINDOW_HOURS", "48"))
PUNISH_COOLDOWN_HOURS = int(os.getenv("PUNISH_COOLDOWN_HOURS", "24"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

DEFAULT_STATE: dict[str, Any] = {
    "events": [],
    "punishments": {},
}

STATE_LOCK = asyncio.Lock()

OWNER_STATUSES = {ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR}
REMOVED_STATUSES = {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def ensure_state_shape(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return deepcopy(DEFAULT_STATE)

    events = data.get("events", [])
    punishments = data.get("punishments", {})

    if not isinstance(events, list):
        events = []
    if not isinstance(punishments, dict):
        punishments = {}

    data["events"] = events
    data["punishments"] = punishments
    return data


def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return deepcopy(DEFAULT_STATE)

    try:
        return ensure_state_shape(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except Exception as exc:
        logging.warning("Failed to load state file: %s", exc)
        return deepcopy(DEFAULT_STATE)


def save_state(state: dict[str, Any]) -> None:
    tmp_file = STATE_FILE.with_suffix(".tmp")
    tmp_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_file.replace(STATE_FILE)


def label_user(user) -> str:
    if not user:
        return "unknown"
    if getattr(user, "username", None):
        return f"@{user.username}"
    full_name = " ".join(part for part in [getattr(user, "first_name", ""), getattr(user, "last_name", "")] if part).strip()
    return full_name or str(user.id)


def is_admin_like(status: str | None) -> bool:
    return status in OWNER_STATUSES


def is_removed_status(status: str | None) -> bool:
    return status in REMOVED_STATUSES


def prune_state(state: dict[str, Any], now: datetime) -> None:
    window = timedelta(hours=WINDOW_HOURS)
    cooldown = timedelta(hours=max(PUNISH_COOLDOWN_HOURS, WINDOW_HOURS))

    state["events"] = [
        event for event in state["events"]
        if (ts := parse_dt(event.get("timestamp"))) and now - ts <= window
    ]

    state["punishments"] = {
        key: value
        for key, value in state["punishments"].items()
        if (ts := parse_dt(value)) and now - ts <= cooldown
    }


async def safe_get_member_status(bot, chat_id: int, user_id: int) -> str | None:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status
    except TelegramError as exc:
        logging.warning("Failed to get status for user %s in chat %s: %s", user_id, chat_id, exc)
        return None


async def strip_admin_rights(bot, chat_id: int, user_id: int) -> None:
    await bot.promote_chat_member(
        chat_id=chat_id,
        user_id=user_id,
        can_change_info=False,
        can_post_messages=False,
        can_edit_messages=False,
        can_delete_messages=False,
        can_invite_users=False,
        can_restrict_members=False,
        can_pin_messages=False,
        can_manage_topics=False,
        can_promote_members=False,
        is_anonymous=False,
    )


async def punish_admin(bot, chat_id: int, user_id: int) -> tuple[bool, bool, str]:
    demoted = False
    banned = False
    reason = ""

    try:
        await strip_admin_rights(bot, chat_id, user_id)
        await asyncio.sleep(1)
    except (BadRequest, Forbidden) as exc:
        reason = f"Demotion failed: {exc}"
        logging.warning("Failed to demote user %s in chat %s: %s", user_id, chat_id, exc)
    except Exception as exc:
        reason = f"Demotion failed: {exc}"
        logging.exception("Unexpected error while demoting user %s in chat %s", user_id, chat_id)

    status = await safe_get_member_status(bot, chat_id, user_id)
    if status is not None and not is_admin_like(status):
        demoted = True

    if not demoted:
        if not reason:
            reason = "Target is still an administrator, so banning is not possible."
        return demoted, banned, reason

    try:
        await bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
        banned = True
    except (BadRequest, Forbidden) as exc:
        reason = f"Ban failed after demotion: {exc}"
        logging.warning("Failed to ban user %s in chat %s: %s", user_id, chat_id, exc)
    except Exception as exc:
        reason = f"Ban failed after demotion: {exc}"
        logging.exception("Unexpected error while banning user %s in chat %s", user_id, chat_id)

    return demoted, banned, reason


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Bot is running.")


async def chatinfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return
    await update.effective_message.reply_text(
        f"chat_id: <code>{chat.id}</code>\n"
        f"chat_type: <code>{chat.type}</code>\n"
        f"title: <code>{chat.title or 'N/A'}</code>",
        parse_mode=ParseMode.HTML,
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return

    member_status = await safe_get_member_status(context.bot, chat.id, user.id)
    await update.effective_message.reply_text(
        f"Your status in this chat is: <code>{member_status}</code>",
        parse_mode=ParseMode.HTML,
    )


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    event = update.chat_member
    if event is None:
        return

    chat = event.chat
    actor = event.from_user
    target = event.new_chat_member.user if event.new_chat_member else None
    old_status = event.old_chat_member.status if event.old_chat_member else None
    new_status = event.new_chat_member.status if event.new_chat_member else None

    logging.info(
        "chat_member update chat_id=%s type=%s actor=%s target=%s old=%s new=%s",
        chat.id,
        chat.type,
        actor.id if actor else None,
        target.id if target else None,
        old_status,
        new_status,
    )

    if chat.type == "channel":
        logging.info("Ignoring channel chat_member update for chat_id=%s", chat.id)
        return

    if chat.type not in {"group", "supergroup"}:
        logging.info("Ignoring non-group chat update for chat_id=%s type=%s", chat.id, chat.type)
        return

    if not actor or not target:
        return

    if actor.id == target.id:
        return

    if not is_removed_status(old_status) or is_removed_status(new_status) is False:
        return

    actor_status = await safe_get_member_status(context.bot, chat.id, actor.id)
    if not is_admin_like(actor_status):
        logging.info("Ignoring removal by non-admin actor=%s status=%s", actor.id, actor_status)
        return

    now = utc_now()
    punish_key = f"{chat.id}:{actor.id}"

    async with STATE_LOCK:
        state = load_state()
        prune_state(state, now)

        state["events"].append(
            {
                "chat_id": chat.id,
                "actor_id": actor.id,
                "actor_name": label_user(actor),
                "target_id": target.id,
                "target_name": label_user(target),
                "timestamp": now.isoformat(),
            }
        )

        count = sum(
            1
            for event in state["events"]
            if event.get("chat_id") == chat.id and event.get("actor_id") == actor.id
        )

        last_punished = parse_dt(state["punishments"].get(punish_key))
        on_cooldown = (
            last_punished is not None
            and now - last_punished < timedelta(hours=PUNISH_COOLDOWN_HOURS)
        )

        should_punish = count > THRESHOLD and not on_cooldown
        if should_punish:
            state["punishments"][punish_key] = now.isoformat()

        save_state(state)

    logging.info(
        "Removal detected chat_id=%s actor_id=%s target_id=%s count=%s",
        chat.id,
        actor.id,
        target.id,
        count,
    )

    if not should_punish:
        return

    demoted, banned, reason = await punish_admin(context.bot, chat.id, actor.id)

    if demoted and banned:
        text = (
            f"✅ Admin <b>{label_user(actor)}</b> exceeded the limit and was demoted and banned.\n"
            f"Removals in last {WINDOW_HOURS}h: <code>{count}</code>"
        )
    elif demoted and not banned:
        text = (
            f"⚠️ Admin <b>{label_user(actor)}</b> was demoted, but banning failed.\n"
            f"Reason: <code>{reason}</code>\n"
            f"Removals in last {WINDOW_HOURS}h: <code>{count}</code>"
        )
    else:
        text = (
            f"❌ Abuse detected for <b>{label_user(actor)}</b>, but punishment failed.\n"
            f"Reason: <code>{reason}</code>\n"
            f"Removals in last {WINDOW_HOURS}h: <code>{count}</code>"
        )

    try:
        await context.bot.send_message(chat.id, text, parse_mode=ParseMode.HTML)
    except TelegramError as exc:
        logging.warning("Failed to send punishment message in chat %s: %s", chat.id, exc)


async def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chatinfo", chatinfo))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ALL, lambda update, context: None))

    logging.info("Bot started")
    await app.run_polling(allowed_updates=["message", "chat_member", "my_chat_member"])


if __name__ == "__main__":
    asyncio.run(main())
