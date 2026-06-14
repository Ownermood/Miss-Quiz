"""
Developer Commands Module for Telegram Quiz Bot
Handles all developer-only commands with enhanced features.

Architecture: DeveloperCommands inherits two mixins:
  BroadcastCommandsMixin  — /broadcast, /broadcast_confirm, /delbroadcast, /delbroadcast_confirm
  QuizEditorMixin         — /editquiz and all supporting callbacks / text-input handlers

This file contains only: __init__, shared utility methods, /dev, /delquiz, /delquiz_confirm.
"""

import html
import logging
import asyncio
import re
import json
import time
from datetime import datetime
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from src.core import config
from src.core.database import DatabaseManager
from src.bot.ui import UI
from src.bot.dev_broadcast import BroadcastCommandsMixin
from src.bot.dev_quiz_editor import QuizEditorMixin

logger = logging.getLogger(__name__)


class DeveloperCommands(BroadcastCommandsMixin, QuizEditorMixin):
    """Handles all developer commands with access control."""

    def __init__(self, db_manager: DatabaseManager, quiz_manager):
        self.db = db_manager
        self.quiz_manager = quiz_manager
        logger.info("Developer commands module initialized")

    # ─── Shared utilities ────────────────────────────────────────────────────

    async def extract_quiz_id_from_message(self, message, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
        """Extract quiz_id from a bot message (poll or text)."""
        if not message:
            return None

        if message.poll:
            poll_id = message.poll.id

            # Database mapping (persistent, works for new quizzes)
            quiz_id = await asyncio.to_thread(self.db.get_quiz_id_from_poll, poll_id)
            if quiz_id:
                logger.debug(f"Extracted quiz_id {quiz_id} from database mapping for poll {poll_id}")
                return quiz_id

            # In-memory context (works before bot restart)
            poll_data = context.bot_data.get(f"poll_{poll_id}")
            if poll_data and 'question_id' in poll_data:
                logger.debug(f"Extracted quiz_id {poll_data['question_id']} from context.bot_data")
                return poll_data['question_id']

            # Question-text match (works for old quizzes)
            if message.poll.question:
                poll_question = message.poll.question.strip()
                if poll_question.startswith('/addquiz'):
                    poll_question = poll_question[len('/addquiz'):].strip()

                all_questions = await asyncio.to_thread(self.db.get_all_questions)
                for q in all_questions:
                    db_question = q.get('question', '').strip()
                    if db_question.startswith('/addquiz'):
                        db_question = db_question[len('/addquiz'):].strip()
                    if db_question == poll_question:
                        logger.debug(f"Extracted quiz_id {q['id']} from question text match")
                        return q['id']

        if message.text:
            match = re.search(r'\[ID:\s*(\d+)\]|Quiz\s*#(\d+)', message.text)
            if match:
                quiz_id = int(match.group(1) or match.group(2))
                logger.debug(f"Extracted quiz_id {quiz_id} from message text")
                return quiz_id

        if message.caption:
            match = re.search(r'\[ID:\s*(\d+)\]|Quiz\s*#(\d+)', message.caption)
            if match:
                quiz_id = int(match.group(1) or match.group(2))
                logger.debug(f"Extracted quiz_id {quiz_id} from message caption")
                return quiz_id

        return None

    async def check_access(self, update: Update) -> bool:
        """Return True if the user is OWNER, WIFU, or a database developer."""
        user_id = update.effective_user.id if update.effective_user else None
        if not user_id:
            return False

        if user_id in config.AUTHORIZED_USERS:
            return True

        developers = await asyncio.to_thread(self.db.get_all_developers)
        is_developer = any(dev['user_id'] == user_id for dev in developers)

        if not is_developer:
            logger.warning(f"Unauthorized access attempt by user {user_id}")

        return is_developer

    async def send_unauthorized_message(self, update: Update):
        """Send a friendly unauthorized-access message and auto-clean it."""
        if not update.effective_message:
            return
        message = await update.effective_message.reply_text(config.UNAUTHORIZED_MESSAGE)
        await self.auto_clean_message(update.effective_message, message, delay=15, is_dev_response=False)

    async def auto_clean_message(self, command_message, bot_reply, delay: int = 5, is_dev_response: bool = True):
        """Auto-delete command and reply after *delay* seconds.

        Developer command responses are never auto-cleaned (is_dev_response=True skips deletion).
        Unauthorized messages are always cleaned regardless of chat type.
        """
        try:
            if is_dev_response:
                return

            await asyncio.sleep(delay)
            try:
                await command_message.delete()
            except Exception as e:
                logger.debug(f"Could not delete command message: {e}")

            try:
                if bot_reply:
                    await bot_reply.delete()
            except Exception as e:
                logger.debug(f"Could not delete reply message: {e}")
        except Exception as e:
            logger.error(f"Error in auto_clean: {e}")

    def parse_inline_buttons(self, text: str) -> tuple:
        """Parse inline buttons from text.

        Supported formats:
          Single row:    [["Button1","URL1"],["Button2","URL2"]]
          Multiple rows: [[["B1","URL1"],["B2","URL2"]],[["B3","URL3"]]]

        Returns: (cleaned_text, InlineKeyboardMarkup | None)
        """
        try:
            text = text.strip()
            match = re.search(r'\[\[(.*?)\]\]\s*$', text, re.DOTALL)
            if not match:
                return text, None

            button_json = '[[' + match.group(1) + ']]'
            cleaned_text = text[:match.start()].strip()
            button_data = json.loads(button_json)

            if not button_data or not isinstance(button_data, list):
                return text, None

            keyboard = []
            total_buttons = 0

            is_multi_row = (
                button_data
                and isinstance(button_data[0], list)
                and len(button_data[0]) > 0
                and isinstance(button_data[0][0], list)
            )

            if is_multi_row:
                for row_data in button_data:
                    if not isinstance(row_data, list):
                        continue
                    row_buttons = []
                    for button in row_data:
                        if total_buttons >= 100:
                            break
                        if isinstance(button, list) and len(button) >= 2:
                            btn_text = str(button[0]).strip()
                            btn_url = str(button[1]).strip()
                            if btn_text and btn_url and (
                                btn_url.startswith('http://') or
                                btn_url.startswith('https://') or
                                btn_url.startswith('t.me/')
                            ):
                                row_buttons.append(InlineKeyboardButton(btn_text, url=btn_url))
                                total_buttons += 1
                                if len(row_buttons) >= 8:
                                    break
                    if row_buttons:
                        keyboard.append(row_buttons)
                    if total_buttons >= 100:
                        break
            else:
                row_buttons = []
                for button in button_data:
                    if total_buttons >= 100:
                        break
                    if isinstance(button, list) and len(button) >= 2:
                        btn_text = str(button[0]).strip()
                        btn_url = str(button[1]).strip()
                        if btn_text and btn_url and (
                            btn_url.startswith('http://') or
                            btn_url.startswith('https://') or
                            btn_url.startswith('t.me/')
                        ):
                            row_buttons.append(InlineKeyboardButton(btn_text, url=btn_url))
                            total_buttons += 1
                            if len(row_buttons) >= 8:
                                break
                if row_buttons:
                    keyboard.append(row_buttons)

            if keyboard:
                logger.info(
                    f"Parsed {sum(len(r) for r in keyboard)} inline buttons "
                    f"in {len(keyboard)} row(s) from broadcast text"
                )
                return cleaned_text, InlineKeyboardMarkup(keyboard)

            return text, None

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse button JSON: {e}")
            return text, None
        except Exception as e:
            logger.error(f"Error parsing inline buttons: {e}")
            return text, None

    async def replace_placeholders(
        self,
        text: str,
        chat_id: int,
        context: ContextTypes.DEFAULT_TYPE,
        user_data: dict | None = None,
        group_data: dict | None = None,
        bot_name_cache: str | None = None,
    ) -> str:
        """Replace {first_name}, {username}, {chat_title}, {bot_name} in *text*.

        Uses database-provided dicts to avoid Telegram API calls per recipient.
        Falls back to get_chat() only when neither user_data nor group_data is given.
        """
        if not text:
            return text

        try:
            bot_name = bot_name_cache if bot_name_cache else (context.bot.first_name or "Bot")
            text = text.replace('{bot_name}', bot_name)

            if user_data:
                first_name = (
                    user_data.get('first_name') or
                    user_data.get('name') or
                    user_data.get('username') or ""
                ).strip() or "User"
                username = f"@{user_data.get('username')}" if user_data.get('username') else "User"
                chat_title = first_name
            elif group_data:
                first_name = "User"
                username = "User"
                chat_title = group_data.get('title') or group_data.get('chat_title') or "Group"
            else:
                try:
                    chat = await context.bot.get_chat(chat_id)
                    if chat.type == 'private':
                        first_name = (chat.first_name or "").strip() or "User"
                        username = f"@{chat.username}" if chat.username else "User"
                        chat_title = first_name
                    else:
                        first_name = "User"
                        username = "User"
                        chat_title = chat.title or "Group"
                except Exception as api_error:
                    logger.warning(f"Fallback get_chat failed for {chat_id}: {api_error}")
                    first_name = "User"
                    username = "User"
                    chat_title = "Chat"

            text = text.replace('{first_name}', first_name)
            text = text.replace('{username}', username)
            text = text.replace('{chat_title}', chat_title)
            return text

        except Exception as e:
            logger.error(f"Error replacing placeholders for chat {chat_id}: {e}")
            return text

    # ─── /dev command ────────────────────────────────────────────────────────

    async def dev(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Developer management: add/remove/list developers, or show message diagnostics."""
        start_time = time.time()
        try:
            if not await self.check_access(update):
                await self.send_unauthorized_message(update)
                return

            if not update.effective_user or not update.effective_chat or not update.message:
                return

            # Reply-mode: show diagnostics for the replied message
            if update.message.reply_to_message:
                replied_msg = update.message.reply_to_message

                diagnostics = "🔍 **Message Diagnostics**\n"
                diagnostics += "━━━━━━━━━━━━━━━━━━━\n\n"
                diagnostics += f"**📨 Message Info:**\n"
                diagnostics += f"• Message ID: `{replied_msg.message_id}`\n"
                diagnostics += f"• Chat ID: `{replied_msg.chat.id}`\n"
                diagnostics += f"• Timestamp: {replied_msg.date}\n"

                if replied_msg.from_user:
                    _u = replied_msg.from_user
                    _nm = _u.full_name or _u.first_name or f"@{_u.username}" or "N/A"
                    diagnostics += f"\n**👤 User Info:**\n"
                    diagnostics += f"• User ID: `{_u.id}`\n"
                    diagnostics += f"• Name: {UI.mention_md(_u.id, _nm)}\n"
                    diagnostics += f"• Username: @{_u.username or 'N/A'}\n"

                if replied_msg.poll:
                    poll_id = replied_msg.poll.id
                    diagnostics += f"\n**📊 Poll Info:**\n"
                    diagnostics += f"• Poll ID: `{poll_id}`\n"
                    diagnostics += f"• Question: {replied_msg.poll.question[:50]}...\n"

                    poll_data = context.bot_data.get(f"poll_{poll_id}")
                    if poll_data:
                        diagnostics += f"\n**🎯 Quiz Data:**\n"
                        diagnostics += f"• Question ID: `{poll_data.get('question_id', 'N/A')}`\n"
                        _cid = poll_data.get('correct_option_id')
                        _cid_str = str(_cid + 1) if isinstance(_cid, int) else 'N/A'
                        diagnostics += f"• Correct Answer: Option {_cid_str}\n"
                        diagnostics += f"• Answers: {len(poll_data.get('user_answers', {}))}\n"
                    else:
                        diagnostics += f"• Status: ⚠️ Poll data expired/unavailable\n"

                if replied_msg.photo:
                    diagnostics += f"\n**📷 Media:**\n• Type: Photo\n• File ID: `{replied_msg.photo[-1].file_id[:30]}...`\n"
                elif replied_msg.video:
                    diagnostics += f"\n**🎥 Media:**\n• Type: Video\n• File ID: `{replied_msg.video.file_id[:30]}...`\n"
                elif replied_msg.document:
                    diagnostics += f"\n**📄 Media:**\n• Type: Document\n• File ID: `{replied_msg.document.file_id[:30]}...`\n"

                if replied_msg.text:
                    preview = replied_msg.text[:100] + "..." if len(replied_msg.text) > 100 else replied_msg.text
                    diagnostics += f"\n**📝 Text Content:**\n```\n{preview}\n```\n"
                elif replied_msg.caption:
                    preview = replied_msg.caption[:100] + "..." if len(replied_msg.caption) > 100 else replied_msg.caption
                    diagnostics += f"\n**📝 Caption:**\n```\n{preview}\n```\n"

                diagnostics += "\n━━━━━━━━━━━━━━━━━━━\n"
                diagnostics += "💡 Use this info to debug issues or verify data"

                await asyncio.to_thread(
                    self.db.log_activity,
                    activity_type='command',
                    user_id=update.effective_user.id,
                    chat_id=update.effective_chat.id,
                    username=update.effective_user.username or "",
                    command='/dev',
                    details={
                        'action': 'contextual_diagnostics',
                        'replied_msg_id': replied_msg.message_id,
                        'replied_msg_type': 'poll' if replied_msg.poll else 'message'
                    },
                    success=True
                )

                reply = await update.message.reply_text(diagnostics, parse_mode=ParseMode.MARKDOWN)
                logger.info(f"Showed contextual diagnostics for message {replied_msg.message_id}")
                return

            action = (
                'help' if not context.args
                else (context.args[0] if not context.args[0].isdigit() else 'quick_add')
            )
            target_user = (
                context.args[1] if context.args and len(context.args) > 1
                else (context.args[0] if context.args and context.args[0].isdigit() else None)
            )

            await asyncio.to_thread(
                self.db.log_activity,
                activity_type='command',
                user_id=update.effective_user.id,
                chat_id=update.effective_chat.id,
                username=update.effective_user.username or "",
                chat_title=getattr(update.effective_chat, 'title', None) or "",
                command='/dev',
                details={'action': action, 'target_user': target_user},
                success=True
            )

            if not context.args:
                reply = await update.message.reply_text(
                    "🔧 **Developer Management**\n\n"
                    "**Commands:**\n"
                    "• /dev [user_id] - Add developer (quick add)\n"
                    "• /dev add [user_id] - Add developer\n"
                    "• /dev remove [user_id] - Remove developer\n"
                    "• /dev list - Show all developers\n\n"
                    "**💡 Reply Mode:**\n"
                    "• Reply to any message with /dev to see diagnostics",
                    parse_mode=ParseMode.MARKDOWN
                )
                await self.auto_clean_message(update.message, reply)
                return

            # Quick add: /dev <user_id>
            try:
                user_id = int(context.args[0])
                try:
                    user_info = await context.bot.get_chat(user_id)
                    username = getattr(user_info, 'username', "") or ""
                    first_name = getattr(user_info, 'first_name', "") or ""
                    last_name = getattr(user_info, 'last_name', "") or ""
                    await asyncio.to_thread(
                        self.db.add_developer,
                        user_id=user_id, username=username,
                        first_name=first_name, last_name=last_name,
                        added_by=update.effective_user.id
                    )
                    display_name = first_name or username or f"User {user_id}"
                    dev_mention = UI.mention(user_id, display_name)
                    reply = await update.message.reply_text(
                        f"✅ Developer added successfully!\n\n👤 {dev_mention}\n🆔 ID: {user_id}",
                        parse_mode=ParseMode.HTML,
                        link_preview_options=LinkPreviewOptions(is_disabled=True)
                    )
                except Exception as e:
                    logger.warning(f"Could not fetch user info for {user_id}: {e}")
                    await asyncio.to_thread(self.db.add_developer, user_id, added_by=update.effective_user.id)
                    dev_mention = UI.mention(user_id, f"User {user_id}")
                    reply = await update.message.reply_text(
                        f"✅ Developer added successfully!\n\n👤 {dev_mention}\n⚠️ Could not fetch user details",
                        parse_mode=ParseMode.HTML,
                        link_preview_options=LinkPreviewOptions(is_disabled=True)
                    )
                logger.info(f"Developer {user_id} added by {update.effective_user.id}")
                await self.auto_clean_message(update.message, reply)
                return
            except ValueError:
                pass  # Not a user ID — treat as action word

            action = context.args[0].lower()

            if action == "add":
                if len(context.args) < 2:
                    reply = await update.message.reply_text("❌ Usage: /dev add [user_id]")
                    await self.auto_clean_message(update.message, reply)
                    return
                try:
                    new_dev_id = int(context.args[1])
                    try:
                        user_info = await context.bot.get_chat(new_dev_id)
                        username = getattr(user_info, 'username', "") or ""
                        first_name = getattr(user_info, 'first_name', "") or ""
                        last_name = getattr(user_info, 'last_name', "") or ""
                        await asyncio.to_thread(
                            self.db.add_developer,
                            user_id=new_dev_id, username=username,
                            first_name=first_name, last_name=last_name,
                            added_by=update.effective_user.id
                        )
                        display_name = first_name or username or f"User {new_dev_id}"
                        dev_mention = f'<b>{html.escape(str(display_name))}</b>'
                        reply = await update.message.reply_text(
                            f"✅ Developer added successfully!\n\n👤 {dev_mention}\n🆔 ID: {new_dev_id}",
                            parse_mode=ParseMode.HTML,
                            link_preview_options=LinkPreviewOptions(is_disabled=True)
                        )
                    except Exception as e:
                        logger.warning(f"Could not fetch user info for {new_dev_id}: {e}")
                        await asyncio.to_thread(self.db.add_developer, new_dev_id, added_by=update.effective_user.id)
                        reply = await update.message.reply_text(
                            f"✅ Developer added successfully!\n\n👤 <b>User {new_dev_id}</b>\n⚠️ Could not fetch user details",
                            parse_mode=ParseMode.HTML,
                            link_preview_options=LinkPreviewOptions(is_disabled=True)
                        )
                    logger.info(f"Developer {new_dev_id} added by {update.effective_user.id}")
                    await self.auto_clean_message(update.message, reply)
                except ValueError:
                    reply = await update.message.reply_text("❌ Invalid user ID")
                    await self.auto_clean_message(update.message, reply)

            elif action == "remove":
                if len(context.args) < 2:
                    reply = await update.message.reply_text("❌ Usage: /dev remove [user_id]")
                    await self.auto_clean_message(update.message, reply)
                    return
                try:
                    dev_id = int(context.args[1])
                    if dev_id in config.AUTHORIZED_USERS:
                        reply = await update.message.reply_text("❌ Cannot remove OWNER or WIFU")
                        await self.auto_clean_message(update.message, reply)
                        return
                    if await asyncio.to_thread(self.db.remove_developer, dev_id):
                        reply = await update.message.reply_text(f"✅ Developer {dev_id} removed")
                        logger.info(f"Developer {dev_id} removed by {update.effective_user.id}")
                        await self.auto_clean_message(update.message, reply)
                    else:
                        reply = await update.message.reply_text(f"❌ Developer {dev_id} not found")
                        await self.auto_clean_message(update.message, reply)
                except ValueError:
                    reply = await update.message.reply_text("❌ Invalid user ID")
                    await self.auto_clean_message(update.message, reply)

            elif action == "list":
                developers = await asyncio.to_thread(self.db.get_all_developers)

                dev_text = """╔══════════════════╗
║ 👥 𝐃𝐞𝐯𝐞𝐥𝐨𝐩𝐞𝐫 & 𝐀𝐝𝐦𝐢𝐧 𝐏𝐚𝐧𝐞𝐥
╚══════════════════╝

👑 𝗗𝗘𝗩𝗘𝗟𝗢𝗣𝗘𝗥𝗦
━━━━━━━━━━━━━━━━━━\n"""

                try:
                    owner_user = await context.bot.get_chat(config.OWNER_ID)
                    owner_name = owner_user.first_name or "Owner"
                except Exception as e:
                    logger.debug(f"Could not fetch owner info: {e}")
                    owner_name = "Owner"

                dev_text += f"• <b>{html.escape(str(owner_name))}</b> (ID: {config.OWNER_ID})\n"

                if config.WIFU_ID:
                    try:
                        wifu_user = await context.bot.get_chat(config.WIFU_ID)
                        wifu_name = wifu_user.first_name or "Developer"
                        dev_text += f"• <b>{html.escape(str(wifu_name))}</b> (ID: {config.WIFU_ID})\n"
                    except Exception as e:
                        logger.debug(f"Could not fetch WIFU info: {e}")
                        dev_text += f"• <b>Developer</b> (ID: {config.WIFU_ID})\n"

                for dev in developers:
                    dev_uid = dev['user_id']
                    try:
                        dev_user = await context.bot.get_chat(dev_uid)
                        dev_name = dev_user.first_name or f"User{dev_uid}"
                        dev_text += f"• <b>{html.escape(str(dev_name))}</b> (ID: {dev_uid})\n"
                    except Exception as e:
                        logger.debug(f"Could not fetch developer info: {e}")
                        d_name = dev.get('username') or dev.get('first_name') or f"User{dev_uid}"
                        dev_text += f"• <b>{html.escape(str(d_name))}</b> (ID: {dev_uid})\n"

                reply = await update.message.reply_text(
                    dev_text, parse_mode=ParseMode.HTML,
                    link_preview_options=LinkPreviewOptions(is_disabled=True)
                )
                await self.auto_clean_message(update.message, reply)

            else:
                reply = await update.message.reply_text("❌ Unknown action. Use: add, remove, or list")
                await self.auto_clean_message(update.message, reply)

            response_time = int((time.time() - start_time) * 1000)
            logger.debug(f"Command /dev completed in {response_time}ms")

        except Exception as e:
            response_time = int((time.time() - start_time) * 1000)
            if update.effective_user and update.effective_chat:
                await asyncio.to_thread(
                    self.db.log_activity,
                    activity_type='error',
                    user_id=update.effective_user.id,
                    chat_id=update.effective_chat.id,
                    command='/dev',
                    details={'error': str(e)},
                    success=False,
                    response_time_ms=response_time
                )
            logger.error(f"Error in dev command: {e}", exc_info=True)
            if update.message:
                reply = await update.message.reply_text("❌ Error executing command")
                await self.auto_clean_message(update.message, reply)

    # ─── /delquiz commands ────────────────────────────────────────────────────

    async def delquiz(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Delete a quiz question by ID or by replying to its poll."""
        start_time = time.time()
        try:
            if not await self.check_access(update):
                await self.send_unauthorized_message(update)
                return

            if not update.effective_user or not update.effective_chat or not update.message:
                return

            quiz_id_arg = context.args[0] if context.args else None
            await asyncio.to_thread(
                self.db.log_activity,
                activity_type='command',
                user_id=update.effective_user.id,
                chat_id=update.effective_chat.id,
                username=update.effective_user.username or "",
                chat_title=getattr(update.effective_chat, 'title', None) or "",
                command='/delquiz',
                details={'quiz_id': quiz_id_arg, 'reply_mode': bool(update.message.reply_to_message)},
                success=True
            )

            questions = await asyncio.to_thread(self.db.get_all_questions)
            if not questions:
                reply = await update.message.reply_text(
                    "❌ No Quizzes Available\n\nAdd new quizzes using /addquiz command"
                )
                await self.auto_clean_message(update.message, reply)
                return

            if update.message.reply_to_message:
                quiz_id = await self.extract_quiz_id_from_message(update.message.reply_to_message, context)
                if quiz_id:
                    quiz = next((q for q in questions if q.get('id') == quiz_id), None)
                    if not quiz:
                        reply = await update.message.reply_text(
                            f"❌ Quiz #{quiz_id} not found in database.\n\n💡 Use /editquiz to view all quizzes"
                        )
                        await self.auto_clean_message(update.message, reply)
                        return

                    if context.user_data is not None:
                        context.user_data['pending_delete_quiz'] = quiz['id']

                    confirm_text = f"🗑 Confirm Quiz Deletion\n\n"
                    confirm_text += f"📌 Quiz #{quiz['id']}\n"
                    confirm_text += f"❓ {quiz['question']}\n\n"
                    for i, opt in enumerate(quiz['options'], 1):
                        marker = "✅" if i - 1 == quiz['correct_answer'] else "⭕"
                        confirm_text += f"{i}️⃣ {opt} {marker}\n"
                    confirm_text += f"\n⚠ Confirm: /delquiz_confirm\n"
                    confirm_text += "❌ Cancel: Ignore this message\n\n"
                    confirm_text += "💡 Once confirmed, the quiz will be permanently deleted."

                    reply = await update.message.reply_text(confirm_text)
                    logger.info(f"Quiz deletion confirmation shown for quiz #{quiz['id']} (via reply)")
                    return
                else:
                    reply = await update.message.reply_text(
                        "❌ Could not find quiz ID in the replied message.\n\n"
                        "💡 Make sure you're replying to:\n"
                        "• A quiz poll sent by the bot\n"
                        "• A message containing quiz information\n\n"
                        "Or use: /delquiz [quiz_id]"
                    )
                    await self.auto_clean_message(update.message, reply)
                    return

            if not context.args:
                reply = await update.message.reply_text(
                    "❌ Invalid Usage\n\n"
                    "Either:\n"
                    "1. Reply to a quiz with /delquiz\n"
                    "2. Use: /delquiz [quiz_number]\n\n"
                    "Use /editquiz to view available quizzes"
                )
                await self.auto_clean_message(update.message, reply)
                return

            try:
                quiz_id = int(context.args[0])
                quiz = next((q for q in questions if q['id'] == quiz_id), None)
                if not quiz:
                    reply = await update.message.reply_text(
                        f"❌ Invalid Quiz ID: {quiz_id}\n\nUse /editquiz to view available quizzes"
                    )
                    await self.auto_clean_message(update.message, reply)
                    return

                if context.user_data is not None:
                    context.user_data['pending_delete_quiz'] = quiz['id']

                confirm_text = f"🗑 Confirm Quiz Deletion\n\n"
                confirm_text += f"📌 Quiz #{quiz['id']}\n"
                confirm_text += f"❓ {quiz['question']}\n\n"
                for i, opt in enumerate(quiz['options'], 1):
                    marker = "✅" if i - 1 == quiz['correct_answer'] else "⭕"
                    confirm_text += f"{i}️⃣ {opt} {marker}\n"
                confirm_text += f"\n⚠ Confirm: /delquiz_confirm\n"
                confirm_text += "❌ Cancel: Ignore this message\n\n"
                confirm_text += "💡 Once confirmed, the quiz will be permanently deleted."

                reply = await update.message.reply_text(confirm_text)
                logger.info(f"Quiz deletion confirmation shown for quiz #{quiz['id']}")

            except ValueError:
                reply = await update.message.reply_text(
                    "❌ Invalid Input\n\nPlease provide a valid quiz ID number\nUsage: /delquiz [quiz_id]"
                )
                await self.auto_clean_message(update.message, reply)

            response_time = int((time.time() - start_time) * 1000)

        except Exception as e:
            response_time = int((time.time() - start_time) * 1000)
            if update.effective_user and update.effective_chat:
                await asyncio.to_thread(
                    self.db.log_activity,
                    activity_type='error',
                    user_id=update.effective_user.id,
                    chat_id=update.effective_chat.id,
                    command='/delquiz',
                    details={'error': str(e)},
                    success=False,
                    response_time_ms=response_time
                )
            logger.error(f"Error in delquiz: {e}", exc_info=True)
            if update.message:
                reply = await update.message.reply_text("❌ Error processing delete request")
                await self.auto_clean_message(update.message, reply)

    async def delquiz_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Confirm and execute quiz deletion."""
        start_time = time.time()
        try:
            if not await self.check_access(update):
                await self.send_unauthorized_message(update)
                return

            if not update.effective_user or not update.effective_chat or not update.message:
                return

            quiz_id = context.user_data.get('pending_delete_quiz') if context.user_data else None

            await asyncio.to_thread(
                self.db.log_activity,
                activity_type='command',
                user_id=update.effective_user.id,
                chat_id=update.effective_chat.id,
                username=update.effective_user.username or "",
                chat_title=getattr(update.effective_chat, 'title', None) or "",
                command='/delquiz_confirm',
                details={'quiz_id': quiz_id, 'action': 'confirm_deletion'},
                success=True
            )

            if not quiz_id:
                reply = await update.message.reply_text(
                    "❌ No quiz pending deletion\n\nPlease use /delquiz first to select a quiz"
                )
                await self.auto_clean_message(update.message, reply)
                return

            questions = await asyncio.to_thread(self.db.get_all_questions)
            quiz_to_delete = next((q for q in questions if q['id'] == quiz_id), None)

            if self.quiz_manager.delete_question_by_db_id(quiz_id):
                if context.user_data is not None:
                    context.user_data.pop('pending_delete_quiz', None)

                quiz_stats = self.quiz_manager.get_quiz_stats()

                await asyncio.to_thread(
                    self.db.log_activity,
                    activity_type='quiz_deleted',
                    user_id=update.effective_user.id,
                    chat_id=update.effective_chat.id,
                    username=update.effective_user.username or "",
                    chat_title=getattr(update.effective_chat, 'title', None) or "",
                    details={
                        'deleted_quiz_id': quiz_id,
                        'question_text': quiz_to_delete['question'][:100] if quiz_to_delete else None,
                        'remaining_quizzes': quiz_stats['total_quizzes'],
                        'integrity_status': quiz_stats['integrity_status']
                    },
                    success=True
                )

                integrity_icon = "✅" if quiz_stats['integrity_status'] == 'synced' else "⚠️"
                reply = await update.message.reply_text(
                    f"✅ Quiz #{quiz_id} deleted successfully! 🗑️\n\n"
                    f"📊 Remaining quizzes: {quiz_stats['total_quizzes']}\n"
                    f"{integrity_icon} Integrity: {quiz_stats['integrity_status']}"
                )
                logger.info(f"Quiz #{quiz_id} deleted by user {update.effective_user.id}")
                await self.auto_clean_message(update.message, reply, delay=3)
            else:
                reply = await update.message.reply_text(f"❌ Quiz #{quiz_id} not found")
                await self.auto_clean_message(update.message, reply)

            response_time = int((time.time() - start_time) * 1000)

        except Exception as e:
            response_time = int((time.time() - start_time) * 1000)
            if update.effective_user and update.effective_chat:
                await asyncio.to_thread(
                    self.db.log_activity,
                    activity_type='error',
                    user_id=update.effective_user.id,
                    chat_id=update.effective_chat.id,
                    command='/delquiz_confirm',
                    details={'error': str(e)},
                    success=False,
                    response_time_ms=response_time
                )
            logger.error(f"Error in delquiz_confirm: {e}", exc_info=True)
            if update.message:
                reply = await update.message.reply_text("❌ Error deleting quiz")
                await self.auto_clean_message(update.message, reply)
