"""
Developer Commands Module for Telegram Quiz Bot
Handles all developer-only commands with enhanced features
"""

import html
import logging
import asyncio
import sys
import os
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

logger = logging.getLogger(__name__)


class DeveloperCommands:
    """Handles all developer commands with access control"""
    
    def __init__(self, db_manager: DatabaseManager, quiz_manager):
        self.db = db_manager
        self.quiz_manager = quiz_manager
        logger.info("Developer commands module initialized")
    
    def extract_quiz_id_from_message(self, message, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
        """Extract quiz_id from a bot message (poll or text).
        
        Args:
            message: Telegram message object to extract quiz ID from
            context: Telegram context for accessing bot_data
            
        Returns:
            int: Quiz ID if found, None otherwise
        """
        if not message:
            return None
        
        # Check if message is a poll (quiz)
        if message.poll:
            poll_id = message.poll.id
            
            # First: Check database mapping (works for NEW quizzes - PERSISTENT)
            quiz_id = self.db.get_quiz_id_from_poll(poll_id)
            if quiz_id:
                logger.debug(f"Extracted quiz_id {quiz_id} from database mapping for poll {poll_id}")
                return quiz_id
            
            # Second: Look up in context.bot_data (works before bot restart)
            poll_data = context.bot_data.get(f"poll_{poll_id}")
            if poll_data and 'question_id' in poll_data:
                logger.debug(f"Extracted quiz_id {poll_data['question_id']} from context.bot_data")
                return poll_data['question_id']
            
            # Third: Match poll question text to database (works for OLD quizzes!)
            if message.poll.question:
                poll_question = message.poll.question.strip()
                # Remove /addquiz prefix if present
                if poll_question.startswith('/addquiz'):
                    poll_question = poll_question[len('/addquiz'):].strip()
                
                # Search for matching question in database
                all_questions = self.db.get_all_questions()
                for q in all_questions:
                    db_question = q.get('question', '').strip()
                    if db_question.startswith('/addquiz'):
                        db_question = db_question[len('/addquiz'):].strip()
                    
                    if db_question == poll_question:
                        logger.debug(f"Extracted quiz_id {q['id']} from question text match")
                        return q['id']
        
        # Check message text for quiz ID pattern
        # Look for patterns like: [ID: 123] or Quiz #123
        if message.text:
            match = re.search(r'\[ID:\s*(\d+)\]|Quiz\s*#(\d+)', message.text)
            if match:
                quiz_id = int(match.group(1) or match.group(2))
                logger.debug(f"Extracted quiz_id {quiz_id} from message text")
                return quiz_id
        
        # Check caption for quiz ID pattern (for media messages)
        if message.caption:
            match = re.search(r'\[ID:\s*(\d+)\]|Quiz\s*#(\d+)', message.caption)
            if match:
                quiz_id = int(match.group(1) or match.group(2))
                logger.debug(f"Extracted quiz_id {quiz_id} from message caption")
                return quiz_id
        
        return None
    
    async def check_access(self, update: Update) -> bool:
        """Check if user is authorized (OWNER, WIFU, or any developer in database)"""
        user_id = update.effective_user.id if update.effective_user else None
        if not user_id:
            return False
        
        # Check if user is OWNER or WIFU
        if user_id in config.AUTHORIZED_USERS:
            return True
        
        # Check if user is in developers database
        developers = self.db.get_all_developers()
        is_developer = any(dev['user_id'] == user_id for dev in developers)
        
        if not is_developer:
            logger.warning(f"Unauthorized access attempt by user {user_id}")
        
        return is_developer
    
    async def send_unauthorized_message(self, update: Update):
        """Send friendly unauthorized message"""
        if not update.effective_message:
            return
        message = await update.effective_message.reply_text(config.UNAUTHORIZED_MESSAGE)
        
        # Clean unauthorized messages (not developer responses) - 15 second delay
        await self.auto_clean_message(update.effective_message, message, delay=15, is_dev_response=False)
    
    async def auto_clean_message(self, command_message, bot_reply, delay: int = 5, is_dev_response: bool = True):
        """Auto-clean command and reply messages after delay
        
        Args:
            command_message: The command message to clean
            bot_reply: The bot's reply message to clean
            delay: Delay in seconds before cleaning
            is_dev_response: If True, skip auto-clean (developer responses should stay visible)
        """
        try:
            # NEVER auto-clean developer command responses
            if is_dev_response:
                logger.debug(f"Skipping auto-clean for developer command response")
                return
            
            # For unauthorized messages, auto-delete in ALL chat types (groups and PMs)
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
    
    def format_number(self, num):
        """Format numbers with K/M suffixes for readability"""
        if num >= 1_000_000:
            return f"{num / 1_000_000:.2f}M"
        elif num >= 1_000:
            return f"{num / 1_000:.2f}K"
        else:
            return f"{num:,}"
    
    def format_relative_time(self, timestamp_str):
        """Convert ISO timestamp to relative time (e.g., '5m ago', '2h ago')"""
        try:
            dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
            now = datetime.now()
            
            if dt.tzinfo is not None:
                from datetime import timezone
                now = datetime.now(timezone.utc)
            
            diff = now - dt
            seconds = int(diff.total_seconds())
            
            if seconds < 60:
                return f"{seconds}s ago"
            elif seconds < 3600:
                return f"{seconds // 60}m ago"
            elif seconds < 86400:
                return f"{seconds // 3600}h ago"
            else:
                return f"{diff.days}d ago"
        except Exception as e:
            logger.error(f"Error formatting relative time: {e}")
            return "recently"
    
    def parse_inline_buttons(self, text: str) -> tuple:
        """
        Parse inline buttons from text format with robust support for multiple formats:
        - Single row: [["Button1","URL1"],["Button2","URL2"]] → 2 buttons in 1 row
        - Multiple rows: [[["B1","URL1"],["B2","URL2"]],[["B3","URL3"]]] → 2 rows
        Returns: (cleaned_text, InlineKeyboardMarkup or None)
        """
        try:
            # Trim whitespace and newlines from text
            text = text.strip()
            
            # More forgiving regex: match [[...]] at end, allow trailing whitespace/newlines
            button_pattern = r'\[\[(.*?)\]\]\s*$'
            match = re.search(button_pattern, text, re.DOTALL)
            
            if not match:
                return text, None
            
            # Extract button JSON and clean text
            button_json = '[[' + match.group(1) + ']]'
            cleaned_text = text[:match.start()].strip()
            
            # Parse button data
            button_data = json.loads(button_json)
            
            if not button_data or not isinstance(button_data, list):
                return text, None
            
            # Determine format: nested array (multiple rows) or flat array (single row)
            keyboard = []
            total_buttons = 0
            
            # Check if it's a nested array (multiple rows format)
            if button_data and isinstance(button_data[0], list) and len(button_data[0]) > 0 and isinstance(button_data[0][0], list):
                # Multiple rows format: [[["B1","URL1"],["B2","URL2"]],[["B3","URL3"]]]
                for row_data in button_data:
                    if not isinstance(row_data, list):
                        continue
                    
                    row_buttons = []
                    for button in row_data:
                        if total_buttons >= 100:  # Telegram limit: 100 buttons total
                            break
                        
                        if isinstance(button, list) and len(button) >= 2:
                            button_text = str(button[0]).strip()
                            button_url = str(button[1]).strip()
                            
                            # Validate URL scheme
                            if button_text and button_url and (
                                button_url.startswith('http://') or 
                                button_url.startswith('https://') or 
                                button_url.startswith('t.me/')
                            ):
                                row_buttons.append(InlineKeyboardButton(button_text, url=button_url))
                                total_buttons += 1
                                
                                if len(row_buttons) >= 8:  # Telegram limit: 8 buttons per row
                                    break
                    
                    if row_buttons:
                        keyboard.append(row_buttons)
                    
                    if total_buttons >= 100:
                        break
            
            else:
                # Single row format: [["Button1","URL1"],["Button2","URL2"]]
                row_buttons = []
                for button in button_data:
                    if total_buttons >= 100:
                        break
                    
                    if isinstance(button, list) and len(button) >= 2:
                        button_text = str(button[0]).strip()
                        button_url = str(button[1]).strip()
                        
                        # Validate URL scheme
                        if button_text and button_url and (
                            button_url.startswith('http://') or 
                            button_url.startswith('https://') or 
                            button_url.startswith('t.me/')
                        ):
                            row_buttons.append(InlineKeyboardButton(button_text, url=button_url))
                            total_buttons += 1
                            
                            if len(row_buttons) >= 8:
                                break
                
                if row_buttons:
                    keyboard.append(row_buttons)
            
            if keyboard:
                logger.info(f"Parsed {sum(len(row) for row in keyboard)} inline buttons in {len(keyboard)} row(s) from broadcast text")
                return cleaned_text, InlineKeyboardMarkup(keyboard)
            
            return text, None
        
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse button JSON: {e}")
            return text, None
        except Exception as e:
            logger.error(f"Error parsing inline buttons: {e}")
            return text, None
    
    async def replace_placeholders(self, text: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE, 
                                   user_data: dict | None = None, group_data: dict | None = None, 
                                   bot_name_cache: str | None = None) -> str:
        """
        Replace placeholders in text (OPTIMIZED - uses database data to avoid API calls):
        {first_name} -> recipient's first name
        {username} -> recipient's username (with @)
        {chat_title} -> group title or first name for PMs
        {bot_name} -> bot's name
        
        Args:
            text: Text with placeholders
            chat_id: Chat ID for fallback lookup
            context: Telegram context
            user_data: User dict from database (if PM) - has first_name, username
            group_data: Group dict from database (if group) - has chat_title
            bot_name_cache: Cached bot name to avoid repeated lookups
        """
        if not text:
            return text
        
        try:
            # Replace bot name (use cached value to avoid API call)
            bot_name = bot_name_cache if bot_name_cache else (context.bot.first_name or "Bot")
            text = text.replace('{bot_name}', bot_name)
            
            # Use provided data from database instead of making API call
            if user_data:
                # PM - use user's info from database
                first_name = (user_data.get('first_name') or user_data.get('name') or user_data.get('username') or "").strip() or "User"
                username = f"@{user_data.get('username')}" if user_data.get('username') else "User"
                chat_title = first_name
            elif group_data:
                # Group - use group info from database
                first_name = "User"
                username = "User"
                chat_title = group_data.get('title') or group_data.get('chat_title') or "Group"
            else:
                # Fallback: fetch from API only if data not provided
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
    
    async def delquiz(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Delete quiz questions - Fixed version without Markdown parsing errors"""
        start_time = time.time()
        try:
            if not await self.check_access(update):
                await self.send_unauthorized_message(update)
                return
            
            if not update.effective_user or not update.effective_chat or not update.message:
                return
            
            # Log command execution immediately
            quiz_id_arg = context.args[0] if context.args else None
            self.db.log_activity(
                activity_type='command',
                user_id=update.effective_user.id,
                chat_id=update.effective_chat.id,
                username=update.effective_user.username or "",
                chat_title=getattr(update.effective_chat, 'title', None) or "",
                command='/delquiz',
                details={'quiz_id': quiz_id_arg, 'reply_mode': bool(update.message.reply_to_message if update.message else None)},
                success=True
            )
            
            questions = self.db.get_all_questions()
            if not questions:
                reply = await update.message.reply_text(
                    "❌ No Quizzes Available\n\n"
                    "Add new quizzes using /addquiz command"
                )
                await self.auto_clean_message(update.message, reply)
                return
            
            # Handle reply to quiz case
            if update.message.reply_to_message:
                quiz_id = self.extract_quiz_id_from_message(update.message.reply_to_message, context)
                
                if quiz_id:
                    # Find the quiz by ID
                    quiz = next((q for q in questions if q.get('id') == quiz_id), None)
                    
                    if not quiz:
                        reply = await update.message.reply_text(
                            f"❌ Quiz #{quiz_id} not found in database.\n\n"
                            "💡 Use /editquiz to view all quizzes"
                        )
                        await self.auto_clean_message(update.message, reply)
                        return
                    
                    # Store quiz ID in user context
                    if context.user_data is not None:
                        context.user_data['pending_delete_quiz'] = quiz['id']
                    
                    confirm_text = f"🗑 Confirm Quiz Deletion\n\n"
                    confirm_text += f"📌 Quiz #{quiz['id']}\n"
                    confirm_text += f"❓ {quiz['question']}\n\n"
                    for i, opt in enumerate(quiz['options'], 1):
                        marker = "✅" if i-1 == quiz['correct_answer'] else "⭕"
                        confirm_text += f"{i}️⃣ {opt} {marker}\n"
                    confirm_text += f"\n⚠ Confirm: /delquiz_confirm\n"
                    confirm_text += "❌ Cancel: Ignore this message\n\n"
                    confirm_text += "💡 Once confirmed, the quiz will be permanently deleted."
                    
                    reply = await update.message.reply_text(confirm_text)
                    logger.info(f"Quiz deletion confirmation shown for quiz #{quiz['id']} (via reply)")
                    return
                else:
                    # Could not extract quiz ID from replied message
                    reply = await update.message.reply_text(
                        "❌ Could not find quiz ID in the replied message.\n\n"
                        "💡 Make sure you're replying to:\n"
                        "• A quiz poll sent by the bot\n"
                        "• A message containing quiz information\n\n"
                        "Or use: /delquiz [quiz_id]"
                    )
                    await self.auto_clean_message(update.message, reply)
                    return
            
            # Handle direct command
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
                        f"❌ Invalid Quiz ID: {quiz_id}\n\n"
                        "Use /editquiz to view available quizzes"
                    )
                    await self.auto_clean_message(update.message, reply)
                    return
                
                # Show confirmation and store quiz ID
                if context.user_data is not None:
                    context.user_data['pending_delete_quiz'] = quiz['id']
                
                confirm_text = f"🗑 Confirm Quiz Deletion\n\n"
                confirm_text += f"📌 Quiz #{quiz['id']}\n"
                confirm_text += f"❓ {quiz['question']}\n\n"
                for i, opt in enumerate(quiz['options'], 1):
                    marker = "✅" if i-1 == quiz['correct_answer'] else "⭕"
                    confirm_text += f"{i}️⃣ {opt} {marker}\n"
                confirm_text += f"\n⚠ Confirm: /delquiz_confirm\n"
                confirm_text += "❌ Cancel: Ignore this message\n\n"
                confirm_text += "💡 Once confirmed, the quiz will be permanently deleted."
                
                reply = await update.message.reply_text(confirm_text)
                logger.info(f"Quiz deletion confirmation shown for quiz #{quiz['id']}")
                
            except ValueError:
                reply = await update.message.reply_text(
                    "❌ Invalid Input\n\n"
                    "Please provide a valid quiz ID number\n"
                    "Usage: /delquiz [quiz_id]"
                )
                await self.auto_clean_message(update.message, reply)
            
            # Calculate response time at end
            response_time = int((time.time() - start_time) * 1000)
        
        except Exception as e:
            response_time = int((time.time() - start_time) * 1000)
            if update.effective_user and update.effective_chat:
                self.db.log_activity(
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
        """Confirm and execute quiz deletion"""
        start_time = time.time()
        try:
            if not await self.check_access(update):
                await self.send_unauthorized_message(update)
                return
            
            if not update.effective_user or not update.effective_chat or not update.message:
                return
            
            # Get quiz ID from context
            quiz_id = context.user_data.get('pending_delete_quiz') if context.user_data else None
            
            # Log command execution immediately
            self.db.log_activity(
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
                    "❌ No quiz pending deletion\n\n"
                    "Please use /delquiz first to select a quiz"
                )
                await self.auto_clean_message(update.message, reply)
                return
            
            # Get quiz details before deletion for logging
            questions = self.db.get_all_questions()
            quiz_to_delete = next((q for q in questions if q['id'] == quiz_id), None)
            
            # Delete using reliable database ID method
            if self.quiz_manager.delete_question_by_db_id(quiz_id):
                # Clear the pending delete
                if context.user_data is not None:
                    context.user_data.pop('pending_delete_quiz', None)
                
                # Get updated counts with integrity check
                quiz_stats = self.quiz_manager.get_quiz_stats()
                
                # Log comprehensive quiz deletion activity
                self.db.log_activity(
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
                
                # Get updated count from database with integrity verification
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
            
            # Calculate response time at end
            response_time = int((time.time() - start_time) * 1000)
        
        except Exception as e:
            response_time = int((time.time() - start_time) * 1000)
            if update.effective_user and update.effective_chat:
                self.db.log_activity(
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
    
    async def dev(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Enhanced developer management command with contextual diagnostics"""
        start_time = time.time()
        try:
            if not await self.check_access(update):
                await self.send_unauthorized_message(update)
                return
            
            if not update.effective_user or not update.effective_chat or not update.message:
                return
            
            # Check if replying to a message for contextual diagnostics
            if update.message.reply_to_message:
                replied_msg = update.message.reply_to_message
                
                # Build contextual diagnostics
                diagnostics = "🔍 **Message Diagnostics**\n"
                diagnostics += "━━━━━━━━━━━━━━━━━━━\n\n"
                
                diagnostics += f"**📨 Message Info:**\n"
                diagnostics += f"• Message ID: `{replied_msg.message_id}`\n"
                diagnostics += f"• Chat ID: `{replied_msg.chat.id}`\n"
                diagnostics += f"• Timestamp: {replied_msg.date}\n"
                
                if replied_msg.from_user:
                    diagnostics += f"\n**👤 User Info:**\n"
                    diagnostics += f"• User ID: `{replied_msg.from_user.id}`\n"
                    diagnostics += f"• Username: @{replied_msg.from_user.username or 'N/A'}\n"
                    diagnostics += f"• Name: {replied_msg.from_user.first_name or 'N/A'}\n"
                
                # Check if it's a quiz
                if replied_msg.poll:
                    poll_id = replied_msg.poll.id
                    diagnostics += f"\n**📊 Poll Info:**\n"
                    diagnostics += f"• Poll ID: `{poll_id}`\n"
                    diagnostics += f"• Question: {replied_msg.poll.question[:50]}...\n"
                    
                    # Try to get quiz data from context
                    poll_data = context.bot_data.get(f"poll_{poll_id}")
                    if poll_data:
                        diagnostics += f"\n**🎯 Quiz Data:**\n"
                        diagnostics += f"• Question ID: `{poll_data.get('question_id', 'N/A')}`\n"
                        diagnostics += f"• Correct Answer: Option {poll_data.get('correct_option_id', 'N/A') + 1}\n"
                        diagnostics += f"• Answers: {len(poll_data.get('user_answers', {}))}\n"
                    else:
                        diagnostics += f"• Status: ⚠️ Poll data expired/unavailable\n"
                
                # Check if it's a media message
                if replied_msg.photo:
                    diagnostics += f"\n**📷 Media:**\n"
                    diagnostics += f"• Type: Photo\n"
                    diagnostics += f"• File ID: `{replied_msg.photo[-1].file_id[:30]}...`\n"
                elif replied_msg.video:
                    diagnostics += f"\n**🎥 Media:**\n"
                    diagnostics += f"• Type: Video\n"
                    diagnostics += f"• File ID: `{replied_msg.video.file_id[:30]}...`\n"
                elif replied_msg.document:
                    diagnostics += f"\n**📄 Media:**\n"
                    diagnostics += f"• Type: Document\n"
                    diagnostics += f"• File ID: `{replied_msg.document.file_id[:30]}...`\n"
                
                # Check for text content
                if replied_msg.text:
                    text_preview = replied_msg.text[:100] + "..." if len(replied_msg.text) > 100 else replied_msg.text
                    diagnostics += f"\n**📝 Text Content:**\n"
                    diagnostics += f"```\n{text_preview}\n```\n"
                elif replied_msg.caption:
                    caption_preview = replied_msg.caption[:100] + "..." if len(replied_msg.caption) > 100 else replied_msg.caption
                    diagnostics += f"\n**📝 Caption:**\n"
                    diagnostics += f"```\n{caption_preview}\n```\n"
                
                diagnostics += "\n━━━━━━━━━━━━━━━━━━━\n"
                diagnostics += "💡 Use this info to debug issues or verify data"
                
                # Log contextual diagnostics activity
                self.db.log_activity(
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
            
            # Determine action for logging
            action = 'help' if not context.args or len(context.args) == 0 else (context.args[0] if not context.args[0].isdigit() else 'quick_add')
            target_user = context.args[1] if context.args and len(context.args) > 1 else (context.args[0] if context.args and len(context.args) > 0 and context.args[0].isdigit() else None)
            
            # Log command execution immediately
            self.db.log_activity(
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
            
            # Check if first argument is a number (user ID for quick add)
            try:
                user_id = int(context.args[0])
                # Quick add: /dev 123456
                # Try to fetch user info from Telegram
                try:
                    user_info = await context.bot.get_chat(user_id)
                    username = user_info.username if hasattr(user_info, 'username') and user_info.username else ""
                    first_name = user_info.first_name if hasattr(user_info, 'first_name') and user_info.first_name else ""
                    last_name = user_info.last_name if hasattr(user_info, 'last_name') and user_info.last_name else ""
                    
                    self.db.add_developer(
                        user_id=user_id,
                        username=username,
                        first_name=first_name,
                        last_name=last_name,
                        added_by=update.effective_user.id
                    )
                    
                    display_name = first_name or username or f"User {user_id}"
                    dev_mention = f'<b>{html.escape(str(display_name))}</b>'
                    reply = await update.message.reply_text(
                        f"✅ Developer added successfully!\n\n"
                        f"👤 {dev_mention}\n"
                        f"🆔 ID: {user_id}",
                        parse_mode=ParseMode.HTML,
                        link_preview_options=LinkPreviewOptions(is_disabled=True)
                    )
                except Exception as e:
                    logger.warning(f"Could not fetch user info for {user_id}: {e}")
                    # Add without user info
                    self.db.add_developer(user_id, added_by=update.effective_user.id)
                    dev_mention = f'<b>User {user_id}</b>'
                    reply = await update.message.reply_text(
                        f"✅ Developer added successfully!\n\n"
                        f"👤 {dev_mention}\n"
                        f"⚠️ Could not fetch user details",
                        parse_mode=ParseMode.HTML,
                        link_preview_options=LinkPreviewOptions(is_disabled=True)
                    )

                logger.info(f"Developer {user_id} added by {update.effective_user.id}")
                await self.auto_clean_message(update.message, reply)
                return
            except ValueError:
                # Not a number, treat as action
                pass
            
            action = context.args[0].lower()
            
            if action == "add":
                if len(context.args) < 2:
                    reply = await update.message.reply_text("❌ Usage: /dev add [user_id]")
                    await self.auto_clean_message(update.message, reply)
                    return
                
                try:
                    new_dev_id = int(context.args[1])
                    
                    # Try to fetch user info from Telegram
                    try:
                        user_info = await context.bot.get_chat(new_dev_id)
                        username = user_info.username if hasattr(user_info, 'username') and user_info.username else ""
                        first_name = user_info.first_name if hasattr(user_info, 'first_name') and user_info.first_name else ""
                        last_name = user_info.last_name if hasattr(user_info, 'last_name') and user_info.last_name else ""
                        
                        self.db.add_developer(
                            user_id=new_dev_id,
                            username=username,
                            first_name=first_name,
                            last_name=last_name,
                            added_by=update.effective_user.id
                        )
                        
                        display_name = first_name or username or f"User {new_dev_id}"
                        dev_mention = f'<b>{html.escape(str(display_name))}</b>'
                        reply = await update.message.reply_text(
                            f"✅ Developer added successfully!\n\n"
                            f"👤 {dev_mention}\n"
                            f"🆔 ID: {new_dev_id}",
                            parse_mode=ParseMode.HTML,
                            link_preview_options=LinkPreviewOptions(is_disabled=True)
                        )
                    except Exception as e:
                        logger.warning(f"Could not fetch user info for {new_dev_id}: {e}")
                        # Add without user info
                        self.db.add_developer(new_dev_id, added_by=update.effective_user.id)
                        dev_mention = f'<b>User {new_dev_id}</b>'
                        reply = await update.message.reply_text(
                            f"✅ Developer added successfully!\n\n"
                            f"👤 {dev_mention}\n"
                            f"⚠️ Could not fetch user details",
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
                    
                    if self.db.remove_developer(dev_id):
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
                developers = self.db.get_all_developers()
                
                # Premium formatted developer panel with Unicode box drawing
                dev_text = """╔══════════════════╗
║ 👥 𝐃𝐞𝐯𝐞𝐥𝐨𝐩𝐞𝐫 & 𝐀𝐝𝐦𝐢𝐧 𝐏𝐚𝐧𝐞𝐥 
╚══════════════════╝

👑 𝗗𝗘𝗩𝗘𝗟𝗢𝗣𝗘𝗥𝗦
━━━━━━━━━━━━━━━━━━\n"""
                
                # Get OWNER info
                try:
                    owner_user = await context.bot.get_chat(config.OWNER_ID)
                    owner_name = owner_user.first_name or "Owner"
                except Exception as e:
                    logger.debug(f"Could not fetch owner info: {e}")
                    owner_name = "Owner"

                owner_mention = f'<b>{html.escape(str(owner_name))}</b>'
                dev_text += f"• {owner_mention} (ID: {config.OWNER_ID})\n"

                # Get WIFU info if exists
                if config.WIFU_ID:
                    try:
                        wifu_user = await context.bot.get_chat(config.WIFU_ID)
                        wifu_name = wifu_user.first_name or "Developer"
                        # Check if name has emoji, otherwise it might be the second developer
                        wifu_mention = f'<b>{html.escape(str(wifu_name))}</b>'
                        dev_text += f"• {wifu_mention} (ID: {config.WIFU_ID})\n"
                    except Exception as e:
                        logger.debug(f"Could not fetch WIFU info: {e}")
                        wifu_mention = f'<b>Developer</b>'
                        dev_text += f"• {wifu_mention} (ID: {config.WIFU_ID})\n"

                # Show other developers from database
                if developers:
                    for dev in developers:
                        dev_uid = dev['user_id']
                        try:
                            dev_user = await context.bot.get_chat(dev_uid)
                            dev_name = dev_user.first_name or f"User{dev_uid}"
                            d_mention = f'<b>{html.escape(str(dev_name))}</b>'
                            dev_text += f"• {d_mention} (ID: {dev_uid})\n"
                        except Exception as e:
                            logger.debug(f"Could not fetch developer info: {e}")
                            d_name = dev.get('username') or dev.get('first_name') or f"User{dev_uid}"
                            d_mention = f'<b>{html.escape(str(d_name))}</b>'
                            dev_text += f"• {d_mention} (ID: {dev_uid})\n"

                reply = await update.message.reply_text(dev_text, parse_mode=ParseMode.HTML,
                                                        link_preview_options=LinkPreviewOptions(is_disabled=True))
                await self.auto_clean_message(update.message, reply)
            
            else:
                reply = await update.message.reply_text("❌ Unknown action. Use: add, remove, or list")
                await self.auto_clean_message(update.message, reply)
            
            # Calculate response time at end
            response_time = int((time.time() - start_time) * 1000)
            logger.debug(f"Command /dev completed in {response_time}ms")
        
        except Exception as e:
            response_time = int((time.time() - start_time) * 1000)
            if update.effective_user and update.effective_chat:
                self.db.log_activity(
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
    
    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Enhanced real-time statistics dashboard with live activity feed"""
        start_time = time.time()
        try:
            if not await self.check_access(update):
                await self.send_unauthorized_message(update)
                return
            
            if not update.effective_user or not update.effective_chat or not update.message:
                return
            
            # Log command execution immediately
            self.db.log_activity(
                activity_type='command',
                user_id=update.effective_user.id,
                chat_id=update.effective_chat.id,
                username=update.effective_user.username or "",
                chat_title=getattr(update.effective_chat, 'title', None) or "",
                command='/stats',
                details={'stats_type': 'real_time_dashboard'},
                success=True
            )
            
            loading = await update.message.reply_text("📊 Loading real-time statistics...")
            
            try:
                # Get user & group metrics
                all_users = self.db.get_all_users_stats()
                
                # Count PM users vs Group-only users
                pm_users = sum(1 for user in all_users if user.get('has_pm_access') == 1)
                group_only_users = sum(1 for user in all_users if user.get('has_pm_access') == 0 or user.get('has_pm_access') is None)
                total_users = pm_users + group_only_users
                
                all_groups = self.db.get_all_groups()
                total_groups = len(all_groups)
                
                user_engagement = self.db.get_user_engagement_stats()
                active_today = user_engagement.get('active_today', 0)
                
                # Get quiz activity (real-time from activity_logs)
                quiz_stats_today = self.db.get_quiz_stats_by_period('today')
                quiz_stats_week = self.db.get_quiz_stats_by_period('week')
                quiz_stats_month = self.db.get_quiz_stats_by_period('month')
                
                quizzes_today = quiz_stats_today.get('total_quizzes', 0)
                quizzes_week = quiz_stats_week.get('total_quizzes', 0)
                quizzes_month = quiz_stats_month.get('total_quizzes', 0)
                
                # Get total quizzes answered (all time)
                all_time_stats = self.db.get_quiz_stats_by_period('all')
                quizzes_total = all_time_stats.get('total_quizzes', 0)
                
                success_rate = quiz_stats_week.get('success_rate', 0)
                
                # Get performance metrics (24h)
                perf_summary = self.db.get_performance_summary(24)
                avg_time = int(perf_summary.get('avg_response_time', 0))
                
                # Get total commands executed in last 24h
                activity_stats_24h = self.db.get_activity_stats(1)
                commands_24h = activity_stats_24h.get('by_type', {}).get('command', 0)
                
                # Get error rate
                error_stats = self.db.get_error_rate_stats(1)
                error_rate = error_stats.get('rate', 0)
                
                # Get top commands (last 7 days)
                command_usage = self.db.get_command_usage_stats(7)
                top_commands = sorted(command_usage.items(), key=lambda x: x[1], reverse=True)[:5]
                
                if top_commands:
                    command_list = "\n".join([f"• {cmd}: {count:,}x" for cmd, count in top_commands])
                else:
                    command_list = "• No commands yet"
                
                # Get recent activity feed (last 10 activities)
                recent_activities = self.db.get_recent_activities(limit=10)
                activity_feed = ""
                
                if recent_activities:
                    for activity in recent_activities:
                        time_ago = self.format_relative_time(activity.get('timestamp', ''))
                        activity_type = activity.get('activity_type', 'unknown')
                        username = activity.get('username', 'Unknown')
                        # Escape underscores in username to prevent Markdown issues
                        safe_username = username.replace('_', '\\_') if username else 'Unknown'
                        command = activity.get('command', '')
                        
                        if activity_type == 'command' and command:
                            activity_feed += f"• {time_ago}: @{safe_username} used {command}\n"
                        elif activity_type == 'quiz_sent':
                            activity_feed += f"• {time_ago}: Quiz sent\n"
                        elif activity_type == 'quiz_answered':
                            activity_feed += f"• {time_ago}: @{safe_username} answered quiz\n"
                        elif activity_type == 'broadcast':
                            activity_feed += f"• {time_ago}: Broadcast sent\n"
                        elif activity_type == 'error':
                            activity_feed += f"• {time_ago}: Error logged\n"
                        else:
                            activity_feed += f"• {time_ago}: {activity_type}\n"
                else:
                    activity_feed = "• No recent activity"
                
                # Format the complete stats message
                stats_text = (
                    f"📊 𝗕𝗼𝘁 𝗦𝘁𝗮𝘁𝘀\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"• 🌐 Total Groups: {total_groups} groups\n"
                    f"• 👤 PM Users: {pm_users} users\n"
                    f"• 👥 Group-only Users: {group_only_users} users\n"
                    f"• 👥 Total Users: {total_users} users\n\n"
                    f"════════════════════\n"
                    f"🤖 𝗢𝘃𝗲𝗿𝗮𝗹𝗹 𝗣𝗲𝗿𝗳𝗼𝗿𝗺𝗮𝗻𝗰𝗲\n"
                    f"────────────────────\n"
                    f"• Today: {quizzes_today}\n"
                    f"• This Week: {quizzes_week}\n"
                    f"• This Month: {quizzes_month}\n"
                    f"• Total: {quizzes_total}\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"✨ Keep quizzing & growing! 🚀"
                )
                
                await loading.edit_text(stats_text, parse_mode=ParseMode.MARKDOWN)
                logger.info(f"Real-time stats displayed to {update.effective_user.id}")
            
            except Exception as e:
                logger.error(f"Error generating real-time stats: {e}", exc_info=True)
                await loading.edit_text("❌ Error generating statistics. Please try again.")
            
            # Calculate response time at end
            response_time = int((time.time() - start_time) * 1000)
            logger.debug(f"Command /stats completed in {response_time}ms")
        
        except Exception as e:
            response_time = int((time.time() - start_time) * 1000)
            if update.effective_user and update.effective_chat:
                self.db.log_activity(
                    activity_type='error',
                    user_id=update.effective_user.id,
                    chat_id=update.effective_chat.id,
                    command='/stats',
                    details={'error': str(e)},
                    success=False,
                    response_time_ms=response_time
                )
            logger.error(f"Error in stats command: {e}", exc_info=True)
            if update.message:
                reply = await update.message.reply_text("❌ Error retrieving statistics")
                await self.auto_clean_message(update.message, reply)
    
    async def broadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Enhanced broadcast supporting media, buttons, placeholders, and auto-cleanup"""
        start_time = time.time()
        try:
            if not await self.check_access(update):
                await self.send_unauthorized_message(update)
            
            if not update.effective_user or not update.effective_chat or not update.message:
                return
            
            # Determine media type and recipient counts for logging (PM-accessible users only)
            users = self.db.get_pm_accessible_users()
            groups = self.db.get_all_groups()
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
            self.db.log_activity(
                activity_type='command',
                user_id=update.effective_user.id,
                chat_id=update.effective_chat.id,
                username=update.effective_user.username or "",
                chat_title=getattr(update.effective_chat, 'title', None) or "",
                command='/broadcast',
                details={'recipient_count': total_targets, 'media_type': media_type, 'users': len(users), 'groups': len(groups)},
                success=True
            )
            
            # Check if replying to a message
            if update.message.reply_to_message:
                replied_message = update.message.reply_to_message
                
                users = self.db.get_pm_accessible_users()
                groups = self.db.get_all_groups()
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
                
                users = self.db.get_pm_accessible_users()
                groups = self.db.get_all_groups()
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
            
            # Calculate response time at end
            response_time = int((time.time() - start_time) * 1000)
            logger.debug(f"Command /broadcast completed in {response_time}ms")
        
        except Exception as e:
            response_time = int((time.time() - start_time) * 1000)
            if update.effective_user and update.effective_chat:
                self.db.log_activity(
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
        """Confirm and send broadcast with media, buttons, placeholders, and auto-cleanup"""
        start_time = time.time()
        try:
            if not await self.check_access(update):
                await self.send_unauthorized_message(update)
                return
            
            if not update.effective_user or not update.effective_chat or not update.message:
                return
            
            # Log command execution immediately
            broadcast_type = context.user_data.get('broadcast_type', 'unknown') if context.user_data else 'unknown'
            self.db.log_activity(
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
            users = self.db.get_pm_accessible_users()  # Only users with PM access
            groups = self.db.get_all_groups()  # Active groups only
            
            success_count = 0
            fail_count = 0
            pm_sent = 0
            group_sent = 0
            skipped_count = 0  # Auto-removed users/groups
            
            # Create unique broadcast ID for tracking
            broadcast_id = f"broadcast_{int(time.time())}_{update.effective_user.id}"
            
            # OPTIMIZATION: Cache bot name once instead of calling for each recipient
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
                        # CONSTRAINED AUTO-CLEANUP: Only delete on specific permission errors
                        if "Forbidden: bot was blocked by the user" in error_msg:
                            logger.info(f"AUTO-CLEANUP: Removing user {user['user_id']} - {error_msg}")
                            self.db.remove_inactive_user(user['user_id'])
                            skipped_count += 1
                        elif "Forbidden: user is deactivated" in error_msg:
                            logger.info(f"AUTO-CLEANUP: Removing user {user['user_id']} - {error_msg}")
                            self.db.remove_inactive_user(user['user_id'])
                            skipped_count += 1
                        elif "Forbidden" in error_msg:
                            # Generic Forbidden - don't delete, just log
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
                        # OPTIMIZED AUTO-CLEANUP: Handle all kicked/removed scenarios
                        if any(keyword in error_msg.lower() for keyword in [
                            "bot was kicked", 
                            "bot is not a member",
                            "chat not found",
                            "group chat was deactivated",
                            "chat has been deleted",
                            "forum topic is closed"
                        ]):
                            logger.info(f"AUTO-CLEANUP: Removing group {group['chat_id']} from database and active chats - {error_msg}")
                            self.db.remove_inactive_group(group['chat_id'])
                            # Also remove from active_chats
                            if hasattr(self, 'quiz_manager'):
                                self.quiz_manager.remove_active_chat(group['chat_id'])
                            skipped_count += 1
                        elif "Forbidden" in error_msg:
                            # Generic Forbidden - don't delete, just log
                            logger.warning(f"SAFETY: Not auto-removing group {group['chat_id']} - error: {error_msg}")
                            fail_count += 1
                        else:
                            logger.warning(f"Failed to send to group {group['chat_id']}: {error_msg}")
                            fail_count += 1
            
            elif broadcast_type in ['photo', 'video', 'document', 'animation']:
                # Media broadcast with placeholder support
                media_file_id = context.user_data.get('broadcast_media_id') if context.user_data else None
                base_caption = context.user_data.get('broadcast_caption') if context.user_data else None
                reply_markup = context.user_data.get('broadcast_buttons') if context.user_data else None
                
                if not media_file_id:
                    reply = await update.message.reply_text("❌ Missing media file ID. Please use /broadcast again.")
                    await self.auto_clean_message(update.message, reply)
                    return
                
                # Ensure base_caption is a string
                if base_caption is None:
                    base_caption = ""
                
                # Truncate caption to Telegram's 1024 character limit
                if len(base_caption) > 1024:
                    base_caption = base_caption[:1021] + "..."
                    logger.warning(f"Caption truncated to 1024 chars for broadcast")
                
                # Send to users (PM)
                for user in users:
                    try:
                        # OPTIMIZED: Apply placeholders using database data (no API call!)
                        caption = await self.replace_placeholders(base_caption or "", user['user_id'], context, 
                            user_data=user, bot_name_cache=bot_name_cache
                        )
                        
                        # Send appropriate media type
                        if broadcast_type == 'photo':
                            sent_msg = await context.bot.send_photo(
                                chat_id=user['user_id'],
                                photo=media_file_id,
                                caption=caption if caption else None,
                                reply_markup=reply_markup
                            )
                        elif broadcast_type == 'video':
                            sent_msg = await context.bot.send_video(
                                chat_id=user['user_id'],
                                video=media_file_id,
                                caption=caption if caption else None,
                                reply_markup=reply_markup
                            )
                        elif broadcast_type == 'document':
                            sent_msg = await context.bot.send_document(
                                chat_id=user['user_id'],
                                document=media_file_id,
                                caption=caption if caption else None,
                                reply_markup=reply_markup
                            )
                        elif broadcast_type == 'animation':
                            sent_msg = await context.bot.send_animation(
                                chat_id=user['user_id'],
                                animation=media_file_id,
                                caption=caption if caption else None,
                                reply_markup=reply_markup
                            )
                        else:
                            # Invalid broadcast type, skip this user
                            continue
                        
                        sent_messages[user['user_id']] = sent_msg.message_id
                        success_count += 1
                        pm_sent += 1
                        if len(users) > 20:
                            await asyncio.sleep(0.03)
                    except Exception as e:
                        error_msg = str(e)
                        # CONSTRAINED AUTO-CLEANUP: Only delete on specific permission errors
                        if "Forbidden: bot was blocked by the user" in error_msg:
                            logger.info(f"AUTO-CLEANUP: Removing user {user['user_id']} - {error_msg}")
                            self.db.remove_inactive_user(user['user_id'])
                            skipped_count += 1
                        elif "Forbidden: user is deactivated" in error_msg:
                            logger.info(f"AUTO-CLEANUP: Removing user {user['user_id']} - {error_msg}")
                            self.db.remove_inactive_user(user['user_id'])
                            skipped_count += 1
                        elif "Forbidden" in error_msg:
                            # Generic Forbidden - don't delete, just log
                            logger.warning(f"SAFETY: Not removing user {user['user_id']} - error was: {error_msg}")
                            fail_count += 1
                        else:
                            logger.warning(f"Failed to send to user {user['user_id']}: {error_msg}")
                            fail_count += 1
                
                # Send to groups
                for group in groups:
                    try:
                        # OPTIMIZED: Apply placeholders using database data (no API call!)
                        caption = await self.replace_placeholders(base_caption or "", group['chat_id'], context, 
                            group_data=group, bot_name_cache=bot_name_cache
                        )
                        
                        # Send appropriate media type
                        if broadcast_type == 'photo':
                            sent_msg = await context.bot.send_photo(
                                chat_id=group['chat_id'],
                                photo=media_file_id,
                                caption=caption if caption else None,
                                reply_markup=reply_markup
                            )
                        elif broadcast_type == 'video':
                            sent_msg = await context.bot.send_video(
                                chat_id=group['chat_id'],
                                video=media_file_id,
                                caption=caption if caption else None,
                                reply_markup=reply_markup
                            )
                        elif broadcast_type == 'document':
                            sent_msg = await context.bot.send_document(
                                chat_id=group['chat_id'],
                                document=media_file_id,
                                caption=caption if caption else None,
                                reply_markup=reply_markup
                            )
                        elif broadcast_type == 'animation':
                            sent_msg = await context.bot.send_animation(
                                chat_id=group['chat_id'],
                                animation=media_file_id,
                                caption=caption if caption else None,
                                reply_markup=reply_markup
                            )
                        else:
                            # Invalid broadcast type, skip this group
                            continue
                        
                        sent_messages[group['chat_id']] = sent_msg.message_id
                        success_count += 1
                        group_sent += 1
                        if len(groups) > 20:
                            await asyncio.sleep(0.03)
                    except Exception as e:
                        error_msg = str(e)
                        # OPTIMIZED AUTO-CLEANUP: Handle all kicked/removed scenarios
                        if any(keyword in error_msg.lower() for keyword in [
                            "bot was kicked", 
                            "bot is not a member",
                            "chat not found",
                            "group chat was deactivated",
                            "chat has been deleted",
                            "forum topic is closed"
                        ]):
                            logger.info(f"AUTO-CLEANUP: Removing group {group['chat_id']} from database and active chats - {error_msg}")
                            self.db.remove_inactive_group(group['chat_id'])
                            # Also remove from active_chats
                            if hasattr(self, 'quiz_manager'):
                                self.quiz_manager.remove_active_chat(group['chat_id'])
                            skipped_count += 1
                        elif "Forbidden" in error_msg:
                            # Generic Forbidden - don't delete, just log
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
                        # OPTIMIZED: Apply placeholders using database data (no API call!)
                        message_text = await self.replace_placeholders(base_message_text or "", user['user_id'], context,
                            user_data=user, bot_name_cache=bot_name_cache
                        )
                        
                        # Try sending with Markdown first, fallback to plain text if parse error
                        try:
                            sent_msg = await context.bot.send_message(
                                chat_id=user['user_id'],
                                text=message_text,
                                parse_mode=ParseMode.MARKDOWN,
                                reply_markup=reply_markup
                            )
                        except Exception as parse_error:
                            if "parse entities" in str(parse_error).lower() or "can't parse" in str(parse_error).lower():
                                # Fallback to plain text on Markdown parse error
                                logger.warning(f"Markdown parse error for user {user['user_id']}, falling back to plain text")
                                sent_msg = await context.bot.send_message(
                                    chat_id=user['user_id'],
                                    text=message_text,
                                    parse_mode=None,
                                    reply_markup=reply_markup
                                )
                            else:
                                raise
                        
                        sent_messages[user['user_id']] = sent_msg.message_id
                        success_count += 1
                        pm_sent += 1
                        if len(users) > 20:
                            await asyncio.sleep(0.03)
                    except Exception as e:
                        error_msg = str(e)
                        # CONSTRAINED AUTO-CLEANUP: Only delete on specific permission errors
                        if "Forbidden: bot was blocked by the user" in error_msg:
                            logger.info(f"AUTO-CLEANUP: Removing user {user['user_id']} - {error_msg}")
                            self.db.remove_inactive_user(user['user_id'])
                            skipped_count += 1
                        elif "Forbidden: user is deactivated" in error_msg:
                            logger.info(f"AUTO-CLEANUP: Removing user {user['user_id']} - {error_msg}")
                            self.db.remove_inactive_user(user['user_id'])
                            skipped_count += 1
                        elif "Forbidden" in error_msg:
                            # Generic Forbidden - don't delete, just log
                            logger.warning(f"SAFETY: Not removing user {user['user_id']} - error was: {error_msg}")
                            fail_count += 1
                        else:
                            logger.warning(f"Failed to send to user {user['user_id']}: {error_msg}")
                            fail_count += 1
                
                # Send to groups
                for group in groups:
                    try:
                        # OPTIMIZED: Apply placeholders using database data (no API call!)
                        message_text = await self.replace_placeholders(base_message_text or "", group['chat_id'], context,
                            group_data=group, bot_name_cache=bot_name_cache
                        )
                        
                        # Try sending with Markdown first, fallback to plain text if parse error
                        try:
                            sent_msg = await context.bot.send_message(
                                chat_id=group['chat_id'],
                                text=message_text,
                                parse_mode=ParseMode.MARKDOWN,
                                reply_markup=reply_markup
                            )
                        except Exception as parse_error:
                            if "parse entities" in str(parse_error).lower() or "can't parse" in str(parse_error).lower():
                                # Fallback to plain text on Markdown parse error
                                logger.warning(f"Markdown parse error for group {group['chat_id']}, falling back to plain text")
                                sent_msg = await context.bot.send_message(
                                    chat_id=group['chat_id'],
                                    text=message_text,
                                    parse_mode=None,
                                    reply_markup=reply_markup
                                )
                            else:
                                raise
                        
                        sent_messages[group['chat_id']] = sent_msg.message_id
                        success_count += 1
                        group_sent += 1
                        if len(groups) > 20:
                            await asyncio.sleep(0.03)
                    except Exception as e:
                        error_msg = str(e)
                        # OPTIMIZED AUTO-CLEANUP: Handle all kicked/removed scenarios
                        if any(keyword in error_msg.lower() for keyword in [
                            "bot was kicked", 
                            "bot is not a member",
                            "chat not found",
                            "group chat was deactivated",
                            "chat has been deleted",
                            "forum topic is closed"
                        ]):
                            logger.info(f"AUTO-CLEANUP: Removing group {group['chat_id']} from database and active chats - {error_msg}")
                            self.db.remove_inactive_group(group['chat_id'])
                            # Also remove from active_chats
                            if hasattr(self, 'quiz_manager'):
                                self.quiz_manager.remove_active_chat(group['chat_id'])
                            skipped_count += 1
                        elif "Forbidden" in error_msg:
                            # Generic Forbidden - don't delete, just log
                            logger.warning(f"SAFETY: Not auto-removing group {group['chat_id']} - error: {error_msg}")
                            fail_count += 1
                        else:
                            logger.warning(f"Failed to send to group {group['chat_id']}: {error_msg}")
                            fail_count += 1
            
            # Store broadcast in database (single doc keeps messages + stats together
            # so delbroadcast can always find the messages via get_latest_broadcast)
            total_targets = len(users) + len(groups)
            message_text = (context.user_data.get('broadcast_message', '') if context.user_data else '')[:500] if broadcast_type == 'text' else f"[{broadcast_type.upper()} BROADCAST]"
            if sent_messages:
                self.db.save_broadcast({
                    "broadcast_id":  broadcast_id,
                    "user_id":       update.effective_user.id,
                    "messages":      sent_messages,
                    "admin_id":      update.effective_user.id,
                    "message_text":  message_text,
                    "total_targets": total_targets,
                    "sent_count":    success_count,
                    "failed_count":  fail_count,
                    "skipped_count": skipped_count,
                    "created_at":    datetime.utcnow().isoformat(),
                })
                logger.info(f"Saved broadcast {broadcast_id} to database with {len(sent_messages)} messages")
            
            # Get stats for result message (from all users, not just PM users)
            all_users = self.db.get_all_users_stats()
            pm_users_count = sum(1 for user in all_users if user.get('has_pm_access') == 1)
            group_only_users = sum(1 for user in all_users if user.get('has_pm_access') == 0 or user.get('has_pm_access') is None)
            total_users_count = pm_users_count + group_only_users
            total_groups_count = len(groups)
            
            # Get quiz performance stats
            quiz_stats_today = self.db.get_quiz_stats_by_period('today')
            quiz_stats_week = self.db.get_quiz_stats_by_period('week')
            quiz_stats_month = self.db.get_quiz_stats_by_period('month')
            all_time_stats = self.db.get_quiz_stats_by_period('all')
            
            quizzes_today = quiz_stats_today.get('total_quizzes', 0)
            quizzes_week = quiz_stats_week.get('total_quizzes', 0)
            quizzes_month = quiz_stats_month.get('total_quizzes', 0)
            quizzes_total = all_time_stats.get('total_quizzes', 0)
            
            # Build optimized result message
            result_text = f"✅ Broadcast completed!\n\n"
            result_text += f"📱 PM Sent: {pm_sent}\n"
            result_text += f"👥 Groups Sent: {group_sent}\n"
            result_text += f"━━━━━━━━━━━━━━━\n"
            result_text += f"✅ Total Sent: {success_count}\n"
            if skipped_count > 0:
                result_text += f"🗑️ Auto-Cleaned: {skipped_count} (kicked/inactive)\n"
            if fail_count > 0:
                result_text += f"⚠️ Skipped: {fail_count} (access restricted)\n"
            
            result_text += f"\n📊 𝗕𝗼𝘁 𝗦𝘁𝗮𝘁𝘀\n"
            result_text += f"━━━━━━━━━━━━━━━━━━━━\n"
            result_text += f"• 🌐 Total Groups: {total_groups_count} groups\n"
            result_text += f"• 👤 PM Users: {pm_users_count} users\n"
            result_text += f"• 👥 Group-only Users: {group_only_users} users\n"
            result_text += f"• 👥 Total Users: {total_users_count} users\n\n"
            result_text += f"════════════════════\n"
            result_text += f"🤖 𝗢𝘃𝗲𝗿𝗮𝗹𝗹 𝗣𝗲𝗿𝗳𝗼𝗿𝗺𝗮𝗻𝗰𝗲\n"
            result_text += f"────────────────────\n"
            result_text += f"• Today: {quizzes_today}\n"
            result_text += f"• This Week: {quizzes_week}\n"
            result_text += f"• This Month: {quizzes_month}\n"
            result_text += f"• Total: {quizzes_total}\n\n"
            result_text += f"━━━━━━━━━━━━━━━━━━━━\n"
            result_text += f"✨ Keep quizzing & growing! 🚀"
            
            await status.edit_text(result_text)
            
            logger.info(f"Broadcast completed by {update.effective_user.id}: {pm_sent} PMs, {group_sent} groups ({success_count} total, {fail_count} failed, {skipped_count} auto-removed)")
            
            # Clear broadcast data
            if context.user_data is not None:
                context.user_data.pop('broadcast_message', None)
            if context.user_data is not None:
                context.user_data.pop('broadcast_message_id', None)
            if context.user_data is not None:
                context.user_data.pop('broadcast_chat_id', None)
            if context.user_data is not None:
                context.user_data.pop('broadcast_type', None)
            if context.user_data is not None:
                context.user_data.pop('broadcast_media_id', None)
            if context.user_data is not None:
                context.user_data.pop('broadcast_caption', None)
            if context.user_data is not None:
                context.user_data.pop('broadcast_buttons', None)
            
            # Calculate response time at end
            response_time = int((time.time() - start_time) * 1000)
            logger.debug(f"Command /broadcast_confirm completed in {response_time}ms - sent: {success_count}, failed: {fail_count}")
        
        except Exception as e:
            response_time = int((time.time() - start_time) * 1000)
            if update.effective_user and update.effective_chat:
                self.db.log_activity(
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
                reply = await update.message.reply_text("❌ Error sending broadcast")
                await self.auto_clean_message(update.message, reply)
    
    async def delbroadcast(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Delete latest broadcast from all groups/users - Works from anywhere!"""
        start_time = time.time()
        try:
            if not await self.check_access(update):
                await self.send_unauthorized_message(update)
                return
            
            if not update.effective_user or not update.effective_chat or not update.message:
                return
            
            # Get latest broadcast from database
            broadcast_data = self.db.get_latest_broadcast()
            target_count = len(broadcast_data.get('messages', [])) if broadcast_data else 0
            
            # Log command execution immediately
            self.db.log_activity(
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

            # Store integer broadcast ID in context for confirmation (prevents race condition with multiple broadcasts)
            if context.user_data is not None:
                context.user_data['pending_delete_broadcast_id'] = broadcast_data.get('id')
            
            # Confirm deletion
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
            logger.info(f"Broadcast deletion prepared by {update.effective_user.id} for {len(broadcast_messages)} chats (ID: {broadcast_data['broadcast_id']})")
            
            # Calculate response time at end
            response_time = int((time.time() - start_time) * 1000)
            logger.debug(f"Command /delbroadcast completed in {response_time}ms")
        
        except Exception as e:
            response_time = int((time.time() - start_time) * 1000)
            if update.effective_user and update.effective_chat:
                self.db.log_activity(
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
        """Confirm and execute broadcast deletion - Optimized for instant deletion"""
        start_time = time.time()
        try:
            if not await self.check_access(update):
                await self.send_unauthorized_message(update)
                return
            
            if not update.effective_user or not update.effective_chat or not update.message:
                return
            
            # Get the specific broadcast ID from context (set by /delbroadcast)
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
            
            # Log command execution immediately
            self.db.log_activity(
                activity_type='command',
                user_id=update.effective_user.id,
                chat_id=update.effective_chat.id,
                username=update.effective_user.username or "",
                chat_title=getattr(update.effective_chat, 'title', None) or "",
                command='/delbroadcast_confirm',
                details={'action': 'confirm_deletion', 'broadcast_id': pending_broadcast_id},
                success=True
            )
            
            # Get the specific broadcast by ID (not latest, to prevent race conditions)
            broadcast_data = self.db.get_broadcast_by_id(pending_broadcast_id)
            
            if not broadcast_data:
                reply = await update.message.reply_text(
                    "❌ Broadcast not found or already deleted.\n\n"
                    f"The broadcast (ID: {pending_broadcast_id}) may have been deleted already."
                )
                await self.auto_clean_message(update.message, reply)
                # Clear the stored ID
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
            
            # Delete from all chats instantly
            for chat_id_str, message_id in broadcast_messages.items():
                try:
                    chat_id = int(chat_id_str)  # Convert string to int (JSON keys are strings)
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
            
            logger.info(f"Broadcast deletion by {update.effective_user.id}: {success_count} deleted, {fail_count} failed (ID: {broadcast_id})")
            
            # Clear broadcast data from database (use int id)
            self.db.delete_broadcast(pending_broadcast_id)
            
            # Clear the stored broadcast ID from context
            if context.user_data is not None:
                context.user_data.pop('pending_delete_broadcast_id', None)
            
            # Calculate response time at end
            response_time = int((time.time() - start_time) * 1000)
            logger.debug(f"Command /delbroadcast_confirm completed in {response_time}ms - deleted: {success_count}, failed: {fail_count}")
        
        except Exception as e:
            response_time = int((time.time() - start_time) * 1000)
            if update.effective_user and update.effective_chat:
                self.db.log_activity(
                    activity_type='error',
                    user_id=update.effective_user.id,
                    chat_id=update.effective_chat.id,
                    command='/delbroadcast_confirm',
                    details={'error': str(e)},
                    success=False,
                    response_time_ms=response_time
                )
            logger.error(f"Error in delbroadcast_confirm: {e}", exc_info=True)
            # Clear the stored broadcast ID on error too
            if context.user_data is not None:
                context.user_data.pop('pending_delete_broadcast_id', None)
            if update.message:
                reply = await update.message.reply_text("❌ Error deleting broadcast")
                await self.auto_clean_message(update.message, reply)
    
    async def performance_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show live performance metrics dashboard"""
        start_time = time.time()
        try:
            if not await self.check_access(update):
                await self.send_unauthorized_message(update)
                return
            
            if not update.effective_user or not update.effective_chat or not update.message:
                return
            
            self.db.log_activity(
                activity_type='command',
                user_id=update.effective_user.id,
                chat_id=update.effective_chat.id,
                username=update.effective_user.username or "",
                chat_title=getattr(update.effective_chat, 'title', None) or "",
                command='/performance',
                success=True
            )
            
            loading_msg = await update.message.reply_text("📊 Loading performance metrics...")
            
            hours = 24
            if context.args and context.args[0].isdigit():
                hours = int(context.args[0])
                hours = min(hours, 168)
            
            perf_summary = self.db.get_performance_summary(hours=hours)
            response_trends = self.db.get_response_time_trends(hours=hours)
            api_calls = self.db.get_api_call_counts(hours=hours)
            memory_history = self.db.get_memory_usage_history(hours=hours)

            # psutil is optional — graceful fallback if not installed
            current_memory_mb = 0.0
            try:
                import psutil
                current_memory_mb = psutil.Process().memory_info().rss / 1024 / 1024
            except Exception:
                pass

            # perf_summary keys: {"response_time": {"avg": X, "count": Y}, "memory": {...}, ...}
            rt_avg  = perf_summary.get("response_time", {}).get("avg", 0) or 0
            mem_avg = perf_summary.get("memory", {}).get("avg", 0) or 0
            total_api_calls = sum(api_calls.values()) if api_calls else 0

            perf_message = f"📊 *Performance Metrics Dashboard*\n"
            perf_message += f"🕒 *Period:* Last {hours} hours\n\n"

            perf_message += f"⚡ *Response Times:*\n"
            perf_message += f"• Average: {rt_avg:.2f}ms\n"
            if response_trends:
                recent_vals = [t['value'] for t in response_trends[-3:] if 'value' in t]
                if recent_vals:
                    recent_avg = sum(recent_vals) / len(recent_vals)
                    perf_message += f"• Recent (3h): {recent_avg:.2f}ms\n"
            perf_message += f"\n"

            perf_message += f"📞 *API Calls:*\n"
            perf_message += f"• Total: {total_api_calls:,}\n"
            if api_calls:
                top_api = sorted(api_calls.items(), key=lambda x: x[1], reverse=True)[:3]
                for api_name, count in top_api:
                    if api_name:
                        perf_message += f"• {api_name}: {count:,}\n"
            perf_message += f"\n"

            perf_message += f"💾 *Memory Usage:*\n"
            if current_memory_mb:
                perf_message += f"• Current: {current_memory_mb:.2f} MB\n"
            if mem_avg > 0:
                perf_message += f"• Average: {mem_avg:.2f} MB\n"
            if memory_history:
                mem_vals = [m['value'] for m in memory_history if 'value' in m]
                if mem_vals:
                    perf_message += f"• Peak: {max(mem_vals):.2f} MB\n"
                    perf_message += f"• Min: {min(mem_vals):.2f} MB\n"
            perf_message += f"\n"

            if response_trends:
                perf_message += f"📈 *Response Time Trends (last 5):*\n"
                for trend in response_trends[-5:]:
                    ts = trend.get('timestamp', '')[:16]
                    val = trend.get('value', 0)
                    perf_message += f"• {ts}: {val:.1f}ms\n"
                perf_message += f"\n"

            perf_message += f"💡 *Commands:*\n"
            perf_message += f"• /performance [hours] - Custom time period\n"
            perf_message += f"• Max 168 hours (7 days)\n"

            await loading_msg.edit_text(perf_message, parse_mode=ParseMode.MARKDOWN)

            response_time = int((time.time() - start_time) * 1000)
            logger.info(f"/performance dashboard shown in {response_time}ms")

            self.db.log_performance_metric(metric='response_time', value=response_time)
            
        except Exception as e:
            response_time = int((time.time() - start_time) * 1000)
            if update.effective_user and update.effective_chat:
                self.db.log_activity(
                    activity_type='error',
                    user_id=update.effective_user.id,
                    chat_id=update.effective_chat.id,
                    command='/performance',
                    details={'error': str(e)},
                    success=False,
                    response_time_ms=response_time
                )
            logger.error(f"Error in performance_stats: {e}", exc_info=True)
            if update.message:
                reply = await update.message.reply_text("❌ Error loading performance metrics")
                await self.auto_clean_message(update.message, reply)
    
    async def devstats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Comprehensive developer statistics dashboard"""
        start_time = time.time()
        try:
            if not await self.check_access(update):
                await self.send_unauthorized_message(update)
                return
            
            if not update.effective_user or not update.effective_chat or not update.message:
                return
            
            loading_msg = await update.message.reply_text("📊 Loading comprehensive dev stats...")
            
            from datetime import datetime, timedelta

            memory_mb = 0.0
            uptime_seconds = 0.0
            try:
                import psutil
                process = psutil.Process()
                memory_mb = process.memory_info().rss / 1024 / 1024
                uptime_seconds = (datetime.now() - datetime.fromtimestamp(process.create_time())).total_seconds()
            except Exception:
                pass
            if hasattr(self.quiz_manager, 'bot_start_time'):
                uptime_seconds = (datetime.now() - self.quiz_manager.bot_start_time).total_seconds()
            
            if uptime_seconds >= 86400:
                uptime_str = f"{uptime_seconds/86400:.1f} days"
            elif uptime_seconds >= 3600:
                uptime_str = f"{uptime_seconds/3600:.1f} hours"
            else:
                uptime_str = f"{uptime_seconds/60:.1f} minutes"
            
            perf_24h = self.db.get_performance_summary(24)
            activity_stats = self.db.get_activity_stats(1)
            
            total_users = self.db.get_user_engagement_stats().get('total_users', 0)
            total_groups = len(self.db.get_all_groups())
            active_today = self.db.get_active_users_count(1)
            active_week = self.db.get_active_users_count(7)
            active_month = self.db.get_active_users_count(30)

            new_users = self.db.get_new_users(7)
            most_active = self.db.get_most_active_users(5)
            
            quiz_today = self.db.get_quiz_stats_by_period('today')
            quiz_week = self.db.get_quiz_stats_by_period('week')
            
            commands_24h = activity_stats.get('by_type', {}).get('command', 0)
            quizzes_sent_24h = activity_stats.get('by_type', {}).get('quiz_sent', 0)
            quizzes_answered_24h = activity_stats.get('by_type', {}).get('quiz_answered', 0)
            broadcasts_24h = activity_stats.get('by_type', {}).get('broadcast', 0)
            errors_24h = activity_stats.get('by_type', {}).get('error', 0)
            
            recent_activities = self.db.get_recent_activities(10)
            activity_feed = ""
            for activity in recent_activities:
                time_ago = self.db.format_relative_time(activity['timestamp'])
                activity_type = activity.get('type') or activity.get('activity_type', 'unknown')
                username = activity.get('username', 'Unknown')
                
                if activity_type == 'command':
                    details = activity.get('details', {})
                    cmd = details.get('command', 'unknown') if isinstance(details, dict) else 'unknown'
                    activity_feed += f"• {time_ago}: @{username} /{cmd}\n"
                elif activity_type == 'quiz_sent':
                    activity_feed += f"• {time_ago}: Quiz sent\n"
                elif activity_type == 'quiz_answered':
                    activity_feed += f"• {time_ago}: @{username} answered\n"
                elif activity_type == 'broadcast':
                    activity_feed += f"• {time_ago}: Broadcast sent\n"
                elif activity_type == 'error':
                    activity_feed += f"• {time_ago}: Error logged\n"
                else:
                    activity_feed += f"• {time_ago}: {activity_type}\n"
            
            if not activity_feed:
                activity_feed = "No recent activity"
            
            most_active_text = ""
            for i, user in enumerate(most_active[:5], 1):
                name = user.get('name') or user.get('first_name') or user.get('username') or f"User{user['user_id']}"
                most_active_text += f"{i}. {name}: {user.get('total_answers', 0)} answers\n"
            if not most_active_text:
                most_active_text = "No active users yet"
            
            rt_avg_24h  = perf_24h.get("response_time", {}).get("avg", 0) or 0
            mem_avg_24h = perf_24h.get("memory", {}).get("avg", 0) or 0
            quiz_today_total = quiz_today.get('total_quizzes', 0)
            quiz_week_total  = quiz_week.get('total_quizzes', 0)
            quiz_week_correct= quiz_week.get('correct_answers', 0)
            success_rate = round(quiz_week_correct / quiz_week_total * 100, 1) if quiz_week_total else 0

            devstats_message = f"""📊 **Developer Statistics Dashboard**
━━━━━━━━━━━━━━━━━━━

⚙️ **System Health**
• Uptime: {uptime_str}
• Memory: {memory_mb:.1f} MB (avg: {mem_avg_24h:.1f} MB)
• Avg Response: {rt_avg_24h:.0f}ms

📊 **Activity Breakdown** (Last 24h)
• Commands Executed: {commands_24h:,}
• Quizzes Sent: {quizzes_sent_24h:,}
• Quizzes Answered: {quizzes_answered_24h:,}
• Broadcasts Sent: {broadcasts_24h:,}
• Errors Logged: {errors_24h:,}

👥 **User Engagement**
• Total Users: {total_users:,}
• Active Today: {active_today}
• Active This Week: {active_week}
• Active This Month: {active_month}
• New Users (7d): {new_users}

📝 **Quiz Performance**
• Sent Today: {quiz_today_total}
• Sent This Week: {quiz_week_total}
• Success Rate: {success_rate}%

🏆 **Most Active Users**
{most_active_text}

📜 **Recent Activity Feed**
{activity_feed}

━━━━━━━━━━━━━━━━━━━
🕐 Generated in {(time.time() - start_time)*1000:.0f}ms"""
            
            keyboard = [
                [InlineKeyboardButton("🔄 Refresh", callback_data="devstats_refresh")],
                [
                    InlineKeyboardButton("📊 Full Activity", callback_data="devstats_activity"),
                    InlineKeyboardButton("⚡ Performance", callback_data="devstats_performance")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await loading_msg.edit_text(
                devstats_message,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup
            )
            
            response_time = int((time.time() - start_time) * 1000)
            logger.info(f"/devstats shown in {response_time}ms")
            
            self.db.log_activity(
                activity_type='command',
                user_id=update.effective_user.id,
                chat_id=update.effective_chat.id,
                username=update.effective_user.username or "",
                details={'command': 'devstats'},
                success=True,
                response_time_ms=response_time
            )
            
        except Exception as e:
            logger.error(f"Error in devstats: {e}", exc_info=True)
            if update.message:
                reply = await update.message.reply_text("❌ Error loading dev statistics")
                await self.auto_clean_message(update.message, reply)
    
    async def activity(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Live activity stream with filtering and pagination"""
        start_time = time.time()
        try:
            if not await self.check_access(update):
                await self.send_unauthorized_message(update)
                return
            
            if not update.effective_user or not update.effective_chat or not update.message:
                return
            
            activity_type = context.args[0] if context.args else 'all'
            page = int(context.args[1]) if context.args and len(context.args) > 1 else 1
            
            valid_types = ['all', 'command', 'quiz_sent', 'quiz_answered', 'broadcast', 'error']
            if activity_type not in valid_types:
                activity_type = 'all'
            
            limit = 50
            offset = (page - 1) * limit
            
            loading_msg = await update.message.reply_text(f"📜 Loading activity stream ({activity_type})...")
            
            if activity_type == 'all':
                activities = self.db.get_recent_activities(limit)
            else:
                activities = self.db.get_recent_activities(limit, activity_type)
            
            if not activities:
                await loading_msg.edit_text(f"📜 No activities found for type: {activity_type}")
                return
            
            activity_text = f"""📜 **Live Activity Stream**
Type: {activity_type.upper()}
━━━━━━━━━━━━━━━━━━━

"""
            
            for activity in activities[:50]:
                time_ago = self.db.format_relative_time(activity['timestamp'])
                activity_type_str = activity.get('type') or activity.get('activity_type', 'unknown')
                user_id = activity.get('user_id')
                username = activity.get('username', 'Unknown')
                chat_title = activity.get('chat_title', '')
                
                details = activity.get('details', {})
                if isinstance(details, dict):
                    if activity_type_str == 'command':
                        cmd = details.get('command', 'unknown')
                        activity_text += f"[{time_ago}] @{username}: /{cmd}\n"
                    elif activity_type_str == 'quiz_sent':
                        if chat_title:
                            activity_text += f"[{time_ago}] Quiz sent to {chat_title}\n"
                        else:
                            activity_text += f"[{time_ago}] Quiz sent\n"
                    elif activity_type_str == 'quiz_answered':
                        correct = details.get('is_correct', False)
                        emoji = "✅" if correct else "❌"
                        activity_text += f"[{time_ago}] {emoji} @{username} answered\n"
                    elif activity_type_str == 'broadcast':
                        recipients = details.get('total_recipients', 0)
                        activity_text += f"[{time_ago}] Broadcast to {recipients} recipients\n"
                    elif activity_type_str == 'error':
                        error_msg = details.get('error', 'Unknown error')[:50]
                        activity_text += f"[{time_ago}] ❌ Error: {error_msg}\n"
                    else:
                        activity_text += f"[{time_ago}] {activity_type_str}\n"
                else:
                    activity_text += f"[{time_ago}] {activity_type_str}\n"
            
            activity_text += f"""
━━━━━━━━━━━━━━━━━━━
📊 Showing {len(activities[:50])} activities
🕐 Loaded in {(time.time() - start_time)*1000:.0f}ms"""
            
            keyboard = [
                [
                    InlineKeyboardButton("🔄 Refresh", callback_data=f"activity_refresh_{activity_type}"),
                    InlineKeyboardButton("🔙 All Types", callback_data="activity_all")
                ],
                [
                    InlineKeyboardButton("💬 Commands", callback_data="activity_command"),
                    InlineKeyboardButton("📝 Quizzes", callback_data="activity_quiz_sent")
                ],
                [
                    InlineKeyboardButton("✅ Answers", callback_data="activity_quiz_answered"),
                    InlineKeyboardButton("❌ Errors", callback_data="activity_error")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await loading_msg.edit_text(
                activity_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup
            )
            
            response_time = int((time.time() - start_time) * 1000)
            logger.info(f"/activity shown in {response_time}ms")
            
            self.db.log_activity(
                activity_type='command',
                user_id=update.effective_user.id,
                chat_id=update.effective_chat.id,
                username=update.effective_user.username or "",
                details={'command': 'activity', 'filter': activity_type},
                success=True,
                response_time_ms=response_time
            )
            
        except Exception as e:
            logger.error(f"Error in activity: {e}", exc_info=True)
            if update.message:
                reply = await update.message.reply_text("❌ Error loading activity stream")
                await self.auto_clean_message(update.message, reply)
    
    async def editquiz(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Interactive quiz editor with inline keyboards (Developer only)"""
        if not update.effective_user or not update.effective_message:
            return
        
        start_time = time.time()
        
        try:
            if not await self.check_access(update):
                await self.send_unauthorized_message(update)
                return
            
            # Check if replying to a quiz message
            if update.message and update.message.reply_to_message:
                quiz_id = self.extract_quiz_id_from_message(update.message.reply_to_message, context)
                
                if quiz_id:
                    # Jump directly to edit mode for this quiz
                    quiz = self.db.get_question_by_id(quiz_id)
                    
                    if quiz:
                        logger.info(f"Editing quiz #{quiz_id} via reply")
                        await self._show_quiz_editor(update, context, quiz_id)
                        
                        response_time = int((time.time() - start_time) * 1000)
                        self.db.log_activity(
                            activity_type='command',
                            user_id=update.effective_user.id,
                            chat_id=update.effective_message.chat_id,
                            username=update.effective_user.username or "",
                            command='/editquiz',
                            details={'quiz_id': quiz_id, 'via_reply': True},
                            success=True,
                            response_time_ms=response_time
                        )
                        return
                    else:
                        reply = await update.message.reply_text(
                            f"❌ Quiz #{quiz_id} not found in database."
                        )
                        await self.auto_clean_message(update.message, reply)
                        return
                else:
                    # Could not extract quiz ID
                    reply = await update.message.reply_text(
                        "❌ Could not find quiz ID in the replied message.\n\n"
                        "💡 Reply to a quiz poll to edit it,\n"
                        "or use /editquiz to browse all quizzes."
                    )
                    await self.auto_clean_message(update.message, reply)
                    return
            
            # Original behavior: show quiz list or specific quiz
            args = context.args if context.args else []
            
            if len(args) > 0 and args[0].isdigit():
                quiz_id = int(args[0])
                await self._show_quiz_editor(update, context, quiz_id)
            else:
                page = int(args[0]) if len(args) > 0 and args[0].isdigit() else 1
                await self._show_quiz_list(update, context, page)
            
            response_time = int((time.time() - start_time) * 1000)
            self.db.log_activity(
                activity_type='command',
                user_id=update.effective_user.id,
                chat_id=update.effective_message.chat_id,
                username=update.effective_user.username or "",
                command='/editquiz',
                success=True,
                response_time_ms=response_time
            )
            
        except Exception as e:
            logger.error(f"Error in editquiz: {e}", exc_info=True)
            if update.effective_message:
                await update.effective_message.reply_text(
                    "❌ Error loading quiz editor. Please try again.",
                    parse_mode=ParseMode.MARKDOWN
                )
    
    async def _show_quiz_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 1) -> None:
        """Show paginated quiz list with selection buttons"""
        questions = self.db.get_all_questions()
        
        if not questions:
            if update.effective_message:
                await update.effective_message.reply_text(
                    "📭 No quizzes found.\n\nAdd new quizzes using /addquiz command.",
                    parse_mode=ParseMode.MARKDOWN
                )
            return
        
        per_page = 10
        total_pages = (len(questions) + per_page - 1) // per_page
        page = max(1, min(page, total_pages))
        
        start_idx = (page - 1) * per_page
        end_idx = min(start_idx + per_page, len(questions))
        
        text = f"""🔍 **Select Quiz to Edit**
━━━━━━━━━━━━━━━━━━━━━

📊 Total: {len(questions)} quizzes
📄 Page {page}/{total_pages}

"""
        
        keyboard = []
        for i in range(start_idx, end_idx):
            q = questions[i]
            category = q.get('category', 'N/A')
            question_preview = q['question'][:50] + '...' if len(q['question']) > 50 else q['question']
            text += f"{i + 1}. {question_preview}\n   📂 Category: {category or 'Uncategorized'}\n\n"
            keyboard.append([InlineKeyboardButton(
                f"✏️ Edit #{q['id']}: {question_preview[:30]}...",
                callback_data=f"edit_quiz_select_{q['id']}"
            )])
        
        nav_buttons = []
        if page > 1:
            nav_buttons.append(InlineKeyboardButton("◀️ Prev", callback_data=f"edit_quiz_list_{page-1}"))
        if page < total_pages:
            nav_buttons.append(InlineKeyboardButton("Next ▶️", callback_data=f"edit_quiz_list_{page+1}"))
        nav_buttons.append(InlineKeyboardButton("❌ Cancel", callback_data="edit_quiz_cancel"))
        
        if nav_buttons:
            keyboard.append(nav_buttons)
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.callback_query:
            await update.callback_query.edit_message_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup
            )
        else:
            if update.effective_message:
                await update.effective_message.reply_text(
                    text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=reply_markup
                )
    
    async def _show_quiz_editor(self, update: Update, context: ContextTypes.DEFAULT_TYPE, quiz_id: int) -> None:
        """Show quiz editor interface with current values"""
        quiz = self.db.get_question_by_id(quiz_id)
        
        if not quiz:
            error_text = f"""❌ **Quiz Not Found**

Quiz ID #{quiz_id} doesn't exist.

💡 Use /totalquiz to see all available quizzes."""
            
            if update.callback_query:
                await update.callback_query.edit_message_text(
                    error_text,
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                if update.effective_message:
                    await update.effective_message.reply_text(
                        error_text,
                        parse_mode=ParseMode.MARKDOWN
                    )
            return
        
        if context.user_data is not None:
            context.user_data[f'editing_quiz_{quiz_id}'] = {
                'id': quiz['id'],
                'question': quiz['question'],
                'options': quiz['options'],
                'correct_answer': quiz['correct_answer'],
                'category': quiz.get('category'),
                'original': quiz.copy()
            }
        
        text = self._format_quiz_editor(quiz)
        keyboard = [
            [
                InlineKeyboardButton("✏️ Edit Question", callback_data=f"edit_quiz_question_{quiz_id}"),
                InlineKeyboardButton("📝 Edit Options", callback_data=f"edit_quiz_options_{quiz_id}")
            ],
            [
                InlineKeyboardButton("📂 Change Category", callback_data=f"edit_quiz_category_{quiz_id}"),
                InlineKeyboardButton("✅ Change Answer", callback_data=f"edit_quiz_answer_{quiz_id}")
            ],
            [
                InlineKeyboardButton("💾 Save Changes", callback_data=f"edit_quiz_save_{quiz_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data="edit_quiz_cancel")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        if update.callback_query:
            await update.callback_query.edit_message_text(
                text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup
            )
        else:
            if update.effective_message:
                await update.effective_message.reply_text(
                    text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=reply_markup
                )
    
    def _format_quiz_editor(self, quiz: dict) -> str:
        """Format quiz data for editor display"""
        options_text = ""
        for i, opt in enumerate(quiz['options']):
            marker = "✓" if i == quiz['correct_answer'] else "○"
            letter = chr(65 + i)
            options_text += f"{letter}) {opt} {marker}\n"
        
        category = quiz.get('category') or 'Uncategorized'
        correct_letter = chr(65 + quiz['correct_answer'])
        
        return f"""✏️ **Edit Quiz #{quiz['id']}**
━━━━━━━━━━━━━━━━━━━━━

**Current Question:**
{quiz['question']}

**Options:**
{options_text}
**📂 Category:** {category}
**✅ Correct Answer:** {correct_letter}

━━━━━━━━━━━━━━━━━━━━━
Select what to edit:"""
    
    async def handle_edit_quiz_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle all edit quiz callback queries"""
        if not update.callback_query or not update.effective_user:
            return
        
        query = update.callback_query
        await query.answer()
        
        if not await self.check_access(update):
            await query.edit_message_text("❌ Unauthorized access.")
            return
        
        data = query.data
        
        if not data:
            return
        
        if data == "edit_quiz_cancel":
            await query.edit_message_text("✅ Quiz editing cancelled.")
            return
        
        if data.startswith("edit_quiz_list_"):
            page = int(data.split("_")[-1])
            await self._show_quiz_list(update, context, page)
        
        elif data.startswith("edit_quiz_select_"):
            quiz_id = int(data.split("_")[-1])
            await self._show_quiz_editor(update, context, quiz_id)
        
        elif data.startswith("edit_quiz_question_"):
            quiz_id = int(data.split("_")[-1])
            if context.user_data is not None:
                context.user_data['waiting_for'] = f'quiz_question_{quiz_id}'
            await query.edit_message_text(
                f"✏️ **Edit Question**\n\nPlease send the new question text:",
                parse_mode=ParseMode.MARKDOWN
            )
        
        elif data.startswith("edit_quiz_options_"):
            quiz_id = int(data.split("_")[-1])
            if context.user_data is not None:
                context.user_data['waiting_for'] = f'quiz_options_{quiz_id}'
            await query.edit_message_text(
                f"""📝 **Edit Options**

Please send options in this format:
`Option1|Option2|Option3|Option4`

Example:
`Paris|London|Berlin|Rome`""",
                parse_mode=ParseMode.MARKDOWN
            )
        
        elif data.startswith("edit_quiz_category_"):
            quiz_id = int(data.split("_")[-1])
            await self._show_category_selector(update, context, quiz_id)
        
        elif data.startswith("edit_quiz_answer_"):
            quiz_id = int(data.split("_")[-1])
            await self._show_answer_selector(update, context, quiz_id)
        
        elif data.startswith("edit_quiz_set_category_"):
            parts = data.split("_")
            quiz_id = int(parts[4])
            category = "_".join(parts[5:])
            if category == "none":
                category = None
            else:
                category = category.replace("_", " ")
            
            if context.user_data is not None:
                quiz_data = context.user_data.get(f'editing_quiz_{quiz_id}')
                if quiz_data:
                    quiz_data['category'] = category
                    await self._show_quiz_editor(update, context, quiz_id)
        
        elif data.startswith("edit_quiz_set_answer_"):
            parts = data.split("_")
            quiz_id = int(parts[4])
            answer_idx = int(parts[5])
            
            if context.user_data is not None:
                quiz_data = context.user_data.get(f'editing_quiz_{quiz_id}')
                if quiz_data:
                    quiz_data['correct_answer'] = answer_idx
                    await self._show_quiz_editor(update, context, quiz_id)
        
        elif data.startswith("edit_quiz_save_"):
            quiz_id = int(data.split("_")[-1])
            await self._save_quiz_changes(update, context, quiz_id)
    
    async def _show_category_selector(self, update: Update, context: ContextTypes.DEFAULT_TYPE, quiz_id: int) -> None:
        """Show category selection keyboard"""
        categories = ["General Knowledge", "Science", "History", "Geography", "Sports", 
                     "Entertainment", "Technology", "Mathematics", "Literature", "Art"]
        
        keyboard = []
        row = []
        for cat in categories:
            cat_key = cat.replace(" ", "_")
            row.append(InlineKeyboardButton(cat, callback_data=f"edit_quiz_set_category_{quiz_id}_{cat_key}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        
        keyboard.append([
            InlineKeyboardButton("❌ No Category", callback_data=f"edit_quiz_set_category_{quiz_id}_none"),
            InlineKeyboardButton("🔙 Back", callback_data=f"edit_quiz_select_{quiz_id}")
        ])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        if update.callback_query:
            await update.callback_query.edit_message_text(
                "📂 **Select Category**\n\nChoose a category for this quiz:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup
            )
    
    async def _show_answer_selector(self, update: Update, context: ContextTypes.DEFAULT_TYPE, quiz_id: int) -> None:
        """Show answer selection keyboard"""
        if context.user_data is None:
            if update.callback_query:
                await update.callback_query.edit_message_text("❌ Quiz data not found.")
            return
        
        quiz_data = context.user_data.get(f'editing_quiz_{quiz_id}')
        if not quiz_data:
            if update.callback_query:
                await update.callback_query.edit_message_text("❌ Quiz data not found.")
            return
        
        keyboard = []
        for i, opt in enumerate(quiz_data['options']):
            letter = chr(65 + i)
            current = "✓" if i == quiz_data['correct_answer'] else ""
            keyboard.append([InlineKeyboardButton(
                f"{letter}) {opt} {current}",
                callback_data=f"edit_quiz_set_answer_{quiz_id}_{i}"
            )])
        
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data=f"edit_quiz_select_{quiz_id}")])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        if update.callback_query:
            await update.callback_query.edit_message_text(
                "✅ **Select Correct Answer**\n\nChoose the correct option:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup
            )
    
    async def _save_quiz_changes(self, update: Update, context: ContextTypes.DEFAULT_TYPE, quiz_id: int) -> None:
        """Save changes to quiz in database"""
        if context.user_data is None:
            if update.callback_query:
                await update.callback_query.edit_message_text("❌ Quiz data not found.")
            return
        
        quiz_data = context.user_data.get(f'editing_quiz_{quiz_id}')
        if not quiz_data:
            if update.callback_query:
                await update.callback_query.edit_message_text("❌ Quiz data not found.")
            return
        
        try:
            success = self.db.update_question(
                qid=quiz_id,
                question=quiz_data['question'],
                options=quiz_data['options'],
                correct_answer=quiz_data['correct_answer'],
                category=quiz_data.get('category')
            )
            
            if success:
                changes = []
                original = quiz_data['original']
                if original['question'] != quiz_data['question']:
                    changes.append('question')
                if original['options'] != quiz_data['options']:
                    changes.append('options')
                if original['correct_answer'] != quiz_data['correct_answer']:
                    changes.append('correct_answer')
                if original.get('category') != quiz_data.get('category'):
                    changes.append('category')
                
                if update.effective_user and update.callback_query and update.callback_query.message:
                    chat_id = getattr(update.callback_query.message, 'chat_id', None)
                    if chat_id:
                        self.db.log_activity(
                            activity_type='quiz_edited',
                            user_id=update.effective_user.id,
                            chat_id=chat_id,
                            username=update.effective_user.username or "",
                            details={'quiz_id': quiz_id, 'changes': changes},
                            success=True
                        )
                
                text = self._format_quiz_editor(quiz_data)
                text = text.replace("Select what to edit:", f"✅ **Changes Saved Successfully!**\n\nModified: {', '.join(changes)}")
                
                if update.callback_query:
                    await update.callback_query.edit_message_text(
                        text,
                        parse_mode=ParseMode.MARKDOWN
                    )
                
                if context.user_data is not None:
                    del context.user_data[f'editing_quiz_{quiz_id}']
            else:
                if update.callback_query:
                    await update.callback_query.edit_message_text("❌ Failed to save changes. Quiz not found.")
        
        except Exception as e:
            logger.error(f"Error saving quiz changes: {e}")
            if update.callback_query:
                await update.callback_query.edit_message_text("❌ Error saving changes. Please try again.")
    
    async def handle_text_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle text input for quiz editing"""
        if not update.message or not update.effective_user or not update.message.text:
            return
        
        if context.user_data is None:
            return
        
        waiting_for = context.user_data.get('waiting_for')
        if not waiting_for:
            return
        
        text = update.message.text.strip()
        
        if waiting_for.startswith('quiz_question_'):
            quiz_id = int(waiting_for.split('_')[-1])
            if context.user_data is not None:
                quiz_data = context.user_data.get(f'editing_quiz_{quiz_id}')
                if quiz_data:
                    quiz_data['question'] = text
                    context.user_data.pop('waiting_for', None)
                    
                    await update.message.reply_text(
                        f"✅ Question updated!\n\nUse /editquiz {quiz_id} to continue editing.",
                        parse_mode=ParseMode.MARKDOWN
                    )
        
        elif waiting_for.startswith('quiz_options_'):
            quiz_id = int(waiting_for.split('_')[-1])
            if context.user_data is not None:
                quiz_data = context.user_data.get(f'editing_quiz_{quiz_id}')
                if quiz_data:
                    options = [opt.strip() for opt in text.split('|')]
                    if len(options) != 4:
                        await update.message.reply_text(
                            "❌ Invalid format. Please provide exactly 4 options separated by |",
                            parse_mode=ParseMode.MARKDOWN
                        )
                        return
                    
                    quiz_data['options'] = options
                    context.user_data.pop('waiting_for', None)
                    
                    await update.message.reply_text(
                        f"✅ Options updated!\n\nUse /editquiz {quiz_id} to continue editing.",
                        parse_mode=ParseMode.MARKDOWN
                    )

