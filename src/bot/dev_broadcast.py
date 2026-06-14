"""
dev_broadcast.py — BroadcastCommandsMixin

Provides /broadcast, /broadcast_confirm, /delbroadcast, /delbroadcast_confirm
for the DeveloperCommands class.  All methods expect self.db and self.quiz_manager
to be available (set by DeveloperCommands.__init__).
"""

import html
import logging
import asyncio
import time
from datetime import datetime
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from src.bot.ui import UI

logger = logging.getLogger(__name__)


class BroadcastCommandsMixin:
    """Broadcast and delete-broadcast command handlers."""

    async def broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Enhanced broadcast supporting media, buttons, placeholders, and auto-cleanup."""
        start_time = time.time()
        try:
            if not await self.check_access(update):
                await self.send_unauthorized_message(update)
                return

            if not update.effective_user or not update.effective_chat or not update.message:
                return

            # Determine media type and recipient counts for logging (PM-accessible users only)
            users = await asyncio.to_thread(self.db.get_pm_accessible_users)
            groups = await asyncio.to_thread(self.db.get_active_groups)
            total_targets = len(users) + len(groups)

            # Determine initial media type for logging
            if update.message.reply_to_message:
                replied_msg = update.message.reply_to_message
                if replied_msg.photo:
                    media_type = 'photo'
                elif replied_msg.video:
                    media_type = 'video'
                elif replied_msg.document:
                    media_type = 'document'
                elif replied_msg.animation:
                    media_type = 'animation'
                else:
                    media_type = 'forward'
            elif context.args:
                media_type = 'text'
            else:
                media_type = 'help'

            # Log command execution immediately
            await asyncio.to_thread(
                self.db.log_activity,
                activity_type='command',
                user_id=update.effective_user.id,
                chat_id=update.effective_chat.id,
                username=update.effective_user.username or "",
                chat_title=getattr(update.effective_chat, 'title', None) or "",
                command='/broadcast',
                details={'recipient_count': total_targets, 'media_type': media_type,
                         'users': len(users), 'groups': len(groups)},
                success=True
            )

            # Check if replying to a message
            if update.message.reply_to_message:
                replied_message = update.message.reply_to_message

                users = await asyncio.to_thread(self.db.get_pm_accessible_users)
                groups = await asyncio.to_thread(self.db.get_active_groups)
                total_targets = len(users) + len(groups)

                # Detect media type
                media_type = None
                media_file_id = None
                media_caption = None
                media_preview = ""

                if replied_message.photo:
                    media_type = 'photo'
                    media_file_id = replied_message.photo[-1].file_id
                    media_caption = replied_message.caption
                    media_preview = "📷 Photo"
                    logger.info("Detected photo in broadcast")
                elif replied_message.video:
                    media_type = 'video'
                    media_file_id = replied_message.video.file_id
                    media_caption = replied_message.caption
                    media_preview = "🎥 Video"
                    logger.info("Detected video in broadcast")
                elif replied_message.document:
                    media_type = 'document'
                    media_file_id = replied_message.document.file_id
                    media_caption = replied_message.caption
                    media_preview = "📄 Document"
                    logger.info("Detected document in broadcast")
                elif replied_message.animation:
                    media_type = 'animation'
                    media_file_id = replied_message.animation.file_id
                    media_caption = replied_message.caption
                    media_preview = "🎬 GIF/Animation"
                    logger.info("Detected animation in broadcast")

                confirm_text = f"📢 Broadcast Confirmation\n\n"

                if media_type:
                    confirm_text += f"Type: {media_preview}\n"
                    if media_caption:
                        confirm_text += f"Caption: {media_caption[:100]}{'...' if len(media_caption) > 100 else ''}\n"
                    confirm_text += f"\n"
                else:
                    confirm_text += f"Forwarding message to:\n"

                confirm_text += f"Recipients:\n"
                confirm_text += f"• {len(users)} users\n"
                confirm_text += f"• {len(groups)} groups\n"
                confirm_text += f"• Total: {total_targets} recipients\n\n"
                confirm_text += f"Confirm: /broadcast_confirm"

                # Store broadcast data
                if media_type:
                    if context.user_data is not None:
                        context.user_data['broadcast_type'] = media_type
                    if context.user_data is not None:
                        context.user_data['broadcast_media_id'] = media_file_id
                    if context.user_data is not None:
                        context.user_data['broadcast_caption'] = media_caption
                else:
                    if context.user_data is not None:
                        context.user_data['broadcast_message_id'] = replied_message.message_id
                    if context.user_data is not None:
                        context.user_data['broadcast_chat_id'] = replied_message.chat_id
                    if context.user_data is not None:
                        context.user_data['broadcast_type'] = 'forward'

                reply = await update.message.reply_text(confirm_text)
                logger.info(f"Broadcast ({media_type or 'forward'}) prepared by {update.effective_user.id}")

            elif context.args:
                message_text = ' '.join(context.args)

                # Parse inline buttons from text
                cleaned_text, reply_markup = self.parse_inline_buttons(message_text)

                users = await asyncio.to_thread(self.db.get_pm_accessible_users)
                groups = await asyncio.to_thread(self.db.get_active_groups)
                total_targets = len(users) + len(groups)

                confirm_text = f"📢 Broadcast Confirmation\n\n"
                confirm_text += f"Message: {cleaned_text[:200]}{'...' if len(cleaned_text) > 200 else ''}\n\n"

                if reply_markup:
                    button_count = sum(len(row) for row in reply_markup.inline_keyboard)
                    confirm_text += f"🔘 Buttons: {button_count} inline button(s)\n\n"

                confirm_text += f"Recipients:\n"
                confirm_text += f"• {len(users)} users\n"
                confirm_text += f"• {len(groups)} groups\n"
                confirm_text += f"• Total: {total_targets} recipients\n\n"
                confirm_text += f"Confirm: /broadcast_confirm"

                if context.user_data is not None:
                    context.user_data['broadcast_message'] = cleaned_text
                if context.user_data is not None:
                    context.user_data['broadcast_buttons'] = reply_markup
                if context.user_data is not None:
                    context.user_data['broadcast_type'] = 'text'

                reply = await update.message.reply_text(confirm_text)
                logger.info(f"Broadcast (text) prepared by {update.effective_user.id}")

            else:
                reply = await update.message.reply_text(
                    "📢 Broadcast Message\n\n"
                    "Usage:\n"
                    "1. Reply to a message/media with /broadcast\n"
                    "2. /broadcast [message text]\n"
                    "3. /broadcast Message [[\"Button\",\"URL\"]]\n\n"
                    "Supported media: Photos, Videos, Documents, GIFs\n"
                    "Placeholders: {first_name}, {username}, {chat_title}, {bot_name}"
                )
                await self.auto_clean_message(update.message, reply)

            response_time = int((time.time() - start_time) * 1000)
            logger.debug(f"Command /broadcast completed in {response_time}ms")

        except Exception as e:
            response_time = int((time.time() - start_time) * 1000)
            if update.effective_user and update.effective_chat:
                await asyncio.to_thread(
                    self.db.log_activity,
                    activity_type='error',
                    user_id=update.effective_user.id,
                    chat_id=update.effective_chat.id,
                    command='/broadcast',
                    details={'error': str(e)},
                    success=False,
                    response_time_ms=response_time
                )
            logger.error(f"Error in broadcast: {e}", exc_info=True)
            if update.message:
                reply = await update.message.reply_text("❌ Error preparing broadcast")
                await self.auto_clean_message(update.message, reply)

    async def broadcast_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Confirm and send broadcast with media, buttons, placeholders, and auto-cleanup."""
        start_time = time.time()
        try:
            if not await self.check_access(update):
                await self.send_unauthorized_message(update)
                return

            if not update.effective_user or not update.effective_chat or not update.message:
                return

            # Log command execution immediately
            broadcast_type = context.user_data.get('broadcast_type', 'unknown') if context.user_data else 'unknown'
            await asyncio.to_thread(
                self.db.log_activity,
                activity_type='command',
                user_id=update.effective_user.id,
                chat_id=update.effective_chat.id,
                username=update.effective_user.username or "",
                chat_title=getattr(update.effective_chat, 'title', None) or "",
                command='/broadcast_confirm',
                details={'broadcast_type': broadcast_type, 'action': 'confirm_broadcast'},
                success=True
            )

            broadcast_type = context.user_data.get('broadcast_type') if context.user_data else None

            # Track sent messages for deletion feature
            sent_messages = {}
            if not broadcast_type:
                reply = await update.message.reply_text("❌ No broadcast found. Please use /broadcast first.")
                await self.auto_clean_message(update.message, reply)
                return

            status = await update.message.reply_text("📢 Sending broadcast...")

            # Get PM-accessible users and active groups for broadcast
            users = await asyncio.to_thread(self.db.get_pm_accessible_users)
            groups = await asyncio.to_thread(self.db.get_active_groups)  # excludes bot_blocked/inactive

            success_count = 0
            fail_count = 0
            pm_sent = 0
            group_sent = 0
            skipped_count = 0  # Auto-removed users/groups

            # Create unique broadcast ID for tracking
            broadcast_id = f"broadcast_{int(time.time())}_{update.effective_user.id}"

            # Cache bot name once to avoid repeated API calls per recipient
            bot_name_cache = context.bot.first_name if context.bot.first_name else "Bot"

            # Get broadcast data based on type
            if broadcast_type == 'forward':
                message_id = context.user_data.get('broadcast_message_id') if context.user_data else None
                chat_id = context.user_data.get('broadcast_chat_id') if context.user_data else None

                if not message_id or not chat_id:
                    reply = await update.message.reply_text("❌ Missing broadcast data. Please use /broadcast again.")
                    await self.auto_clean_message(update.message, reply)
                    return

                # Send to users (PM)
                for user in users:
                    try:
                        sent_msg = await context.bot.copy_message(
                            chat_id=user['user_id'],
                            from_chat_id=chat_id,
                            message_id=message_id
                        )
                        sent_messages[user['user_id']] = sent_msg.message_id
                        success_count += 1
                        pm_sent += 1
                        if len(users) > 20:
                            await asyncio.sleep(0.03)
                    except Exception as e:
                        error_msg = str(e)
                        if "Forbidden: bot was blocked by the user" in error_msg:
                            logger.info(f"AUTO-CLEANUP: Removing user {user['user_id']} - {error_msg}")
                            await asyncio.to_thread(self.db.remove_inactive_user, user['user_id'])
                            skipped_count += 1
                        elif "Forbidden: user is deactivated" in error_msg:
                            logger.info(f"AUTO-CLEANUP: Removing user {user['user_id']} - {error_msg}")
                            await asyncio.to_thread(self.db.remove_inactive_user, user['user_id'])
                            skipped_count += 1
                        elif "Forbidden" in error_msg:
                            logger.warning(f"SAFETY: Not removing user {user['user_id']} - error was: {error_msg}")
                            fail_count += 1
                        else:
                            logger.warning(f"Failed to send to user {user['user_id']}: {error_msg}")
                            fail_count += 1

                # Send to groups
                for group in groups:
                    try:
                        sent_msg = await context.bot.copy_message(
                            chat_id=group['chat_id'],
                            from_chat_id=chat_id,
                            message_id=message_id
                        )
                        sent_messages[group['chat_id']] = sent_msg.message_id
                        success_count += 1
                        group_sent += 1
                        if len(groups) > 20:
                            await asyncio.sleep(0.03)
                    except Exception as e:
                        error_msg = str(e)
                        if any(keyword in error_msg.lower() for keyword in [
                            "bot was kicked", "bot is not a member", "chat not found",
                            "group chat was deactivated", "chat has been deleted", "forum topic is closed"
                        ]):
                            logger.info(f"AUTO-CLEANUP: Removing group {group['chat_id']} - {error_msg}")
                            await asyncio.to_thread(self.db.remove_inactive_group, group['chat_id'])
                            if hasattr(self, 'quiz_manager'):
                                self.quiz_manager.remove_active_chat(group['chat_id'])
                            skipped_count += 1
                        elif "Forbidden" in error_msg:
                            logger.warning(f"SAFETY: Not auto-removing group {group['chat_id']} - error: {error_msg}")
                            fail_count += 1
                        else:
                            logger.warning(f"Failed to send to group {group['chat_id']}: {error_msg}")
                            fail_count += 1

            elif broadcast_type in ['photo', 'video', 'document', 'animation']:
                media_file_id = context.user_data.get('broadcast_media_id') if context.user_data else None
                base_caption = context.user_data.get('broadcast_caption') if context.user_data else None
                reply_markup = context.user_data.get('broadcast_buttons') if context.user_data else None

                if not media_file_id:
                    reply = await update.message.reply_text("❌ Missing media file ID. Please use /broadcast again.")
                    await self.auto_clean_message(update.message, reply)
                    return

                if base_caption is None:
                    base_caption = ""

                if len(base_caption) > 1024:
                    base_caption = base_caption[:1021] + "..."
                    logger.warning(f"Caption truncated to 1024 chars for broadcast")

                # Send to users (PM)
                for user in users:
                    try:
                        caption = await self.replace_placeholders(base_caption or "", user['user_id'], context,
                            user_data=user, bot_name_cache=bot_name_cache)

                        if broadcast_type == 'photo':
                            sent_msg = await context.bot.send_photo(
                                chat_id=user['user_id'], photo=media_file_id,
                                caption=caption if caption else None, reply_markup=reply_markup)
                        elif broadcast_type == 'video':
                            sent_msg = await context.bot.send_video(
                                chat_id=user['user_id'], video=media_file_id,
                                caption=caption if caption else None, reply_markup=reply_markup)
                        elif broadcast_type == 'document':
                            sent_msg = await context.bot.send_document(
                                chat_id=user['user_id'], document=media_file_id,
                                caption=caption if caption else None, reply_markup=reply_markup)
                        elif broadcast_type == 'animation':
                            sent_msg = await context.bot.send_animation(
                                chat_id=user['user_id'], animation=media_file_id,
                                caption=caption if caption else None, reply_markup=reply_markup)
                        else:
                            continue

                        sent_messages[user['user_id']] = sent_msg.message_id
                        success_count += 1
                        pm_sent += 1
                        if len(users) > 20:
                            await asyncio.sleep(0.03)
                    except Exception as e:
                        error_msg = str(e)
                        if "Forbidden: bot was blocked by the user" in error_msg:
                            logger.info(f"AUTO-CLEANUP: Removing user {user['user_id']} - {error_msg}")
                            await asyncio.to_thread(self.db.remove_inactive_user, user['user_id'])
                            skipped_count += 1
                        elif "Forbidden: user is deactivated" in error_msg:
                            logger.info(f"AUTO-CLEANUP: Removing user {user['user_id']} - {error_msg}")
                            await asyncio.to_thread(self.db.remove_inactive_user, user['user_id'])
                            skipped_count += 1
                        elif "Forbidden" in error_msg:
                            logger.warning(f"SAFETY: Not removing user {user['user_id']} - error was: {error_msg}")
                            fail_count += 1
                        else:
                            logger.warning(f"Failed to send to user {user['user_id']}: {error_msg}")
                            fail_count += 1

                # Send to groups
                for group in groups:
                    try:
                        caption = await self.replace_placeholders(base_caption or "", group['chat_id'], context,
                            group_data=group, bot_name_cache=bot_name_cache)

                        if broadcast_type == 'photo':
                            sent_msg = await context.bot.send_photo(
                                chat_id=group['chat_id'], photo=media_file_id,
                                caption=caption if caption else None, reply_markup=reply_markup)
                        elif broadcast_type == 'video':
                            sent_msg = await context.bot.send_video(
                                chat_id=group['chat_id'], video=media_file_id,
                                caption=caption if caption else None, reply_markup=reply_markup)
                        elif broadcast_type == 'document':
                            sent_msg = await context.bot.send_document(
                                chat_id=group['chat_id'], document=media_file_id,
                                caption=caption if caption else None, reply_markup=reply_markup)
                        elif broadcast_type == 'animation':
                            sent_msg = await context.bot.send_animation(
                                chat_id=group['chat_id'], animation=media_file_id,
                                caption=caption if caption else None, reply_markup=reply_markup)
                        else:
                            continue

                        sent_messages[group['chat_id']] = sent_msg.message_id
                        success_count += 1
                        group_sent += 1
                        if len(groups) > 20:
                            await asyncio.sleep(0.03)
                    except Exception as e:
                        error_msg = str(e)
                        if any(keyword in error_msg.lower() for keyword in [
                            "bot was kicked", "bot is not a member", "chat not found",
                            "group chat was deactivated", "chat has been deleted", "forum topic is closed"
                        ]):
                            logger.info(f"AUTO-CLEANUP: Removing group {group['chat_id']} - {error_msg}")
                            await asyncio.to_thread(self.db.remove_inactive_group, group['chat_id'])
                            if hasattr(self, 'quiz_manager'):
                                self.quiz_manager.remove_active_chat(group['chat_id'])
                            skipped_count += 1
                        elif "Forbidden" in error_msg:
                            logger.warning(f"SAFETY: Not auto-removing group {group['chat_id']} - error: {error_msg}")
                            fail_count += 1
                        else:
                            logger.warning(f"Failed to send to group {group['chat_id']}: {error_msg}")
                            fail_count += 1

            else:  # text broadcast with buttons and placeholders
                base_message_text = context.user_data.get('broadcast_message') if context.user_data else None
                reply_markup = context.user_data.get('broadcast_buttons') if context.user_data else None

                # Send to users (PM)
                for user in users:
                    try:
                        message_text = await self.replace_placeholders(base_message_text or "", user['user_id'], context,
                            user_data=user, bot_name_cache=bot_name_cache)
                        try:
                            sent_msg = await context.bot.send_message(
                                chat_id=user['user_id'], text=message_text,
                                parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
                        except Exception as parse_error:
                            if "parse entities" in str(parse_error).lower() or "can't parse" in str(parse_error).lower():
                                logger.warning(f"Markdown parse error for user {user['user_id']}, falling back to plain text")
                                sent_msg = await context.bot.send_message(
                                    chat_id=user['user_id'], text=message_text,
                                    parse_mode=None, reply_markup=reply_markup)
                            else:
                                raise

                        sent_messages[user['user_id']] = sent_msg.message_id
                        success_count += 1
                        pm_sent += 1
                        if len(users) > 20:
                            await asyncio.sleep(0.03)
                    except Exception as e:
                        error_msg = str(e)
                        if "Forbidden: bot was blocked by the user" in error_msg:
                            logger.info(f"AUTO-CLEANUP: Removing user {user['user_id']} - {error_msg}")
                            await asyncio.to_thread(self.db.remove_inactive_user, user['user_id'])
                            skipped_count += 1
                        elif "Forbidden: user is deactivated" in error_msg:
                            logger.info(f"AUTO-CLEANUP: Removing user {user['user_id']} - {error_msg}")
                            await asyncio.to_thread(self.db.remove_inactive_user, user['user_id'])
                            skipped_count += 1
                        elif "Forbidden" in error_msg:
                            logger.warning(f"SAFETY: Not removing user {user['user_id']} - error was: {error_msg}")
                            fail_count += 1
                        else:
                            logger.warning(f"Failed to send to user {user['user_id']}: {error_msg}")
                            fail_count += 1

                # Send to groups
                for group in groups:
                    try:
                        message_text = await self.replace_placeholders(base_message_text or "", group['chat_id'], context,
                            group_data=group, bot_name_cache=bot_name_cache)
                        try:
                            sent_msg = await context.bot.send_message(
                                chat_id=group['chat_id'], text=message_text,
                                parse_mode=ParseMode.MARKDOWN, reply_markup=reply_markup)
                        except Exception as parse_error:
                            if "parse entities" in str(parse_error).lower() or "can't parse" in str(parse_error).lower():
                                logger.warning(f"Markdown parse error for group {group['chat_id']}, falling back to plain text")
                                sent_msg = await context.bot.send_message(
                                    chat_id=group['chat_id'], text=message_text,
                                    parse_mode=None, reply_markup=reply_markup)
                            else:
                                raise

                        sent_messages[group['chat_id']] = sent_msg.message_id
                        success_count += 1
                        group_sent += 1
                        if len(groups) > 20:
                            await asyncio.sleep(0.03)
                    except Exception as e:
                        error_msg = str(e)
                        if any(keyword in error_msg.lower() for keyword in [
                            "bot was kicked", "bot is not a member", "chat not found",
                            "group chat was deactivated", "chat has been deleted", "forum topic is closed"
                        ]):
                            logger.info(f"AUTO-CLEANUP: Removing group {group['chat_id']} - {error_msg}")
                            await asyncio.to_thread(self.db.remove_inactive_group, group['chat_id'])
                            if hasattr(self, 'quiz_manager'):
                                self.quiz_manager.remove_active_chat(group['chat_id'])
                            skipped_count += 1
                        elif "Forbidden" in error_msg:
                            logger.warning(f"SAFETY: Not auto-removing group {group['chat_id']} - error: {error_msg}")
                            fail_count += 1
                        else:
                            logger.warning(f"Failed to send to group {group['chat_id']}: {error_msg}")
                            fail_count += 1

            # Store broadcast in database so /delbroadcast can find the messages
            total_targets = len(users) + len(groups)
            message_text = (context.user_data.get('broadcast_message', '') if context.user_data else '')[:500] \
                if broadcast_type == 'text' else f"[{broadcast_type.upper()} BROADCAST]"
            if sent_messages:
                await asyncio.to_thread(
                    self.db.save_broadcast,
                    {
                        "broadcast_id":  broadcast_id,
                        "user_id":       update.effective_user.id,
                        "messages":      {str(k): v for k, v in sent_messages.items()},
                        "admin_id":      update.effective_user.id,
                        "message_text":  message_text,
                        "total_targets": total_targets,
                        "sent_count":    success_count,
                        "failed_count":  fail_count,
                        "skipped_count": skipped_count,
                        "created_at":    datetime.utcnow().isoformat(),
                    }
                )
                logger.info(f"Saved broadcast {broadcast_id} to database with {len(sent_messages)} messages")

            # Build result message
            LINE = "━" * 38
            extra = ""
            if skipped_count > 0:
                extra += f"│  Auto-Cleaned  ›  <b>{skipped_count}</b>  (blocked/inactive)\n"
            if fail_count > 0:
                extra += f"│  Skipped       ›  <b>{fail_count}</b>  (no access)\n"

            result_text = (
                f"📢  <b>𝐁𝐑𝐎𝐀𝐃𝐂𝐀𝐒𝐓  𝐂𝐎𝐌𝐏𝐋𝐄𝐓𝐄𝐃</b>\n"
                f"{LINE}\n\n"
                f"📬  <b>𝐃𝐄𝐋𝐈𝐕𝐄𝐑𝐘  𝐑𝐄𝐏𝐎𝐑𝐓</b>\n"
                f"╭──────────────────────────────────────╮\n"
                f"│  PM Users      ›  <b>{pm_sent}</b>\n"
                f"│  Groups        ›  <b>{group_sent}</b>\n"
                f"│  Total Sent    ›  <b>{success_count}</b>\n"
                f"{extra}"
                f"╰──────────────────────────────────────╯\n\n"
                f"{LINE}\n"
                f"🗑️  Use /delbroadcast to undo\n"
                f"{LINE}"
            )

            await status.edit_text(result_text, parse_mode=ParseMode.HTML)

            logger.info(
                f"Broadcast completed by {update.effective_user.id}: "
                f"{pm_sent} PMs, {group_sent} groups ({success_count} total, "
                f"{fail_count} failed, {skipped_count} auto-removed)"
            )

            # Clear broadcast data from user session
            for key in ('broadcast_message', 'broadcast_message_id', 'broadcast_chat_id',
                        'broadcast_type', 'broadcast_media_id', 'broadcast_caption',
                        'broadcast_buttons'):
                if context.user_data is not None:
                    context.user_data.pop(key, None)

            response_time = int((time.time() - start_time) * 1000)
            logger.debug(f"Command /broadcast_confirm completed in {response_time}ms - sent: {success_count}, failed: {fail_count}")

        except Exception as e:
            response_time = int((time.time() - start_time) * 1000)
            if update.effective_user and update.effective_chat:
                await asyncio.to_thread(
                    self.db.log_activity,
                    activity_type='error',
                    user_id=update.effective_user.id,
                    chat_id=update.effective_chat.id,
                    command='/broadcast_confirm',
                    details={'error': str(e)},
                    success=False,
                    response_time_ms=response_time
                )
            logger.error(f"Error in broadcast_confirm: {e}", exc_info=True)
            if update.message:
                reply = await update.message.reply_text(
                    f"❌ Error sending broadcast\n\n<code>{html.escape(str(e)[:300])}</code>",
                    parse_mode=ParseMode.HTML,
                )
                await self.auto_clean_message(update.message, reply)

    async def delbroadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Delete latest broadcast from all groups/users — works from anywhere."""
        start_time = time.time()
        try:
            if not await self.check_access(update):
                await self.send_unauthorized_message(update)
                return

            if not update.effective_user or not update.effective_chat or not update.message:
                return

            # Get latest broadcast from database
            broadcast_data = await asyncio.to_thread(self.db.get_latest_broadcast)
            target_count = len(broadcast_data.get('messages', [])) if broadcast_data else 0

            # Log command execution immediately
            await asyncio.to_thread(
                self.db.log_activity,
                activity_type='command',
                user_id=update.effective_user.id,
                chat_id=update.effective_chat.id,
                username=update.effective_user.username or "",
                chat_title=getattr(update.effective_chat, 'title', None) or "",
                command='/delbroadcast',
                details={'target_count': target_count},
                success=True
            )

            if not broadcast_data:
                reply = await update.message.reply_text(
                    "❌ No recent broadcast found\n\n"
                    "Either no broadcast was sent yet or it was already deleted."
                )
                await self.auto_clean_message(update.message, reply)
                return

            broadcast_messages = broadcast_data.get('messages', [])

            if not broadcast_messages:
                reply = await update.message.reply_text("❌ Broadcast data not found")
                await self.auto_clean_message(update.message, reply)
                return

            if context.user_data is not None:
                context.user_data['pending_delete_broadcast_id'] = broadcast_data.get('id')

            confirm_text = (
                "🗑️ Delete Broadcast Confirmation\n\n"
                f"This will delete the latest broadcast from {len(broadcast_messages)} chats.\n\n"
                f"📋 Broadcast ID: {broadcast_data['broadcast_id']}\n\n"
                "⚠️ Note: Some deletions may fail if:\n"
                "• Bot is not admin in groups\n"
                "• Message is older than 48 hours\n\n"
                "Confirm: /delbroadcast_confirm"
            )

            reply = await update.message.reply_text(confirm_text)
            logger.info(
                f"Broadcast deletion prepared by {update.effective_user.id} "
                f"for {len(broadcast_messages)} chats (ID: {broadcast_data['broadcast_id']})"
            )

            response_time = int((time.time() - start_time) * 1000)
            logger.debug(f"Command /delbroadcast completed in {response_time}ms")

        except Exception as e:
            response_time = int((time.time() - start_time) * 1000)
            if update.effective_user and update.effective_chat:
                await asyncio.to_thread(
                    self.db.log_activity,
                    activity_type='error',
                    user_id=update.effective_user.id,
                    chat_id=update.effective_chat.id,
                    command='/delbroadcast',
                    details={'error': str(e)},
                    success=False,
                    response_time_ms=response_time
                )
            logger.error(f"Error in delbroadcast: {e}", exc_info=True)
            if update.message:
                reply = await update.message.reply_text("❌ Error preparing broadcast deletion")
                await self.auto_clean_message(update.message, reply)

    async def delbroadcast_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Confirm and execute broadcast deletion."""
        start_time = time.time()
        try:
            if not await self.check_access(update):
                await self.send_unauthorized_message(update)
                return

            if not update.effective_user or not update.effective_chat or not update.message:
                return

            pending_broadcast_id = None
            if context.user_data is not None:
                pending_broadcast_id = context.user_data.get('pending_delete_broadcast_id')

            if not pending_broadcast_id:
                reply = await update.message.reply_text(
                    "❌ No pending broadcast deletion found.\n\n"
                    "Please use /delbroadcast first to select a broadcast for deletion."
                )
                await self.auto_clean_message(update.message, reply)
                return

            await asyncio.to_thread(
                self.db.log_activity,
                activity_type='command',
                user_id=update.effective_user.id,
                chat_id=update.effective_chat.id,
                username=update.effective_user.username or "",
                chat_title=getattr(update.effective_chat, 'title', None) or "",
                command='/delbroadcast_confirm',
                details={'action': 'confirm_deletion', 'broadcast_id': pending_broadcast_id},
                success=True
            )

            broadcast_data = await asyncio.to_thread(self.db.get_broadcast_by_id, pending_broadcast_id)

            if not broadcast_data:
                reply = await update.message.reply_text(
                    "❌ Broadcast not found or already deleted.\n\n"
                    f"The broadcast (ID: {pending_broadcast_id}) may have been deleted already."
                )
                await self.auto_clean_message(update.message, reply)
                if context.user_data is not None:
                    context.user_data.pop('pending_delete_broadcast_id', None)
                return

            broadcast_id = broadcast_data['broadcast_id']
            broadcast_messages = broadcast_data.get('messages', [])

            if not broadcast_messages:
                reply = await update.message.reply_text("❌ Broadcast data not found")
                await self.auto_clean_message(update.message, reply)
                return

            status = await update.message.reply_text("🗑️ Deleting broadcast instantly...")

            success_count = 0
            fail_count = 0

            for chat_id_str, message_id in broadcast_messages.items():
                try:
                    chat_id = int(chat_id_str)
                    await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
                    success_count += 1
                except Exception as e:
                    logger.debug(f"Failed to delete from chat {chat_id_str}: {e}")
                    fail_count += 1

            await status.edit_text(
                f"✅ Broadcast deleted instantly!\n\n"
                f"• Deleted: {success_count}\n"
                f"• Failed: {fail_count}\n\n"
                f"💡 Failed deletions occur when bot lacks permissions or message is too old."
            )

            logger.info(
                f"Broadcast deletion by {update.effective_user.id}: "
                f"{success_count} deleted, {fail_count} failed (ID: {broadcast_id})"
            )

            await asyncio.to_thread(self.db.delete_broadcast, pending_broadcast_id)

            if context.user_data is not None:
                context.user_data.pop('pending_delete_broadcast_id', None)

            response_time = int((time.time() - start_time) * 1000)
            logger.debug(f"Command /delbroadcast_confirm completed in {response_time}ms - deleted: {success_count}, failed: {fail_count}")

        except Exception as e:
            response_time = int((time.time() - start_time) * 1000)
            if update.effective_user and update.effective_chat:
                await asyncio.to_thread(
                    self.db.log_activity,
                    activity_type='error',
                    user_id=update.effective_user.id,
                    chat_id=update.effective_chat.id,
                    command='/delbroadcast_confirm',
                    details={'error': str(e)},
                    success=False,
                    response_time_ms=response_time
                )
            logger.error(f"Error in delbroadcast_confirm: {e}", exc_info=True)
            if context.user_data is not None:
                context.user_data.pop('pending_delete_broadcast_id', None)
            if update.message:
                reply = await update.message.reply_text("❌ Error deleting broadcast")
                await self.auto_clean_message(update.message, reply)
